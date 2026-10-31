"""Per-rule runtime engine for the Entity Guard integration."""

from __future__ import annotations

import asyncio
import bisect
import logging
import math
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Context, Event, HassJob, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)
from homeassistant.helpers.event import (
    async_call_later,
    async_track_point_in_time,
    async_track_state_change_event,
    async_track_time_change,
)
from homeassistant.util import dt as dt_util

from .const import (
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_RGB_COLOR,
    ATTRIBUTE_SERVICE_MAP,
    COLOR_ATTRIBUTES,
    COLOR_RGB_TOLERANCE,
    COLOR_TEMP_KELVIN_TOLERANCE,
    CONSECUTIVE_EXHAUSTION_LIMIT,
    DEFAULT_LOOP_SUPPRESS_MINUTES,
    DOMAIN,
    DOMAIN_MODE_SERVICE_MAP,
    DOMAIN_SERVICE_MAP,
    ENFORCE_TIMEOUT_SECONDS,
    ERROR_RECOVERY_SUCCESS_THRESHOLD,
    ERROR_THRESHOLD,
    EVENT_CANCELLED,
    EVENT_ENFORCED,
    EVENT_LOOP_DETECTED,
    EVENT_SKIPPED,
    EVENT_SUPPRESSED,
    EVENT_UNREACHABLE,
    EVENT_VERIFIED,
    FOLLOW_ON_STATES,
    MODE_DYNAMIC_ATTRIBUTE,
    MODE_DYNAMIC_STATE,
    MODE_FIXED_ATTRIBUTE,
    MODE_FIXED_STATE,
    OPERATOR_GE,
    OPERATOR_GT,
    OPERATOR_LE,
    OPERATOR_LT,
    REASON_SERVICE_CALL_FAILED,
    REASON_UNREACHABLE_AFTER_RETRIES,
    RECENTLY_ENFORCED_WINDOW_SECONDS,
    SOURCE_VALUE_FROM_ATTRIBUTE,
    STARTUP_GRACE_PERIOD_SECONDS,
    STATUS_ARMED,
    STATUS_CONDITIONAL,
    STATUS_COOLDOWN,
    STATUS_DISABLED,
    STATUS_ENFORCING,
    STATUS_ERROR,
    STATUS_MASTER_DISABLED,
    STATUS_PENDING,
    STATUS_STARTING,
    STATUS_SUPPRESSED,
    STATUS_VERIFYING,
    VERIFY_DELAY_SECONDS,
    VERIFY_MAX_ATTEMPTS,
    signal_master,
    signal_rule_update,
)
from .issue_helpers import (
    async_raise_unsupported_domain,
    async_raise_unsupported_state,
)
from .models import RuleConfig, RuleRuntimeState
from .storage import EntityGuardStore

_LOGGER = logging.getLogger(__name__)


def signal_for_rule(rule_id: str) -> str:
    """Dispatcher signal name for a per-rule status update."""
    return signal_rule_update(rule_id)


def signal_master_update() -> str:
    """Dispatcher signal name for hub master switch updates."""
    return signal_master()


class RuleEngine:
    """Runs evaluation, enforcement, debounce, and rate-limiting for one rule."""

    def __init__(
        self,
        hass: HomeAssistant,
        config: RuleConfig,
        store: EntityGuardStore,
        master_enabled_getter: Callable[[], bool],
        entry: ConfigEntry | None = None,
    ) -> None:
        """Initialize the engine."""
        self._hass = hass
        self._config = config
        self._store = store
        self._master_enabled_getter = master_enabled_getter
        self._entry = entry
        self._state: RuleRuntimeState = RuleRuntimeState()
        self._unsub_callbacks: list[Callable[[], None]] = []
        self._pending_enforcements: dict[str, Callable[[], None]] = {}
        self._pending_enforcement_due: dict[str, datetime] = {}
        self._pending_eval_tasks: dict[str, asyncio.Task] = {}
        # Verify-after-enforce (opt-in, state modes). Per-target one-shot verify
        # timers armed on a successful enforce; _verify_attempts counts corrective
        # re-enforces per target. On exhaustion the target re-enters the counted
        # enforcing cycle (self-heal) rather than dead-ending; _consecutive_exhaustions
        # counts full exhaustions per target and trips loop protection at
        # CONSECUTIVE_EXHAUSTION_LIMIT. _unreachable is now only a brief double-arm
        # guard across the corrective window (no longer a permanent latch).
        self._pending_verifications: dict[str, Callable[[], None]] = {}
        self._verify_attempts: dict[str, int] = {}
        # Entities whose in-flight verify was armed by a MANUAL force (test_enforce).
        # A forced cycle bypasses the suppressed/flags_unmet drops (see the
        # VERIFY-OUTCOME CONTRACT above _arm_verify). Mirrors _verify_attempts'
        # lifecycle: populated at arm, cleared wherever a cycle resolves/drops.
        self._forced_verifications: set[str] = set()
        self._unreachable: set[str] = set()
        self._consecutive_exhaustions: dict[str, int] = {}
        self._startup_complete = False
        self._is_unloaded: bool = False
        self._current_status: str = STATUS_STARTING
        self._flag_entity_ids: frozenset[str] = frozenset(
            f.entity for f in config.flags
        )
        self._cooldown_broadcast_unsubs: dict[str, Callable[[], None]] = {}
        self._recently_enforced: bool = False
        self._recently_enforced_unsub: Callable[[], None] | None = None
        # Verify-confirmed twin of recently_enforced. Armed only by _fire_verified
        # (verify-on, confirmed-success path); backs the recently_enforced_verified
        # sensor that exists only while verify is enabled.
        self._recently_enforced_verified: bool = False
        self._recently_enforced_verified_unsub: Callable[[], None] | None = None
        # Failure twin of recently_enforced_verified. Armed only by _fire_unreachable
        # (verify-on, exhausted-retries path); backs the recently_enforced_unreachable
        # sensor that exists only while verify is enabled.
        self._recently_enforced_unreachable: bool = False
        self._recently_enforced_unreachable_unsub: Callable[[], None] | None = None
        # Abandoned-cycle twin of recently_enforced_verified. Armed only by
        # _fire_cancelled (verify-on, verify armed then dropped because an external
        # condition changed mid-window — disabled/master-off/suppressed/flags-unmet,
        # NOT a device fault); backs the recently_enforced_cancelled sensor that
        # exists only while verify is enabled. The neutral sibling of verified
        # (success) / unreachable (device failure): no verdict was reached.
        self._recently_enforced_cancelled: bool = False
        self._recently_enforced_cancelled_unsub: Callable[[], None] | None = None
        # One-shot suppression-expiry timer (EG-4). Cancelled and replaced when
        # suppression is set, cleared, or the rule's lifecycle changes.
        self._suppression_timer_unsub: Callable[[], None] | None = None
        # Cached entity_id of this rule's status sensor — used in EVENT_ENFORCED so
        # the logbook "filter by integration" query finds the event via event_data match.
        self._rule_entity_id: str | None = None

    @property
    def config(self) -> RuleConfig:
        """Return the rule's static config."""
        return self._config

    @property
    def _is_dynamic(self) -> bool:
        """True for the source-driven modes (dynamic_state / dynamic_attribute)."""
        return self._config.mode in (MODE_DYNAMIC_STATE, MODE_DYNAMIC_ATTRIBUTE)

    @property
    def state(self) -> RuleRuntimeState:
        """Return the live runtime state."""
        return self._state

    async def async_setup(self) -> None:
        """Subscribe listeners, restore persisted state, schedule startup grace."""
        _LOGGER.debug(
            "Engine setup: rule=%s targets=%s flags=%d",
            self._config.name,
            self._config.target_entities,
            len(self._config.flags),
        )
        blob = self._store.get_rule_state(self._config.unique_id)
        restored = EntityGuardStore.blob_to_runtime(blob)
        # Preserve the freshly created lock — locks are intentionally not persisted.
        restored.reentrance_lock = self._state.reentrance_lock
        self._state = restored
        if blob:
            _LOGGER.debug(
                "Restored persisted state: total=%d today=%d suppressed_until=%s",
                self._state.enforcement_count_total,
                self._state.enforcement_count_today,
                self._state.suppressed_until,
            )

        self._maybe_reset_today_counter()
        self._maybe_backfill_counter_since()

        # Restore suppression-expiry timer if a persisted suppression window is still
        # in the future (EG-4). Older expiry is cleared lazily on the next evaluation.
        self._schedule_suppression_timer()

        watched: set[str] = set(self._config.target_entities)
        watched.update(f.entity for f in self._config.flags)
        # Dynamic modes: the source entity is an extra watched input (never a
        # target, never enforced). Its changes drive a sweep of all targets.
        if self._is_dynamic and self._config.source_entity:
            watched.add(self._config.source_entity)
        if watched:
            self._unsub_callbacks.append(
                async_track_state_change_event(
                    self._hass, list(watched), self._handle_state_event
                )
            )

        self._unsub_callbacks.append(
            async_track_time_change(
                self._hass, self._handle_midnight, hour=0, minute=0, second=0
            )
        )

        self._unsub_callbacks.append(
            async_call_later(
                self._hass,
                STARTUP_GRACE_PERIOD_SECONDS,
                self._handle_startup_grace_done,
            )
        )

        self._unsub_callbacks.append(
            async_dispatcher_connect(
                self._hass, signal_master_update(), self._handle_master_changed
            )
        )

        # Immediately reflect disabled/master-disabled state so the UI never
        # shows STATUS_STARTING when the rule is off. For enabled rules we
        # still broadcast STARTING (startup grace not yet complete).
        if not self._state.enabled or not self._master_enabled_getter():
            self._apply_idle_status()
        else:
            self._broadcast_status()

    async def async_unload(self) -> None:
        """Cancel listeners and pending enforcement timers."""
        self._is_unloaded = True
        _LOGGER.debug("Engine unload: rule=%s", self._config.name)
        for unsub in self._unsub_callbacks:
            try:
                unsub()
            except Exception:
                _LOGGER.debug("Listener unsubscribe failed", exc_info=True)
        self._unsub_callbacks.clear()

        for cancel in list(self._pending_enforcements.values()):
            try:
                cancel()
            except Exception:
                _LOGGER.debug("Pending enforcement cancel failed", exc_info=True)
        self._pending_enforcements.clear()
        self._pending_enforcement_due.clear()

        # Verify timers (lifecycle gap): a verify timer firing after unload would
        # enforce on a torn-down rule — cancel + clear like _pending_enforcements.
        for cancel in list(self._pending_verifications.values()):
            try:
                cancel()
            except Exception:
                _LOGGER.debug("Pending verify cancel failed", exc_info=True)
        self._pending_verifications.clear()
        self._verify_attempts.clear()
        self._forced_verifications.clear()
        self._unreachable.clear()
        self._consecutive_exhaustions.clear()

        for task in list(self._pending_eval_tasks.values()):
            task.cancel()
        self._pending_eval_tasks.clear()

        for cancel in list(self._cooldown_broadcast_unsubs.values()):
            try:
                cancel()
            except Exception:
                pass
        self._cooldown_broadcast_unsubs.clear()

        self._cancel_suppression_timer()
        self._cancel_recently_enforced_timer()
        self._cancel_recently_enforced_verified_timer()
        self._cancel_recently_enforced_unreachable_timer()
        self._cancel_recently_enforced_cancelled_timer()

        self._persist()
        await self._store.async_save_now()

    @callback
    def _handle_state_event(self, event: Event) -> None:
        """State change listener — schedules async evaluation, deduplicating per entity."""
        entity_id = event.data.get("entity_id")
        new_state = event.data.get("new_state")
        if entity_id is None:
            return
        # A REAL state_changed for a target clears the unreachable latch (the target
        # moved, so it is reachable again and may be re-verified) and resets the
        # self-heal counters. What it does to an in-flight verify depends on WHERE it
        # moved: a move that reached desired IS the success the verify was armed to
        # confirm — so when a verify is pending and the target reached desired we
        # CONFIRM NOW. This is the confirm route for a target whose ACK lands just
        # AFTER the enforce call returned (the enforce tail's synchronous fast-read
        # already saw not-reached and armed this listener); the event lands well
        # before the 5 s deadline, collapsing the old blind dwell to device latency.
        # A responsive blocking=True target instead commits its state DURING the
        # call and is confirmed in-tick by the enforce tail — never reaching here.
        # A move still OFF desired (or offline/unknown) is a genuine
        # away-move that makes the in-flight cycle stale: cancel it, the reactive path
        # re-evaluates. (Offline/unknown counts as not-reached so we never confirm on
        # a vanished target — the timer's _verify_reached must not be the only guard.)
        if entity_id in self._config.target_entities:
            reached_desired = (
                new_state is not None
                and new_state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN)
                and not self._trigger_decision(entity_id, new_state)[0]
            )
            pending = entity_id in self._pending_verifications
            # Capture forced-origin NOW: every teardown below routes through
            # _cancel_verify, which discards _forced_verifications — so membership
            # is gone before the confirm/cancelled fire. Read it while it holds.
            forced = entity_id in self._forced_verifications
            offline = new_state is None or new_state.state in (
                STATE_UNAVAILABLE,
                STATE_UNKNOWN,
            )
            if not reached_desired and (offline or not pending):
                # Cancel the stale deadline timer on a genuine away-move: the target
                # vanished (offline/unknown — never confirm on it), or no verify is
                # in flight so this is a plain reactive supersede. BUT when a verify
                # IS pending and the target is merely still-online-not-yet-reached
                # (a light GROUP reads any-on 'on' mid fan-out until its LAST member
                # turns off), do NOT cancel: that intermediate event is the enforce's
                # own in-progress transition, not an away-move. The in-flight verify
                # owns the deadline timer + corrective loop and will confirm on the
                # final reached event. Cancelling here popped pending, so the confirm
                # survived only by racing a re-enforce — flaky; prod lost the race
                # and recently_enforced_verified never pulsed.
                self._cancel_verify(entity_id)
            # Clear the self-heal markers BEFORE a possible confirm: the target moved,
            # so the superseded cycle is abandoned. Reset here — the genuine-event
            # site — NOT inside _cancel_verify, which the escalation path calls
            # mid-cycle right after incrementing. Without this the next enforce's
            # fresh verify cycle would inherit a stale count and latch early.
            self._unreachable.discard(entity_id)
            self._consecutive_exhaustions.pop(entity_id, None)
            if (
                reached_desired
                and pending
                and not self._verify_event_dropped(entity_id)
            ):
                # HAPPY PATH, event-driven confirm. Order is load-bearing:
                #   1. _cancel_verify pops _pending_verifications AND unsubscribes the
                #      deadline cb — so the timer can never also fire (no double
                #      EVENT_VERIFIED, no stray exhaustion path).
                #   2. _confirm_verified then re-derives status against an EMPTY
                #      pending map, leaving VERIFYING for ARMED/COOLDOWN. Cancel MUST
                #      precede confirm, else the derive still sees pending → stuck.
                self._cancel_verify(entity_id)
                self._confirm_verified(entity_id, forced)
            else:
                # No confirm (not reached, no pending, or dropped for disabled/
                # suppressed): abandon the superseded cycle's attempt count. Re-derive
                # whenever a verify was pending so a VERIFYING card resolves NOW
                # instead of lingering until the ~5s deadline _fire:
                #   - not reached: _cancel_verify above already popped pending, so the
                #     derive yields the real idle status (ARMED/COOLDOWN).
                #   - reached but DROPPED (disabled/master-off/suppressed): the
                #     target already complied, so no VERIFYING is owed. Drop the
                #     stale deadline handle too — else an unsuppress / suppression
                #     expiry + re-arm BEFORE the ~5s deadline fires clears
                #     suppressed_until while _pending_verifications still holds this
                #     entity, and the next derive falls through to the
                #     pending→VERIFYING branch: a phantom VERIFYING on an
                #     already-compliant target until the stale timer finally fires.
                #     _derive_idle_status ranks DISABLED/MASTER_DISABLED/SUPPRESSED
                #     above VERIFYING, so the card is correct WHILE dropped; the
                #     cancel only prevents the stale handle outliving the drop.
                self._verify_attempts.pop(entity_id, None)
                if pending:
                    if reached_desired:
                        # Reached-but-DROPPED: the twin of the deadline _fire drop
                        # sites. Fire the abandoned-cycle signal with the same
                        # reason the deadline path would, EXCEPT unloaded (entry
                        # tearing down — no bus event, its pulse timer would be
                        # cancelled immediately). _cancel_verify pops pending so the
                        # deadline _fire can't also fire cancelled for this cycle.
                        reason = self._verify_drop_reason(entity_id)
                        self._cancel_verify(entity_id)
                        if reason is not None and reason != "unloaded":
                            self._fire_cancelled(entity_id, reason, forced)
                    self._apply_idle_status()
        trigger_context_id: str | None = getattr(event.context, "id", None)
        self._schedule_eval_task(entity_id, new_state, trigger_context_id)

    def _schedule_eval_task(
        self,
        entity_id: str,
        new_state: Any,
        trigger_context_id: str | None = None,
    ) -> None:
        """Schedule async_evaluate for entity_id, cancelling any prior pending task."""
        existing = self._pending_eval_tasks.pop(entity_id, None)
        if existing is not None and not existing.done():
            existing.cancel()
        task = self._hass.async_create_task(
            self.async_evaluate(entity_id, new_state, trigger_context_id)
        )
        if task is not None:
            self._pending_eval_tasks[entity_id] = task
            task.add_done_callback(
                lambda t, eid=entity_id: self._pending_eval_tasks.pop(eid, None)
            )

    def _sweep_targets(self) -> None:
        """Schedule an eval task for every target entity (re-arm sweep)."""
        for entity_id in self._config.target_entities:
            current = self._hass.states.get(entity_id)
            self._schedule_eval_task(entity_id, current)

    @callback
    def _handle_midnight(self, _now: datetime) -> None:
        """Zero today counter at HA-local midnight."""
        self._state.enforcement_count_today = 0
        self._state.skip_count_today = 0
        self._state.today_reset_date = dt_util.now().date()
        self._persist()
        # _broadcast_status() first so counter sensors refresh unconditionally,
        # then _apply_idle_status() re-derives correct status (handles disabled/master-off).
        self._broadcast_status()
        self._apply_idle_status()

    @callback
    def _handle_startup_grace_done(self, _now: datetime) -> None:
        """After grace, set correct status and evaluate every target once."""
        _LOGGER.debug(
            "Startup grace done for rule=%s; sweeping %d targets",
            self._config.name,
            len(self._config.target_entities),
        )
        self._startup_complete = True

        # Set status synchronously before sweep tasks run so the UI is never stuck
        # on STATUS_STARTING after grace.
        self._apply_idle_status()

        if not self._state.enabled or not self._master_enabled_getter():
            return
        self._sweep_targets()

    def _maybe_reset_today_counter(self) -> None:
        """Reset today counter if persisted reset date is stale."""
        today = dt_util.now().date()
        if self._state.today_reset_date != today:
            self._state.enforcement_count_today = 0
            self._state.skip_count_today = 0
            self._state.today_reset_date = today

    def _maybe_backfill_counter_since(self) -> None:
        """Populate counter_total_since on first load / upgrade.

        Priority: ConfigEntry.created_at (rule creation) → dt_util.now() (fresh
        install without created_at). Only runs when the persisted value is None,
        so a genuine reset via async_clear_history is never overwritten.
        """
        if self._state.counter_total_since is not None:
            return
        created = getattr(self._entry, "created_at", None) if self._entry else None
        self._state.counter_total_since = created or dt_util.now()
        self._persist()

    async def async_evaluate(
        self,
        entity_id: str,
        new_state: Any,
        trigger_context_id: str | None = None,
    ) -> None:
        """Evaluate a state change against the rule and enforce if appropriate."""
        now = dt_util.now()
        self._prune_expired_cooldowns(now)

        # A dynamic-mode source change reaches the SUPPRESSED (L362) and
        # CONDITIONAL (L408) dedupe guards and moves the card-visible
        # follow_desired_on / attribute_desired_value sensor attrs while the
        # status may not change. Hoisted once here, reused by both guards and the
        # suppression-expiry lazy-sweep skip below.
        is_source = self._is_dynamic and entity_id == self._config.source_entity

        if not self._state.enabled or not self._master_enabled_getter():
            # Rule inactive, but this event came from a watched entity (the
            # listener only subscribes watched ones). The card reads live flag
            # values from the status sensor's extra_state_attributes, which only
            # refresh on a state write. _set_status dedupes when the status stays
            # DISABLED/MASTER_DISABLED, so force a broadcast on that dedupe to
            # keep the card reactive. Same stale-attr guard as the CONDITIONAL
            # (L398) and ARMED (L415) flag-change paths.
            self._apply_idle_status(broadcast_on_dedupe=True)
            return

        if not self._startup_complete and entity_id in self._config.target_entities:
            # During grace, ignore target state events; flag changes still re-evaluate.
            if entity_id not in self._flag_entity_ids:
                return
        if (
            not self._startup_complete
            and self._is_dynamic
            and entity_id == self._config.source_entity
        ):
            # During grace, ignore source changes too (sweep happens post-grace).
            # ponytail: the card's follow_desired_on can read stale here, but it
            # self-heals at the grace-end sweep (a brief startup-only window), so
            # no forced broadcast — not worth a signal per source tick during grace.
            return

        if self._state.suppressed_until and self._state.suppressed_until > now:
            prev_status = self._current_status
            self._set_status(STATUS_SUPPRESSED)
            # Same stale-attr guard as the CONDITIONAL and ARMED paths: a watched
            # flag entity — OR a dynamic-mode source — can change while the rule
            # stays SUPPRESSED, leaving the card's flags[].current or
            # follow_desired_on / attribute_desired_value frozen. Force a refresh
            # when the status didn't move and the trigger was a flag or the source.
            if (entity_id in self._flag_entity_ids or is_source) and (
                prev_status == self._current_status
            ):
                self._broadcast_status()
            # A target can recover to its desired state WHILE the rule is
            # suppressed (e.g. loop protection paused it, then the device came
            # back correct on its own). Clear its stale non-ok outcome so
            # "Targets needing attention" drops it — but do NOT touch suppression:
            # the rule stays SUPPRESSED and suppressed_until is unchanged. This is
            # the suppressed-path analogue of the not-triggered recovery clear.
            if entity_id in self._config.target_entities and not is_source:
                before = self._state.target_outcomes.get(entity_id)
                triggered, _skip = self._trigger_decision(entity_id, new_state)
                if not triggered:
                    self._clear_target_outcome_if_live(entity_id, new_state, now)
                    if self._state.target_outcomes.get(entity_id) is not before:
                        self._broadcast_status()
            return
        if self._state.suppressed_until and self._state.suppressed_until <= now:
            # Lazy suppression expiry (the timer may not have drained yet). Clearing
            # here cancels the pending expiry timer, so _handle_suppression_expired's
            # all-targets re-arm sweep will NOT fire. This event only re-evaluates
            # `entity_id` (fall-through below); sweep the SIBLING targets now so a
            # still-non-compliant target that emits no event of its own is not left
            # unguarded.
            #
            # BUT only for a PLAIN-TARGET trigger. A flag or dynamic-source trigger
            # has a dedicated branch below that already sweeps ALL targets — running
            # the lazy sibling sweep too would double-schedule them. The per-entity
            # cancel in _schedule_eval_task does NOT save us at delay 0: the first
            # eval task runs to completion (synchronous enforce) before the second
            # schedule, so the cancel is a no-op and both enforce. Skip the lazy
            # sweep for flag/source triggers; their own branch owns the sweep.
            self._clear_suppression_state()
            is_flag_trigger = entity_id in self._flag_entity_ids
            if (
                self._state.enabled
                and self._startup_complete
                and self._master_enabled_getter()
                and not is_flag_trigger
                and not is_source
            ):
                for target in self._config.target_entities:
                    if target == entity_id:
                        continue
                    current = self._hass.states.get(target)
                    # Sibling sweep: pass context_id=None so a sibling target's
                    # enforce/logbook event is not mis-attributed to the parent
                    # context of a DIFFERENT entity's triggering event.
                    self._schedule_eval_task(target, current, None)

        if not self._flags_match():
            prev_status = self._current_status
            self._set_status(STATUS_CONDITIONAL)
            # A flag entity OR a dynamic-mode source changed but overall status
            # stayed CONDITIONAL: _set_status dedupes the broadcast, so the status
            # sensor's extra_state_attributes (flags[].current, follow_desired_on,
            # attribute_desired_value — all card-visible) never reach the UI. Force
            # a refresh so the frontend sees the fresh values.
            if (entity_id in self._flag_entity_ids or is_source) and (
                prev_status == self._current_status
            ):
                self._broadcast_status()
            self._cancel_pending_for_entities(self._config.target_entities)
            return

        flag_entity_ids = self._flag_entity_ids

        # Flag entity change: flags just became satisfied — sweep all targets.
        if entity_id in flag_entity_ids:
            prev_status = self._current_status
            self._set_status(self._derive_armed_or_cooldown(now))
            # Same stale-attr guard as the CONDITIONAL path above: if the overall
            # status didn't move (e.g. already ARMED, non-critical flag flipped),
            # the flag row on the card would otherwise show stale values.
            if prev_status == self._current_status:
                self._broadcast_status()
            for target in self._config.target_entities:
                if target == entity_id:
                    continue  # handled below as a target if also a target entity
                current = self._hass.states.get(target)
                if current is not None and self._is_triggered(target, current):
                    self._schedule_eval_task(target, current, trigger_context_id)
            # If entity is flag-only, stop here; if also a target, fall through.
            if entity_id not in self._config.target_entities:
                return

        # Dynamic modes: a source change drives a sweep of all targets. The source
        # is never itself enforced (config flow forbids source in targets; the
        # dynamic trigger decision guards defensively). Mirrors the flag-change sweep.
        if self._is_dynamic and entity_id == self._config.source_entity:
            prev_status = self._current_status
            self._set_status(self._derive_armed_or_cooldown(now))
            # A source change moves follow_desired_on (exposed on the status sensor
            # for the card). If the overall status didn't move, _set_status dedupes
            # the broadcast and the stale desired value would stick — force a refresh,
            # same guard as the flag-change sweep above.
            if prev_status == self._current_status:
                self._broadcast_status()
            for target in self._config.target_entities:
                current = self._hass.states.get(target)
                self._schedule_eval_task(target, current, trigger_context_id)
            return

        triggered, skip_reason = self._trigger_decision(entity_id, new_state)

        if skip_reason is not None:
            self._cancel_pending(entity_id)
            self._fire_skipped(entity_id, skip_reason)
            self._set_or_force_broadcast(self._derive_armed_or_cooldown(now))
            return

        if not triggered:
            # State no longer matches; cancel any pending delayed enforcement.
            self._cancel_pending(entity_id)
            # Recovery: a target that is LIVE and no longer triggers the rule is
            # compliant (it reached the desired state), so clear a stale non-ok
            # outcome — otherwise a target that failed while offline and then came
            # back already-correct stays in "Targets needing attention" forever,
            # because no enforce fires to overwrite the latched failure. Guarded to
            # a live state (new_state present and not unavailable/unknown) so an
            # offline target is NOT falsely marked healthy, and only written when
            # an outcome actually needs clearing (avoid broadcast churn).
            self._clear_target_outcome_if_live(entity_id, new_state, now)
            self._set_or_force_broadcast(self._derive_armed_or_cooldown(now))
            return

        if self._config.debounce_enabled and self._in_cooldown(entity_id, now):
            self._set_status(STATUS_COOLDOWN)
            return

        # Per-direction delay (dynamic_state): the wait before enforcing may differ
        # for driving the target ON vs OFF. effective_delay() resolves this once —
        # dynamic_state picks delay_on/off by the desired direction, every other
        # mode (and source-offline, desired_on=None) returns delay_seconds. Resolve
        # once and reuse for BOTH the gate below and the scheduled duration, so a
        # direction with delay 0 goes immediate while the other still waits.
        #
        # The resolved delay is PINNED to the arming direction for the life of the
        # countdown — it is not re-resolved if the source flips mid-wait. That is
        # safe: _fire re-runs _trigger_decision against live state, so a flip that
        # makes the target compliant cancels cleanly, and a genuinely new direction
        # re-enters async_evaluate (fresh effective_delay). See the pin test in
        # tests/test_split_delay_engine.py.
        delay = self._config.effective_delay(self._follow_desired_on())
        if delay > 0:
            # Order is load-bearing: schedule first (adds to _pending_enforcement_due),
            # then broadcast so the sensor receives the updated dict.
            self._schedule_delayed_enforcement(entity_id, delay, trigger_context_id)
            self._set_or_force_broadcast(STATUS_PENDING)
            return

        await self._enforce(entity_id, trigger_context_id=trigger_context_id)

    def _flags_match(self) -> bool:
        """Return True only if every configured flag entity matches its target state."""
        if not self._config.flags:
            return True
        for flag in self._config.flags:
            state_obj = self._hass.states.get(flag.entity)
            if state_obj is None:
                return False
            if state_obj.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                return False
            if state_obj.state != flag.match_state:
                return False
        return True

    def _is_triggered(self, entity_id: str, new_state: Any) -> bool:
        """Return True if the rule's trigger condition holds for entity_id's state."""
        triggered, _skip_reason = self._trigger_decision(entity_id, new_state)
        return triggered

    def _clear_target_outcome_if_live(
        self, entity_id: str, new_state: Any, now: datetime
    ) -> None:
        """Clear a stale non-ok target outcome once the target is live + compliant.

        Called from the not-triggered branch: a target that no longer triggers the
        rule and is reachable has reached its desired state on its own (e.g. it
        recovered from the offline failure that latched target_outcomes[...] =
        failed). Overwrite that to ok so "Targets needing attention" drops it.
        Skips offline targets (None / unavailable / unknown) and no-ops when there
        is nothing non-ok to clear, so it never writes/broadcasts needlessly.
        """
        if new_state is None or getattr(new_state, "state", None) in (
            STATE_UNAVAILABLE,
            STATE_UNKNOWN,
        ):
            return
        prev = self._state.target_outcomes.get(entity_id)
        if prev is None or prev.get("outcome") == "ok":
            return
        self._state.target_outcomes[entity_id] = {
            "outcome": "ok",
            "reason": None,
            "error": None,
            "at": now,
        }
        self._persist()

    def _trigger_decision(
        self, entity_id: str, new_state: Any
    ) -> tuple[bool, str | None]:
        """Return trigger result and optional skip reason.

        In dynamic modes entity_id is a TARGET; the decision reads the SOURCE's
        current value and returns triggered when the target differs from it.
        """
        if self._config.mode == MODE_DYNAMIC_STATE:
            return self._dynamic_state_trigger_decision(entity_id, new_state)
        if self._config.mode == MODE_DYNAMIC_ATTRIBUTE:
            return self._dynamic_attribute_trigger_decision(entity_id, new_state)

        if new_state is None:
            return False, None

        if self._config.mode == MODE_FIXED_STATE:
            if new_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                return False, None
            return new_state.state in self._config.trigger_states, None

        if self._config.mode == MODE_FIXED_ATTRIBUTE:
            attr = self._config.attribute
            if attr in COLOR_ATTRIBUTES:
                return self._color_trigger_decision(new_state)
            op = self._config.operator
            threshold = self._config.threshold
            if attr is None or op is None or threshold is None:
                return False, None
            value = (
                new_state.attributes.get(attr)
                if hasattr(new_state, "attributes")
                else None
            )
            try:
                value_f = float(value)
            except (TypeError, ValueError):
                return False, None
            return _compare(value_f, op, threshold), None

        return False, None

    def _follow_desired_on(self) -> bool | None:
        """Compute the desired on/off for a dynamic_state rule from the SOURCE.

        Reads the source's STATE by default, or a named source ATTRIBUTE when
        source_value_from == attribute (the common source picker shared with
        dynamic_attribute). The read value is reduced to on/off via the on-ish
        rule (_value_is_on): a state string in FOLLOW_ON_STATES, or a truthy
        attribute value (non-zero number, on-ish string, non-empty), is ON.
        follow_invert flips it. Returns None when the source is absent/offline or
        the chosen attribute is missing — caller treats None as not-triggered.
        """
        source_id = self._config.source_entity
        if not source_id:
            return None
        src = self._hass.states.get(source_id)
        if src is None:
            return None
        if src.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return None
        if self._config.source_value_from == SOURCE_VALUE_FROM_ATTRIBUTE:
            name = self._config.source_attribute
            if not name or not hasattr(src, "attributes"):
                return None
            value = src.attributes.get(name)
            if value is None:
                return None
            on = _value_is_on(value)
        else:
            on = src.state in FOLLOW_ON_STATES
        return (not on) if self._config.follow_invert else on

    def _dynamic_state_trigger_decision(
        self, entity_id: str, new_state: Any
    ) -> tuple[bool, str | None]:
        """dynamic_state comparator for a single TARGET entity.

        Enforce only when the target's current on/off differs from the desired
        derived from the live source. Pure state — no attribute (dynamic_attribute
        is a separate mode). Primary guard against a service call per source tick.
        """
        # Defensive: the config flow forbids source ∈ targets, but never silently
        # enforce the source itself.
        if entity_id == self._config.source_entity:
            return False, "source_is_target"

        desired_on = self._follow_desired_on()
        if desired_on is None:
            # Source absent/offline — not-triggered (matches existing offline path).
            return False, None

        if new_state is None or new_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            # Target offline/unknown — can't compare; leave it alone.
            return False, None

        cur_on = new_state.state in FOLLOW_ON_STATES
        return (cur_on != desired_on), None

    def _dynamic_source_value(self) -> Any | None:
        """Return the live value driving a dynamic_attribute rule's target attr.

        Read from the source entity's STATE or a named ATTRIBUTE per config. None
        when the source is absent/offline or the named attribute is missing — the
        caller treats None as not-triggered (never writes None to the target),
        reusing the fixed_attribute missing-value behavior. No type coercion (v1
        raw copy); a type-mismatched value degrades at the guarded service call.
        """
        source_id = self._config.source_entity
        if not source_id:
            return None
        src = self._hass.states.get(source_id)
        if src is None:
            return None
        if src.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return None
        if self._config.source_value_from == SOURCE_VALUE_FROM_ATTRIBUTE:
            name = self._config.source_attribute
            if not name or not hasattr(src, "attributes"):
                return None
            return src.attributes.get(name)
        # Default: source state.
        return src.state

    def _dynamic_attribute_trigger_decision(
        self, entity_id: str, new_state: Any
    ) -> tuple[bool, str | None]:
        """dynamic_attribute comparator for a single TARGET entity.

        Same shape as fixed_attribute enforcement, but the desired value is read
        live from the source. Enforce only when the target's current value for the
        bound attribute differs from the source value (color within tolerance,
        numeric/scalar exact). Source or target offline / missing value →
        not-triggered.
        """
        if entity_id == self._config.source_entity:
            return False, "source_is_target"

        value = self._dynamic_source_value()
        if value is None:
            return False, None

        attr = self._config.attribute
        if attr is None:
            return False, None
        if new_state is None or new_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return False, None

        tgt_val = (
            new_state.attributes.get(attr) if hasattr(new_state, "attributes") else None
        )
        if self._attr_values_match(attr, value, tgt_val):
            return False, None
        return True, None

    def _attr_values_match(self, attr: str, desired: Any, current: Any) -> bool:
        """Return True when current equals desired for attribute attr.

        Color attrs use the engine's existing tolerances; everything else compares
        for equality after a best-effort numeric coercion so "50" (string state)
        and 50 (int attr) are treated as equal and don't cause a re-enforce loop.
        """
        if attr == ATTR_RGB_COLOR:
            d = _normalize_rgb_color(desired)
            c = _normalize_rgb_color(current)
            if d is None or c is None:
                return False
            return _rgb_matches(c, d)
        if attr == ATTR_COLOR_TEMP_KELVIN:
            d = _normalize_kelvin(desired)
            c = _normalize_kelvin(current)
            if d is None or c is None:
                return False
            return abs(c - d) <= COLOR_TEMP_KELVIN_TOLERANCE
        # Numeric best-effort: compare as floats when both coerce, else as strings.
        try:
            return float(desired) == float(current)
        except (TypeError, ValueError):
            return str(desired) == str(current)

    def _color_trigger_decision(self, new_state: Any) -> tuple[bool, str | None]:
        """Return trigger result and skip reason for color-match attributes."""
        attr = self._config.attribute
        if attr is None:
            return False, None
        if new_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return False, "light_unavailable"
        if new_state.state != "on":
            return False, "light_off"

        attributes = new_state.attributes if hasattr(new_state, "attributes") else {}
        if attr == ATTR_RGB_COLOR:
            target = _normalize_rgb_color(self._config.target_value)
            if target is None:
                return False, None
            current = _normalize_rgb_color(attributes.get(attr))
            if current is None:
                return False, "attribute_unavailable"
            return not _rgb_matches(current, target), None

        if attr == ATTR_COLOR_TEMP_KELVIN:
            target = _normalize_kelvin(self._config.target_value)
            if target is None:
                return False, None
            current = _normalize_kelvin(attributes.get(attr))
            if current is None:
                return False, "attribute_unavailable"
            return abs(current - target) > COLOR_TEMP_KELVIN_TOLERANCE, None

        return False, None

    def _in_cooldown(self, entity_id: str, now: datetime) -> bool:
        """Return True if the entity is still inside its debounce cooldown."""
        end = self._state.cooldowns.get(entity_id)
        return end is not None and end > now

    def _derive_armed_or_cooldown(self, now: datetime) -> str:
        """Pick between PENDING, VERIFYING, COOLDOWN, and ARMED based on state."""
        if self._pending_enforcements:
            return STATUS_PENDING
        # A verify re-read is armed (post-successful-enforce, within its window):
        # the enforce call returned OK but we have not yet confirmed the target
        # actually moved. Surface VERIFYING so the card does not flip to ARMED
        # (falsely "all good") while the re-check is still outstanding.
        if self._pending_verifications:
            return STATUS_VERIFYING
        if any(end > now for end in self._state.cooldowns.values()):
            return STATUS_COOLDOWN
        return STATUS_ARMED

    def _set_or_force_broadcast(self, new_status: str) -> None:
        """Set status or force a broadcast when status is already new_status.

        When new_status is STATUS_PENDING and the rule is already PENDING,
        _set_status would deduplicate the broadcast. Force it so the sensor
        receives the updated _pending_enforcement_due dict (an entity was just
        added or removed from it).
        """
        if new_status == STATUS_PENDING and self._current_status == STATUS_PENDING:
            self._broadcast_status()
        else:
            self._set_status(new_status)

    def _prune_expired_cooldowns(self, now: datetime) -> None:
        """Remove expired per-entity cooldown entries."""
        expired = [eid for eid, end in self._state.cooldowns.items() if end <= now]
        for eid in expired:
            del self._state.cooldowns[eid]

    def _schedule_delayed_enforcement(
        self,
        entity_id: str,
        delay_seconds: int | None = None,
        trigger_context_id: str | None = None,
    ) -> None:
        """Arm a delayed enforcement after `delay_seconds`; cancels any prior
        pending one for this entity. The caller resolves the direction-specific
        delay (effective_delay) and passes it in. When omitted, falls back to the
        rule's shared delay_seconds (the pre-split default)."""
        if delay_seconds is None:
            delay_seconds = self._config.delay_seconds
        self._cancel_pending(entity_id)

        async def _fire(_now: datetime) -> None:
            if self._is_unloaded:
                return
            if not self._state.enabled or not self._master_enabled_getter():
                self._apply_idle_status()
                return
            # Defense-in-depth: never enforce while suppressed. async_suppress /
            # loop-protection cancel pending timers, but guard here too for the
            # race where suppression lands in the same tick this timer fires.
            if (
                self._state.suppressed_until
                and self._state.suppressed_until > dt_util.now()
            ):
                if self._pending_enforcements.get(entity_id) is my_cancel:
                    self._pending_enforcements.pop(entity_id, None)
                    self._pending_enforcement_due.pop(entity_id, None)
                    self._apply_idle_status()
                return
            current = self._hass.states.get(entity_id)
            triggered, skip_reason = self._trigger_decision(entity_id, current)
            if skip_reason is not None:
                owned = self._pending_enforcements.get(entity_id) is my_cancel
                if owned:
                    self._pending_enforcements.pop(entity_id, None)
                    self._pending_enforcement_due.pop(entity_id, None)
                # _fire_skipped FIRST so it increments skip_count before the
                # broadcast reads it — else the skip sensors show the pre-increment
                # value (and a no-op broadcast on an already-ARMED rule would leave
                # them stale indefinitely). Matches the direct async_evaluate path.
                self._fire_skipped(entity_id, skip_reason)
                if owned:
                    self._set_or_force_broadcast(self._derive_armed_or_cooldown(_now))
                return
            if not triggered:
                # _trigger_decision returns (False, None) when current is None, so
                # `not triggered` already covers the state-gone case.
                if self._pending_enforcements.get(entity_id) is my_cancel:
                    self._pending_enforcements.pop(entity_id, None)
                    self._pending_enforcement_due.pop(entity_id, None)
                    self._set_or_force_broadcast(self._derive_armed_or_cooldown(_now))
                return
            if not self._flags_match():
                if self._pending_enforcements.get(entity_id) is my_cancel:
                    self._pending_enforcements.pop(entity_id, None)
                    self._pending_enforcement_due.pop(entity_id, None)
                    self._set_or_force_broadcast(self._derive_armed_or_cooldown(_now))
                return
            try:
                await self._enforce(entity_id, trigger_context_id=trigger_context_id)
            finally:
                if self._pending_enforcements.get(entity_id) is my_cancel:
                    self._pending_enforcements.pop(entity_id, None)
                    self._pending_enforcement_due.pop(entity_id, None)
                    # _enforce already called _set_status(post_status), but the
                    # entity was still in _pending_enforcements at that point so
                    # _derive_armed_or_cooldown returned PENDING and the set was a
                    # no-op. Re-DERIVE now that the dict is clean — a bare re-emit
                    # would replay the stale PENDING and the card freezes there
                    # (clear-suppression stuck-PENDING bug). broadcast_on_dedupe so
                    # a still-pending SIBLING (derived status stays PENDING) still
                    # force-broadcasts — else this entity's popped past-due row
                    # lingers on the card until an unrelated broadcast. Matches the
                    # sibling branches above (_set_or_force_broadcast).
                    self._apply_idle_status(_now, broadcast_on_dedupe=True)

        @callback
        def _fire_cb(now: datetime) -> None:
            # async_call_later expects a sync callback; bridge to the coroutine.
            self._hass.async_create_task(_fire(now))

        cancel = async_call_later(self._hass, delay_seconds, _fire_cb)
        my_cancel = cancel
        self._pending_enforcements[entity_id] = cancel
        self._pending_enforcement_due[entity_id] = dt_util.now() + timedelta(
            seconds=delay_seconds
        )

    def _cancel_pending(self, entity_id: str) -> None:
        """Cancel any pending delayed enforcement for entity_id."""
        cancel = self._pending_enforcements.pop(entity_id, None)
        self._pending_enforcement_due.pop(entity_id, None)
        if cancel is not None:
            try:
                cancel()
            except Exception:
                _LOGGER.debug("Pending cancel failed for %s", entity_id, exc_info=True)

    def _cancel_pending_for_entities(self, entity_ids: list[str]) -> None:
        """Cancel pending enforcements for a list of entities."""
        for eid in entity_ids:
            self._cancel_pending(eid)

    # ----------------------------------------------------------- verify-after-enforce
    def _cancel_verify(self, entity_id: str) -> None:
        """Cancel any pending verify timer for entity_id (dedupe + teardown helper).

        Clears the forced-origin bit too: every event-path resolution routes its
        teardown through here (away-move/offline cancel, reached-confirm,
        reached-but-dropped, fast-confirm, set_enabled drop), so discarding from
        _forced_verifications HERE keeps the R6 sibling-drop lockstep by
        construction — no event-path caller can forget it. The deadline _fire
        path pops _pending_verifications directly (identity guard) and so still
        discards forced explicitly in each of its own branches.
        """
        cancel = self._pending_verifications.pop(entity_id, None)
        self._forced_verifications.discard(entity_id)
        if cancel is not None:
            try:
                cancel()
            except Exception:
                _LOGGER.debug("Verify cancel failed for %s", entity_id, exc_info=True)

    def _verify_reached(self, entity_id: str) -> bool:
        """True when entity_id's live state now matches the rule's desired state.

        Reuses the trigger decision: a state rule is 'triggered' only while the
        target still violates desired, so reached == not triggered. A missing/
        offline target (trigger_decision returns False) reads as 'reached' here —
        we can't correct an offline target and must not loop on it; the reactive
        listener handles it when it comes back.
        """
        current = self._hass.states.get(entity_id)
        triggered, _skip = self._trigger_decision(entity_id, current)
        return not triggered

    def _dead_group_members(self, entity_id: str) -> list[str]:
        """Friendly-names of a light group's unavailable/unknown members.

        Empty list when entity_id is not a group (plain entity has no member
        `entity_id` attribute) or every member is live. Used only to annotate a
        group enforce that raised because one dead member's integration threw
        while the group aggregate already read the desired state — the card
        shows which members are the problem.
        """
        gstate = self._hass.states.get(entity_id)
        if gstate is None:
            return []
        members = gstate.attributes.get("entity_id")
        if not isinstance(members, (list, tuple)):
            return []  # plain entity, not a group
        dead: list[str] = []
        for member in members:
            mstate = self._hass.states.get(member)
            if mstate is None or mstate.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                name = (
                    mstate.attributes.get("friendly_name")
                    if mstate is not None
                    else None
                )
                dead.append(name or member)
        return dead

    def _confirm_verified(self, entity_id: str, forced: bool = False) -> None:
        """Resolve a reached-desired verify cycle: fire EVENT_VERIFIED, go idle.

        Shared by the event-driven happy-path confirm (_handle_state_event, the
        instant the target's post-enforce ACK reaches desired) and the deadline
        timer's reached branch (_fire, the fallback for a device whose ACK the
        listener computed as not-reached but the re-read now sees compliant).

        CONTRACT: the caller MUST have already removed this entity's handle from
        _pending_verifications (the timer pops it via the identity guard; the
        event site via _cancel_verify) so the _apply_idle_status below sees an
        empty pending map and leaves VERIFYING for the real idle status.

        `forced` is passed by the caller because the same _cancel_verify that pops
        the pending handle also discards _forced_verifications, so membership is
        already gone by the time we fire — the caller captures it first.

        attempts = corrective re-enforces done + 1 (the original enforce). First
        clean read (no corrective) -> 1; after N correctives -> N+1.
        """
        attempts = self._verify_attempts.get(entity_id, 0) + 1
        self._verify_attempts.pop(entity_id, None)
        # Confirmed success — the one clean signal automations gate on. verify-on
        # by construction, success-only (reached desired), one pulse per cycle.
        # Arms the recently_enforced_verified sensor too.
        self._fire_verified(entity_id, attempts, forced)
        # Verify cycle closed and target compliant: re-derive so the card leaves
        # VERIFYING for the real idle status (ARMED / COOLDOWN).
        self._apply_idle_status()

    def _verify_drop_reason(self, entity_id: str) -> str | None:
        """Return WHY a pending verify must be DROPPED mid-window, else None.

        Single source of truth for the event-confirm drop decision. Mirrors
        _fire's disabled/master-off, suppressed, AND flags-unmet early-returns so
        the event path and the deadline timer stay in lockstep. Flags are read
        HERE (event time, when the ACK lands), matching _fire's read at deadline
        time: both paths decide on the flag state at the instant they resolve,
        not at arm time, so a flag that flips mid-window drops the cycle
        identically whichever path confirms first. The reason string feeds
        _fire_cancelled; "unloaded" is returned for the drop decision but the
        caller does NOT fire a cancelled signal for it (the entry is tearing down).
        """
        if self._is_unloaded:
            return "unloaded"
        if not self._state.enabled:
            return "disabled"
        if not self._master_enabled_getter():
            return "master_off"
        # MANUAL force bypasses suppressed + flags_unmet: a forced cycle judges the
        # DEVICE only (see VERIFY-OUTCOME CONTRACT above _arm_verify). Twin of the
        # same bypass in _fire — both paths must honor forced identically.
        forced = entity_id in self._forced_verifications
        if (
            not forced
            and self._state.suppressed_until
            and self._state.suppressed_until > dt_util.now()
        ):
            return "suppressed"
        # Flags unmet → rule no longer allowed to act; drop without a verified
        # pulse. Twin of _fire's flags-unmet drop. Without this the event path
        # fires EVENT_VERIFIED for a cycle the deadline path abandons — a verified
        # signal that flickers on device-ACK timing alone.
        if not forced and not self._flags_match():
            return "flags_unmet"
        return None

    def _verify_event_dropped(self, entity_id: str) -> bool:
        """True when a pending verify must be DROPPED (not confirmed) mid-window.

        Thin boolean wrapper over _verify_drop_reason for the confirm-gate check.
        """
        return self._verify_drop_reason(entity_id) is not None

    # ── VERIFY-OUTCOME CONTRACT (do not drift) ────────────────────────────────
    # A verify cycle arms after a successful enforce and resolves to exactly one of:
    #   VERIFIED    device reached desired (state-based; don't care who moved it — Ex4a)
    #   UNREACHABLE device silent through VERIFY_MAX_ATTEMPTS correctives (attempt-based)
    #   CANCELLED   cycle abandoned mid-window, no verdict, reason ∈
    #               {disabled, master_off, suppressed, flags_unmet} (unloaded drops silently)
    #
    # MANUAL force (async_test_enforce) → FULL verify ALWAYS (Ex1-3). The user overrode
    #   conditions on purpose, so a forced cycle judges the DEVICE only: it BYPASSES the
    #   suppressed and flags_unmet drops. It still honors unloaded (teardown) and
    #   disabled/master_off (the rule/hub is genuinely off — "forced" can't re-enable it).
    #
    # AUTOMATIC cycle (flags met → enforce) keeps all four drops. flags/suppression
    # are read at RESOLVE time (whichever path confirms first), not at arm time:
    #   auto + flags lapse mid-window (REACHED or not)  → CANCELLED(flags_unmet)
    #   auto + suppressed mid-window                    → CANCELLED(suppressed)
    #   auto + disabled/master_off mid-window           → CANCELLED(reason)
    # The rule is no longer allowed to act the instant its flags/suppression lapse, so
    # a target that reached desired is NOT confirmed VERIFIED — "only when flags match"
    # outranks "device happens to be right". (Ex4a: VERIFIED requires who-cares-who-
    # moved-it AND the rule still permitted to act at resolve time.)
    #
    # The `forced` bool threads the manual case through the THREE judgement paths that
    # must stay in lockstep: the deadline `_fire` closure here, the event path via
    # `_verify_drop_reason`, and the synchronous fast-confirm in `_enforce`'s tail
    # (which also routes its in-tick confirm through `_verify_drop_reason` — flags can
    # lapse during the shielded service await, so fast-confirm must gate identically or
    # the outcome flips VERIFIED↔CANCELLED on ACK-timing luck). It is stored in
    # `_forced_verifications` for the duration of the cycle so the event path (which has
    # no call frame) can read it.
    # ──────────────────────────────────────────────────────────────────────────
    def _arm_verify(
        self,
        entity_id: str,
        trigger_context_id: str | None = None,
        forced: bool = False,
    ) -> None:
        """Arm a one-shot verify re-read VERIFY_DELAY_SECONDS after a success.

        forced=True (manual test_enforce) makes this cycle bypass the suppressed
        and flags_unmet drops — see the VERIFY-OUTCOME CONTRACT above.
        """
        self._cancel_verify(entity_id)
        if forced:
            self._forced_verifications.add(entity_id)
        else:
            self._forced_verifications.discard(entity_id)

        async def _fire(_now: datetime) -> None:
            if self._is_unloaded:
                return
            # Only act on OUR OWN handle (identity guard, like _schedule_delayed).
            # If the handle in the map is not ours — the event-driven happy-path
            # confirm already cancelled+confirmed this cycle (popping the map), or a
            # later cycle re-armed over us — this fire is STALE: return without
            # re-confirming (else a cancelled deadline that still fires would emit a
            # second EVENT_VERIFIED / run a stray corrective). A re-armed newer cycle
            # owns the live handle and will confirm on its own fire.
            if self._pending_verifications.get(entity_id) is not my_cancel:
                return
            self._pending_verifications.pop(entity_id, None)
            # Read forced from the set (not the closure) so it stays in lockstep with
            # _verify_drop_reason, and so a forced cycle's engine correctives — which
            # re-arm preserving membership — keep bypassing the suppressed/flags_unmet
            # drops until the cycle resolves VERIFIED or UNREACHABLE.
            is_forced = entity_id in self._forced_verifications
            if not self._state.enabled or not self._master_enabled_getter():
                # Rule went inactive during the verify window; our handle was
                # cleared by the identity-guarded pop above. Re-derive so VERIFYING
                # resolves to the real idle status (DISABLED / MASTER_DISABLED)
                # instead of staying stuck on the card, mirroring the suppressed
                # drop below. Fire the abandoned-cycle signal (no verdict reached).
                # NB: disabled/master_off drop even when forced — a forced cycle
                # judges the DEVICE but cannot re-enable a rule the user switched off.
                reason = "disabled" if not self._state.enabled else "master_off"
                self._verify_attempts.pop(entity_id, None)
                self._forced_verifications.discard(entity_id)
                self._fire_cancelled(entity_id, reason, is_forced)
                self._apply_idle_status()
                return
            # Suppressed → do not correct; drop the cycle (a real event re-arms it).
            # Bypassed when forced (manual force judges the device — see CONTRACT).
            # ponytail: no _forced_verifications.discard here — the `not is_forced`
            # guard means a forced entry can never reach this branch (nothing to
            # discard). If the bypass is ever narrowed, add the discard then.
            if (
                not is_forced
                and self._state.suppressed_until
                and self._state.suppressed_until > dt_util.now()
            ):
                self._verify_attempts.pop(entity_id, None)
                # Our handle was cleared by the identity-guarded pop above; re-derive
                # so VERIFYING resolves to the real idle status (SUPPRESSED here).
                self._fire_cancelled(entity_id, "suppressed", is_forced)
                self._apply_idle_status()
                return
            # Conditional flags went unmet during the verify window → the rule is no
            # longer allowed to act. Drop the cycle without correcting; a later
            # flag-met state event re-enters via the normal enforce path and re-arms
            # verify. Without this, a corrective re-enforce would fire while the rule
            # shows CONDITIONAL (flags unmet) — acting when the user said "only when
            # flags match". Mirrors the suppressed-drop above.
            # Bypassed when forced (manual force judges the device — see CONTRACT).
            if not is_forced and not self._flags_match():
                self._verify_attempts.pop(entity_id, None)
                self._fire_cancelled(entity_id, "flags_unmet", is_forced)
                self._apply_idle_status()
                return
            if self._verify_reached(entity_id):
                # Reached desired — DEFENSIVE deadline-path confirm. On the happy
                # path the event site (_handle_state_event) already confirmed and
                # cancelled this timer, so we rarely arrive here; this remains the
                # fallback for a target whose ACK the listener computed as
                # not-reached but the re-read now sees compliant. Our handle was
                # popped by the identity guard above, so _confirm_verified's
                # _apply_idle_status leaves VERIFYING.
                self._forced_verifications.discard(entity_id)
                self._confirm_verified(entity_id, is_forced)
                return
            attempts = self._verify_attempts.get(entity_id, 0)
            if attempts >= VERIFY_MAX_ATTEMPTS:
                # Exhausted this verify cycle. OVERWRITE the provisional "ok" the
                # corrective enforce wrote with a "failed" outcome (this re-read is
                # the source of truth, not the service call's return), then SELF-HEAL:
                # re-enter the COUNTED enforcing cycle instead of dead-ending, so a
                # target EG is supposed to own is driven to desired without human
                # action. The only terminators are the existing guards.
                self._verify_attempts.pop(entity_id, None)
                # Forced cycle ends here: the self-heal re-enter below is a fresh
                # ENGINE-driven enforce (not the user's manual force), so it must
                # NOT inherit the bypass. Drop membership before re-entering.
                self._forced_verifications.discard(entity_id)
                count = self._consecutive_exhaustions.get(entity_id, 0) + 1
                self._consecutive_exhaustions[entity_id] = count
                self._state.target_outcomes[entity_id] = {
                    "outcome": "failed",
                    "reason": REASON_UNREACHABLE_AFTER_RETRIES,
                    "error": (
                        f"target did not reach desired state after "
                        f"{VERIFY_MAX_ATTEMPTS} corrective attempts"
                    ),
                    "at": dt_util.now(),
                }
                if count == 1:
                    # Fire the skipped EVENT once per fresh exhaustion streak — not
                    # every 5 s cycle (that floods the bus on a dead device). A real
                    # recovery resets the counter (see _handle_state_event), so a
                    # later death re-fires it. NB: do NOT route through _fire_skipped
                    # — its classifier would clobber the "failed" outcome to "skipped".
                    self._hass.bus.async_fire(
                        EVENT_SKIPPED,
                        {
                            "rule_id": self._config.unique_id,
                            "rule_name": self._config.name,
                            "entity_id": entity_id,
                            "reason": REASON_UNREACHABLE_AFTER_RETRIES,
                            "forced": is_forced,
                        },
                    )
                    # Dedicated failure signal (EVENT_UNREACHABLE + its 30s pulse
                    # sensor) — the verify-only twin of _fire_verified. Same count==1
                    # gate so it fires once per streak alongside the skipped event.
                    self._fire_unreachable(entity_id, is_forced)
                if count >= CONSECUTIVE_EXHAUSTION_LIMIT:
                    # Dead-device kill-switch floor: trip loop protection directly.
                    # The direct call bypasses the rate-limit gate (only its caller
                    # at the enforce site is gated on max_enforcements_per_minute),
                    # so a dead target is suppressed even when the rate limit is off.
                    await self._trigger_loop_protection(
                        entity_id, cause="unreachable_loop"
                    )
                    self._apply_idle_status()
                    return
                # Re-enter the enforcing cycle: drop the transient double-arm marker
                # (so the re-entering enforce can re-arm verify) and schedule a COUNTED
                # non-corrective enforce after the rule delay. Counted + non-corrective
                # is load-bearing: it feeds the rate_limit_window so loop protection can
                # trip — the corrective bypass path would be invisible to the guards.
                self._unreachable.discard(entity_id)
                self._schedule_delayed_enforcement(
                    entity_id, trigger_context_id=trigger_context_id
                )
                # Re-derive off VERIFYING; _schedule_delayed_enforcement sets PENDING
                # when it arms, which _apply_idle_status reflects.
                self._apply_idle_status()
                return
            # Not reached, attempts remain: corrective re-enforce (bypass rate limit
            # + debounce/cooldown — it is corrective, not a new trigger), count it,
            # and re-arm one more verify.
            self._verify_attempts[entity_id] = attempts + 1
            await self._enforce(
                entity_id,
                bypass_rate_limit=True,
                trigger_context_id=trigger_context_id,
                is_corrective=True,
            )
            # Re-arm only if still enabled/loaded and not newly latched.
            if (
                not self._is_unloaded
                and self._state.enabled
                and self._master_enabled_getter()
                and entity_id not in self._unreachable
            ):
                self._arm_verify(entity_id, trigger_context_id, forced=is_forced)
                # The corrective _enforce above derived its post-status with no
                # verify pending (our handle was popped at fire time), so it
                # broadcast ARMED. Re-arming repopulated _pending_verifications;
                # re-derive now (same tick, no paint between) so the card shows
                # VERIFYING for the remainder of the retry window, not ARMED.
                self._apply_idle_status()

        @callback
        def _fire_cb(now: datetime) -> None:
            self._hass.async_create_task(_fire(now))

        cancel = async_call_later(self._hass, VERIFY_DELAY_SECONDS, _fire_cb)
        my_cancel = cancel
        self._pending_verifications[entity_id] = cancel

    async def _enforce(
        self,
        entity_id: str,
        *,
        user_id: str | None = None,
        bypass_rate_limit: bool = False,
        trigger_context_id: str | None = None,
        is_corrective: bool = False,
        forced: bool = False,
    ) -> None:
        """Rate-limit, lock, and execute the enforcement service call.

        is_corrective=True marks a verify-driven re-enforce: it must NOT arm a fresh
        verify timer on success (the verify cycle that triggered it owns the retry
        accounting), else each corrective would chain a new verify indefinitely.

        forced=True marks a MANUAL force (test_enforce): the verify cycle it arms
        bypasses the suppressed/flags_unmet drops — see the VERIFY-OUTCOME CONTRACT
        above _arm_verify. Only the user's first manual enforce is forced; the
        engine's correctives are not.
        """
        now = dt_util.now()

        async with self._state.reentrance_lock:
            cutoff = now - timedelta(seconds=60)
            idx = bisect.bisect_left(self._state.rate_limit_window, cutoff)
            if idx:
                del self._state.rate_limit_window[:idx]
            # Limit <= 0 means user disabled the rate limit entirely; skip loop protection.
            if (
                not bypass_rate_limit
                and self._config.max_enforcements_per_minute > 0
                and len(self._state.rate_limit_window)
                >= self._config.max_enforcements_per_minute
            ):
                await self._trigger_loop_protection(entity_id)
                return

            # Capture before _set_status(STATUS_ENFORCING) so the success path can
            # detect ERROR→OK recovery (EG-6).
            was_in_error = self._current_status == STATUS_ERROR
            self._set_status(STATUS_ENFORCING)

            current_state = self._hass.states.get(entity_id)
            if (
                self._config.mode in (MODE_FIXED_ATTRIBUTE, MODE_DYNAMIC_ATTRIBUTE)
                and self._config.attribute in COLOR_ATTRIBUTES
                and current_state is not None
            ):
                _triggered, skip_reason = self._color_trigger_decision(current_state)
                if skip_reason is not None:
                    self._fire_skipped(entity_id, skip_reason, forced=forced)
                    self._set_status(self._derive_armed_or_cooldown(now))
                    return

            # Dynamic modes compute the live target value at enforce time (source
            # may have moved since the decision). None → source offline/missing →
            # skip cleanly (never write None to the target).
            state_override: str | None = None
            value_override: Any | None = None
            if self._config.mode == MODE_DYNAMIC_STATE:
                desired_on = self._follow_desired_on()
                if desired_on is None:
                    self._fire_skipped(entity_id, "source_offline", forced=forced)
                    self._set_status(self._derive_armed_or_cooldown(now))
                    return
                state_override = "on" if desired_on else "off"
            elif self._config.mode == MODE_DYNAMIC_ATTRIBUTE:
                value_override = self._dynamic_source_value()
                if value_override is None:
                    self._fire_skipped(
                        entity_id, "source_value_unavailable", forced=forced
                    )
                    self._set_status(self._derive_armed_or_cooldown(now))
                    return

            service_call = self._resolve_service(
                entity_id, state_override=state_override, value_override=value_override
            )
            if service_call is None:
                self._fire_skipped(entity_id, "no_service_mapping", forced=forced)
                _LOGGER.debug("No service mapping resolved for %s", entity_id)
                # Surface a repair issue. Two honest cards:
                #   Card A (unsupported_domain) — the domain is in NEITHER service
                #     map, so Entity Guard can't drive it at all.
                #   Card B (unsupported_state) — the domain IS mapped, but this
                #     specific state has no mapping / isn't in the device's valid
                #     set (climate→"banana"). Entity Guard controls the domain, just
                #     not this state.
                # Only the state modes can produce a state-level miss; attribute
                # modes resolve via _resolve_attribute_service (dead-defensive) and
                # carry no meaningful state string → always Card A.
                domain = entity_id.split(".", 1)[0]
                if self._config.mode == MODE_FIXED_STATE:
                    bad_state: str | None = self._config.target_state
                elif self._config.mode == MODE_DYNAMIC_STATE:
                    bad_state = state_override
                else:
                    bad_state = None
                if bad_state is not None and (
                    domain in DOMAIN_SERVICE_MAP or domain in DOMAIN_MODE_SERVICE_MAP
                ):
                    async_raise_unsupported_state(self._hass, domain, bad_state)
                else:
                    async_raise_unsupported_domain(self._hass, domain)
                self._set_status(self._derive_armed_or_cooldown(now))
                return

            domain, service, data = service_call
            _LOGGER.info(
                "Enforcing rule '%s' on %s via %s.%s",
                self._config.name,
                entity_id,
                domain,
                service,
            )
            _LOGGER.debug("Service call data: %s", data)
            self._state.last_triggered_user_id = user_id
            self._state.last_triggered_entity = entity_id
            ctx = Context(user_id=user_id, parent_id=trigger_context_id)

            def _record_failure(err_str: str) -> None:
                # Shared failure handling for both a raised service error and a
                # timeout — identical except for the message. Routes through the
                # service_call_failed path so a timeout composes with the per-target
                # "failed" outcome (target_outcomes) exactly like any other failure.
                #
                # GROUP-MEMBER RECLASSIFICATION: a light group's blocking turn-off
                # fans out; a dead member whose integration RAISES (e.g. SwitchBot
                # Cloud offline) propagates one exception out of the group call even
                # though the group AGGREGATE already reached desired (HA any-on: a
                # group of N-off + M-unavailable reads 'off'). That is not a rule
                # failure — the group IS at desired; only a dead member couldn't be
                # commanded. Reclassify as a clean skip so it does NOT climb
                # consecutive_errors into a sticky STATUS_ERROR red banner. Keyed on
                # _verify_reached (group as a whole at desired), NOT the literal
                # 'off' — a single dead light that raised is itself the unreached
                # thing (_verify_reached False), so it still counts as an error.
                dead = self._dead_group_members(entity_id)
                if dead and self._verify_reached(entity_id):
                    self._fire_skipped(
                        entity_id,
                        "member_unavailable",
                        error=err_str,
                        members=dead,
                        forced=forced,
                    )
                    self._set_status(self._derive_armed_or_cooldown(now))
                    return
                self._fire_skipped(
                    entity_id,
                    REASON_SERVICE_CALL_FAILED,
                    error=err_str,
                    forced=forced,
                )
                self._state.consecutive_errors += 1
                self._state.last_error = err_str
                # Reset recovery counter on any failure (EG-6).
                self._state.consecutive_success_count = 0
                self._persist()
                if self._state.consecutive_errors >= ERROR_THRESHOLD:
                    self._set_status(STATUS_ERROR)
                else:
                    self._set_status(self._derive_armed_or_cooldown(now))

            # pending_cancel: the flag-sweep schedules one eval task per target
            # (_schedule_eval_task, cancel-and-replace). A target's OWN post-enforce
            # state_changed re-enters the sweep and cancels THIS still-awaiting task.
            # For a battery/RF switch (SwitchBot etc.) the integration ACK — and the
            # target's state_changed — lands SECONDS after the blocking call starts,
            # so the cancel reliably arrives while we are parked in the await, after
            # the device committed to moving but before the success tail (counters,
            # last_enforced, EVENT_ENFORCED, _persist) runs. We must not lose that
            # bookkeeping. Run the call as a SHIELDED child so the cancel cannot kill
            # the in-flight device move; swallow the CancelledError delivered to our
            # frame, remember it, let the (synchronous) success tail record, then
            # re-raise after _set_status so the task still ends cancelled and the
            # replacement eval task owns the entity going forward.
            call = asyncio.ensure_future(
                asyncio.wait_for(
                    self._hass.services.async_call(
                        domain, service, data, blocking=True, context=ctx
                    ),
                    ENFORCE_TIMEOUT_SECONDS,
                )
            )
            pending_cancel = False
            try:
                # Bound the call: a hung integration would otherwise block here
                # forever while holding reentrance_lock, freezing the rule without
                # ever reaching the error path. On timeout, wait_for cancels the
                # wrapped async_call (delivering CancelledError into it) and releases
                # the lock — our coroutine frame is cleaned up, though a pathological
                # integration ignoring cancellation may still finish its side effect
                # in the background (not a leak this wrapper introduces).
                while True:
                    try:
                        await asyncio.shield(call)
                        break
                    except asyncio.CancelledError:
                        if call.cancelled():
                            # The call itself was cancelled (not us) — real abort.
                            raise
                        # Our frame was cancelled by the per-entity replace. Keep
                        # waiting on the shielded call so the device move + tail
                        # still complete; record the cancel to re-raise at the end.
                        pending_cancel = True
                        continue
            except TimeoutError:
                # 3.11+: TimeoutError is an alias of asyncio.TimeoutError. str() of it
                # is empty, so synthesize a non-empty message for the card/last_error.
                msg = f"service call timed out after {ENFORCE_TIMEOUT_SECONDS}s"
                _LOGGER.warning(
                    "Enforcement timed out for %s on rule '%s' after %ds",
                    entity_id,
                    self._config.name,
                    ENFORCE_TIMEOUT_SECONDS,
                )
                _record_failure(msg)
                return
            except Exception as err:
                # Spec: target unavailable / service errors → skip silently + debug log.
                _LOGGER.warning(
                    "Enforcement failed for %s on rule '%s': %s",
                    entity_id,
                    self._config.name,
                    err,
                )
                _record_failure(str(err))
                return

            # A CORRECTIVE re-enforce (the verify cycle's own retry) is not a
            # user-trigger-driven enforcement and its success is PROVISIONAL —
            # the verify re-read may yet overturn it to failed. So it must not
            # touch the user-visible/loop-protection bookkeeping: counting it
            # would (a) inflate the "Enforcements" sensors, (b) silently eat the
            # rule's loop-protection budget via the rate_limit_window (tripping
            # suppression sooner than the real trigger rate warrants), and (c)
            # falsely clear STATUS_ERROR on a success verify might reject. The
            # verify cycle is bounded by _verify_attempts + the unreachable
            # latch, so skipping this costs no safety. Still UNgated below:
            # EVENT_ENFORCED, _arm_recently_enforced, _persist, and the
            # provisional target_outcomes "ok" (which verify overwrites).
            recovered_from_error = False
            if not is_corrective:
                self._state.enforcement_count_today += 1
                self._state.enforcement_count_total += 1
                self._state.last_enforced = now
                # insort (not append): `now` was captured before the reentrance lock,
                # so two enforces can reach here with out-of-order timestamps. The
                # bisect prune above assumes an ascending window — keep it sorted.
                bisect.insort(self._state.rate_limit_window, now)
                # Track consecutive successes for STATUS_ERROR auto-recovery (EG-6).
                # While still in ERROR, do NOT zero consecutive_errors / last_error — they
                # remain visible on the status sensor until the recovery threshold is met.
                if was_in_error:
                    self._state.consecutive_success_count += 1
                    if (
                        self._state.consecutive_success_count
                        >= ERROR_RECOVERY_SUCCESS_THRESHOLD
                    ):
                        _LOGGER.info(
                            "Rule '%s' auto-recovered from STATUS_ERROR after %d successes",
                            self._config.name,
                            self._state.consecutive_success_count,
                        )
                        self._state.consecutive_success_count = 0
                        self._state.consecutive_errors = 0
                        self._state.last_error = None
                        recovered_from_error = True
                else:
                    self._state.consecutive_success_count = 0
                    self._state.consecutive_errors = 0
                    self._state.last_error = None

                if self._config.debounce_enabled and self._config.debounce_seconds > 0:
                    self._state.cooldowns[entity_id] = now + timedelta(
                        seconds=self._config.debounce_seconds
                    )

            self._hass.bus.async_fire(
                EVENT_ENFORCED,
                {
                    "rule_id": self._config.unique_id,
                    "rule_name": self._config.name,
                    "entity_id": self._get_rule_entity_id() or entity_id,
                    "target_entity_id": entity_id,
                    "domain": entity_id.split(".", 1)[0],
                    "trigger": self._config.mode,
                    "target": self._event_target(
                        follow_direction=state_override, follow_value=value_override
                    ),
                    "reason": "rule_match",
                    "user_id": user_id,
                    "forced": forced,
                },
                context=ctx,
            )

            self._arm_recently_enforced()
            self._persist()
            # Per-target success — overwrites any prior skipped/failed entry so the
            # card row for this target clears once it enforces cleanly again. BUT do
            # not clobber a live "failed/unreachable" outcome mid-self-heal: this
            # enforce is the re-entry of a cycle whose verify has not yet re-read, so
            # the failed signal must stay visible between cycles. It flips back to ok
            # when the verify re-read reaches desired (resets the counter) or a real
            # state_changed recovers the target.
            prior = self._state.target_outcomes.get(entity_id)
            mid_exhaustion = self._consecutive_exhaustions.get(entity_id, 0) > 0
            if not (
                mid_exhaustion
                and prior is not None
                and prior.get("outcome") == "failed"
                and prior.get("reason") == REASON_UNREACHABLE_AFTER_RETRIES
            ):
                self._state.target_outcomes[entity_id] = {
                    "outcome": "ok",
                    "reason": None,
                    "error": None,
                    "at": now,
                }
            # Verify-after-enforce: on a NORMAL successful enforce of a verify-enabled
            # state rule, arm a one-shot re-read. Not for corrective enforces (the
            # verify cycle owns the retry), not for a latched-unreachable target. The
            # "ok" write above is provisional — the verify re-read may overturn it to
            # failed (the silent-ignore case: service returned OK, device never moved).
            if (
                not is_corrective
                and self._config.verify_enabled
                and self._config.mode in (MODE_FIXED_STATE, MODE_DYNAMIC_STATE)
                and self._state.enabled
                and self._master_enabled_getter()
                and entity_id not in self._unreachable
                and entity_id not in self._pending_verifications
            ):
                # Already-pending guard: a verify in flight owns its deadline timer
                # and corrective loop. A re-enforce fired by an intermediate event
                # (e.g. a light GROUP mid fan-out still reads any-on 'on', so the
                # reactive path re-enforces before the group settles) must NOT
                # re-arm over it — _arm_verify cancels the live timer first, so
                # churning re-arms kept resetting the 5s deadline and the confirm
                # survived only by racing the group's final member-off event.
                # ponytail: a genuinely NEW reactive trigger on the same entity
                # mid-window reuses the in-flight verify's attempts/context_id
                # rather than arming a fresh cycle — the pulse still fires, only
                # its attribution is the prior cycle's. Edge-only (single-entity
                # reached event confirms+pops in ~100ms); carry a cycle-id on the
                # pending entry if that attribution ever needs to be exact.
                self._arm_verify(entity_id, trigger_context_id, forced=forced)
                # Fast-confirm: a responsive target with blocking=True commits its
                # post-enforce state to hass DURING the shielded await above, so
                # the ACK state_changed already fired while _pending_verifications
                # was empty — the event-driven confirm (_handle_state_event)
                # missed it, and verify would otherwise eat the full
                # VERIFY_DELAY_SECONDS blind deadline before re-reading (the
                # user-visible "why the long delay before validation"). Re-read
                # live state synchronously now that the verify is armed: if
                # already reached, confirm in this same tick. Pop first (the
                # _confirm_verified contract), then confirm. A silent-ignore
                # target reads not-reached here and keeps its deadline for the
                # corrective loop — unchanged.
                if self._verify_reached(entity_id):
                    # Gate the in-tick confirm through _verify_drop_reason, the SAME
                    # single source of truth the deadline _fire and event paths use.
                    # flags/suppression can lapse DURING the shielded service await
                    # above (set_enabled/master/flags are synchronous, take no
                    # reentrance_lock — see the post_status note below), so a reached
                    # AUTOMATIC cycle whose flags lapsed mid-enforce must drop
                    # CANCELLED here too, not confirm VERIFIED — otherwise the outcome
                    # flips VERIFIED↔CANCELLED on pure ACK-timing luck (fast vs
                    # deadline/event path). forced bypasses the gate (device-only
                    # judgement) identically to the other two paths. Reached-but-
                    # dropped is the twin of the event path's reached-but-dropped block
                    # (_handle_state_event): same reason, same unloaded-stays-silent.
                    reason = self._verify_drop_reason(entity_id)
                    # Lockstep with the 5 sibling drop sites (event path + deadline
                    # _fire branches) which all pop _verify_attempts alongside the
                    # pending handle. Unreachable here (fast-confirm runs only on a
                    # fresh non-corrective enforce, gated on pending ABSENT, so attempts
                    # is always 0), but keep the sibling-drop set uniform so a future
                    # reorder can't leak a stale count. _cancel_verify intentionally
                    # does NOT pop attempts (the escalation path calls it mid-cycle).
                    self._verify_attempts.pop(entity_id, None)
                    if reason is None:
                        self._cancel_verify(entity_id)
                        self._confirm_verified(entity_id, forced)
                    else:
                        self._cancel_verify(entity_id)
                        if reason != "unloaded":
                            self._fire_cancelled(entity_id, reason, forced)

        # Pick the post-enforce status once and emit a single _set_status so the
        # dispatcher fires exactly one update — avoids ENFORCING→ARMED→ERROR
        # flicker on each non-final recovery success (EG-6).
        if was_in_error and not recovered_from_error:
            post_status = STATUS_ERROR
        else:
            # Derive the real post-enforce status. For a normal debounced enforce a
            # cooldown was set, so this is COOLDOWN — unless a verify re-read was just
            # armed (verify outranks COOLDOWN → VERIFYING for the re-check window). A
            # corrective sets no cooldown and must not claim STATUS_COOLDOWN (a
            # cooldown with no expiry), which deriving also gets right.
            #
            # Route through _derive_idle_status, NOT _derive_armed_or_cooldown
            # directly: set_enabled()/master toggle are SYNCHRONOUS and take no
            # reentrance_lock, so a disable can interleave while this enforce is
            # parked in the shielded service-call await above. On resume the tail
            # must honour the now-off enabled/master flag and paint DISABLED/
            # MASTER_DISABLED — the raw armed/cooldown deriver has no such gate and
            # would overwrite the correct DISABLED paint with a stuck ARMED.
            post_status = self._derive_idle_status(dt_util.now())
        self._set_status(post_status)

        # Arm the cooldown-expiry broadcast (independent of status flicker).
        # Done BEFORE honouring a deferred cancel so the card still gets its
        # scheduled cooldown repaint on the cancel path (a debounced enforce
        # cancelled mid-ACK still set a cooldown above).
        if self._config.debounce_enabled and self._config.debounce_seconds > 0:
            cooldown_end = self._state.cooldowns.get(entity_id)
            if cooldown_end is not None:
                remaining = (cooldown_end - now).total_seconds()
                if remaining > 0:
                    old_unsub = self._cooldown_broadcast_unsubs.pop(entity_id, None)
                    if old_unsub is not None:
                        try:
                            old_unsub()
                        except Exception:
                            pass

                    _eid = entity_id

                    @callback
                    def _broadcast_after_cooldown(_now: datetime) -> None:
                        self._cooldown_broadcast_unsubs.pop(_eid, None)
                        if self._is_unloaded:
                            return
                        self._apply_idle_status()

                    unsub = async_call_later(
                        self._hass, remaining, _broadcast_after_cooldown
                    )
                    self._cooldown_broadcast_unsubs[entity_id] = unsub

        # Bookkeeping committed, status painted, cooldown broadcast armed — now
        # honour the cancel the per-entity replace delivered mid service-ACK.
        # Re-raising leaves this task cancelled; the replacement eval task
        # already owns the entity going forward.
        if pending_cancel:
            raise asyncio.CancelledError

    async def _trigger_loop_protection(
        self, entity_id: str, *, cause: str = "rate_limit"
    ) -> None:
        """Auto-suppress and notify the user.

        cause="rate_limit" (default): too many enforces/min — the classic loop.
        cause="unreachable_loop": a verify-enabled target never reached desired
        after CONSECUTIVE_EXHAUSTION_LIMIT full self-heal cycles — the dead-device
        kill-switch floor (fires even when the rate limit is disabled). Only the
        three user-facing strings branch on cause; the suppress-timer, pending
        cancel, and status set are identical either way.
        """
        if cause == "unreachable_loop":
            _LOGGER.warning(
                "Self-heal ceiling hit: rule='%s' entity=%s never reached desired "
                "after %d verify cycles — auto-suppressing for %d min",
                self._config.name,
                entity_id,
                CONSECUTIVE_EXHAUSTION_LIMIT,
                DEFAULT_LOOP_SUPPRESS_MINUTES,
            )
            suppression_reason = "unreachable_loop"
            notification = (
                f"Entity Guard rule `{self._config.name}` could not drive target "
                f"`{entity_id}` to its desired state after {CONSECUTIVE_EXHAUSTION_LIMIT} "
                f"verify cycles and was auto-suppressed for "
                f"{DEFAULT_LOOP_SUPPRESS_MINUTES} minutes. The target is likely "
                f"offline or being overridden."
            )
        else:
            _LOGGER.warning(
                "Loop protection triggered: rule='%s' entity=%s limit=%d/min — "
                "auto-suppressing for %d min",
                self._config.name,
                entity_id,
                self._config.max_enforcements_per_minute,
                DEFAULT_LOOP_SUPPRESS_MINUTES,
            )
            suppression_reason = "loop_protection"
            notification = (
                f"Entity Guard rule `{self._config.name}` exceeded "
                f"{self._config.max_enforcements_per_minute} enforcements/min and was "
                f"auto-suppressed for {DEFAULT_LOOP_SUPPRESS_MINUTES} minutes."
            )
        suppress_until = dt_util.now() + timedelta(
            minutes=DEFAULT_LOOP_SUPPRESS_MINUTES
        )
        self._state.suppressed_until = suppress_until
        self._state.suppression_reason = suppression_reason
        self._persist()
        self._schedule_suppression_timer()
        # Stop any pending enforcement too (same reason as manual suppress).
        self._cancel_pending_for_entities(self._config.target_entities)

        self._hass.bus.async_fire(
            EVENT_LOOP_DETECTED,
            {
                "rule_id": self._config.unique_id,
                "rule_name": self._config.name,
                "entity_id": entity_id,
                "suppressed_until": suppress_until.isoformat(),
                "limit": self._config.max_enforcements_per_minute,
                "cause": cause,
                # Loop detection is engine-driven rate-limiting, never a manual
                # force — forced is always False here (payload parity with siblings).
                "forced": False,
            },
        )
        persistent_notification.async_create(
            self._hass,
            notification,
            title="Entity Guard loop detected",
            notification_id=f"{DOMAIN}_loop_{self._config.unique_id}",
        )
        self._set_status(STATUS_SUPPRESSED)

    def _resolve_service(
        self,
        entity_id: str,
        *,
        state_override: str | None = None,
        value_override: Any | None = None,
    ) -> tuple[str, str, dict[str, Any]] | None:
        """Pick the right service call for the entity given the rule mode.

        state_override (dynamic_state) and value_override (dynamic_attribute) pass
        the live-derived target value; the fixed modes never pass them and keep
        their existing behavior byte-identically.
        """
        domain = entity_id.split(".", 1)[0]

        if self._config.mode == MODE_FIXED_STATE:
            return self._resolve_state_service(
                entity_id, domain, self._config.target_state
            )

        if self._config.mode == MODE_DYNAMIC_STATE:
            # Dynamic state MUST pass a computed direction; never fall back to
            # config.target_state (parse fills it "on" even for dynamic rules).
            assert state_override is not None, (
                "dynamic_state enforce without state_override"
            )
            return self._resolve_state_service(entity_id, domain, state_override)

        if self._config.mode == MODE_FIXED_ATTRIBUTE:
            return self._resolve_attribute_service(
                entity_id, self._config.attribute, self._config.target_value
            )

        if self._config.mode == MODE_DYNAMIC_ATTRIBUTE:
            # Dynamic attribute MUST pass a live value; never fall back to
            # config.target_value (parse writes none for dynamic rules).
            assert value_override is not None, (
                "dynamic_attribute enforce without value_override"
            )
            return self._resolve_attribute_service(
                entity_id, self._config.attribute, value_override
            )

        return None

    def _resolve_attribute_service(
        self, entity_id: str, attr: str | None, value: Any
    ) -> tuple[str, str, dict[str, Any]] | None:
        """Resolve the attribute-set service call. Shared by fixed + dynamic
        attribute modes; only the source of `value` differs (config vs live)."""
        if attr is None or value is None:
            return None
        mapping = ATTRIBUTE_SERVICE_MAP.get(attr)
        if mapping is None:
            return None
        full, kwarg = mapping
        svc_domain, svc_name = full.split(".", 1)
        return svc_domain, svc_name, {"entity_id": entity_id, kwarg: value}

    def _resolve_state_service(
        self, entity_id: str, domain: str, target_state: str
    ) -> tuple[str, str, dict[str, Any]] | None:
        """Resolve the on/off-style service for a target state.

        Extracted from the former MODE_FIXED_STATE body verbatim so follow mode can reuse
        the exact same domain-service resolution with a computed direction.
        """
        domain_map = DOMAIN_SERVICE_MAP.get(domain)
        if domain_map and target_state in domain_map:
            full = domain_map[target_state]
            svc_domain, svc_name = full.split(".", 1)
            return svc_domain, svc_name, {"entity_id": entity_id}

        # Bare on/off → homeassistant.turn_on/off, BEFORE the mode map. Follow mode
        # (dynamic_state) always feeds exactly "on"/"off" here, and a generic on/off
        # must never become an invalid mode payload (e.g. climate.set_hvac_mode
        # hvac_mode="on" — "on" is not a valid HVAC mode and the call fails). Only a
        # NON-on/off state (heat, eco, an input_select option…) should reach the mode
        # map. A mode domain keeps its parameterless entry above (climate "off" →
        # climate.turn_off) because that is matched before this branch.
        #
        # EXCEPT input_select: its options are arbitrary user strings, so a literal
        # "on"/"off" option is a real select_option target, and input_select has no
        # turn_on/off service — it must always route through the mode map below.
        if target_state in ("on", "off") and domain != "input_select":
            return (
                "homeassistant",
                f"turn_{target_state}",
                {"entity_id": entity_id},
            )

        # Mode-style domain (climate/water_heater/input_select): one service for the
        # whole domain, the non-on/off target_state passed as a data kwarg
        # (climate heat/cool/… → climate.set_hvac_mode hvac_mode=<state>). Reject a
        # state the device won't accept (absent from its valid-states attribute, e.g.
        # climate hvac_modes) so a doomed set_*_mode call becomes an unsupported_state
        # repair (return None → no_service_mapping → Card B) instead of a loud failure.
        mode_service = DOMAIN_MODE_SERVICE_MAP.get(domain)
        if mode_service is not None:
            full, kwarg, valid_attr = mode_service
            st = self._hass.states.get(entity_id)
            valid = st.attributes.get(valid_attr) if st else None
            # ponytail: reject ONLY when the valid set is known; a missing attr
            # (offline/unavailable target) → let through, the device/HA judges.
            if valid is not None and target_state not in valid:
                return None
            svc_domain, svc_name = full.split(".", 1)
            return svc_domain, svc_name, {"entity_id": entity_id, kwarg: target_state}

        return None

    def _event_target(
        self,
        *,
        follow_direction: str | None = None,
        follow_value: Any | None = None,
    ) -> Any:
        """Return the event/logbook target payload.

        follow_direction (dynamic_state) / follow_value (dynamic_attribute) are the
        value actually driven by the current enforce, captured before the service
        call. Passing them avoids re-reading the volatile source — a re-read could
        mislabel the logbook if the source moved between the decision and the event
        fire. Callers outside _enforce omit them (diagnostic) and fall back to a
        live read.
        """
        if self._config.mode == MODE_FIXED_STATE:
            return self._config.target_state
        if self._config.mode == MODE_DYNAMIC_STATE:
            if follow_direction is not None:
                direction = follow_direction
            else:
                desired_on = self._follow_desired_on()
                direction = "on" if desired_on else "off"
            return f"follow:{self._config.source_entity}:{direction}"
        if self._config.mode == MODE_DYNAMIC_ATTRIBUTE:
            value = (
                follow_value
                if follow_value is not None
                else self._dynamic_source_value()
            )
            src_from = (
                f"{self._config.source_entity}.{self._config.source_attribute}"
                if self._config.source_value_from == SOURCE_VALUE_FROM_ATTRIBUTE
                else self._config.source_entity
            )
            return f"follow:{src_from}->{self._config.attribute}={value}"
        if self._config.attribute in COLOR_ATTRIBUTES:
            return _describe_color_target(
                self._config.attribute, self._config.target_value
            )
        return self._config.target_value

    def _get_rule_entity_id(self) -> str | None:
        """Return this rule's status sensor entity_id (cached after first call).

        Used as event_data["entity_id"] in EVENT_ENFORCED so the logbook
        "filter by integration" query (which matches event_data.entity_id against
        Entity Guard entity_ids) finds enforcement events.
        """
        if self._rule_entity_id is not None:
            return self._rule_entity_id
        if self._entry is None:
            return None
        target_unique_id = f"{self._entry.entry_id}_status"
        ent_reg = er.async_get(self._hass)
        for entry in er.async_entries_for_config_entry(ent_reg, self._entry.entry_id):
            if entry.unique_id == target_unique_id:
                self._rule_entity_id = entry.entity_id
                return self._rule_entity_id
        return None

    def _fire_skipped(
        self,
        entity_id: str,
        reason: str,
        *,
        error: str | None = None,
        members: list[str] | None = None,
        forced: bool = False,
    ) -> None:
        """Emit a skipped-enforcement event.

        members: friendly-names of the group members behind a member_unavailable
        skip. Stored on the outcome (capped) so the card can list which members
        failed without re-deriving them.
        forced: this skip happened during a manual force (async_test_enforce).
        """
        data: dict[str, Any] = {
            "rule_id": self._config.unique_id,
            "rule_name": self._config.name,
            "entity_id": entity_id,
            "reason": reason,
            "forced": forced,
        }
        if error is not None:
            data["error"] = error
        # member_unavailable carries the failing group members. Cap at 3 names +
        # a "+N more" tail so a group with many dead members stays one short line.
        # Computed BEFORE the fire so the SAME capped shape lands on both the
        # event payload (automations read which members died) and the card
        # outcome below — one `shown`, never a raw-vs-capped mismatch.
        shown: list[str] | None = None
        if members:
            cap = 3
            shown = members[:cap]
            if len(members) > cap:
                shown = [*shown, f"+{len(members) - cap} more"]
            data["members"] = shown
        self._hass.bus.async_fire(EVENT_SKIPPED, data)
        # Skip counters: count only genuine "did-not-act" skips, NOT a failed
        # service call (REASON_SERVICE_CALL_FAILED has its own error path —
        # consecutive_errors / STATUS_ERROR — and counting it here would double-
        # count and muddy the misconfig signal). skip_count_today resets daily;
        # skip_count_total is persisted and monotonic like enforcement_count_total.
        if reason != REASON_SERVICE_CALL_FAILED:
            self._state.skip_count_today += 1
            self._state.skip_count_total += 1
        # Record the per-target outcome. REASON_SERVICE_CALL_FAILED is routed through
        # _fire_skipped but is semantically a FAIL, not a skip — derive from the
        # reason so this one write correctly labels every non-ok call site.
        self._state.target_outcomes[entity_id] = {
            "outcome": "failed" if reason == REASON_SERVICE_CALL_FAILED else "skipped",
            "reason": reason,
            "error": error,
            "at": dt_util.now(),
        }
        # member_unavailable: record the (already-capped) members on the outcome
        # so the card lists which members failed without re-deriving them. Same
        # `shown` the event payload carries. Only set when present so other
        # outcomes keep their old shape.
        if shown is not None:
            self._state.target_outcomes[entity_id]["members"] = shown
        # Persist so skip counts survive a restart like the enforcement counters.
        # A skip-only rule never hits the enforce-success persist, so without this
        # its skip_count_total would reset to 0 on every restart. _persist ->
        # set_rule_state is debounced internally, so a per-skip call is cheap.
        self._persist()

    async def async_test_enforce(self, user_id: str | None = None) -> None:
        """Force an enforcement run against every target entity.

        Suppression-expiry timer is re-armed after the loop so any stale
        callback queued before manual enforcement is replaced (EG-4).

        When the rule or master switch is disabled the service call still fires
        (so the user can validate the rule is correct) but status is restored to
        DISABLED/MASTER_DISABLED after each enforce so the UI never leaves the
        disabled state between entities.
        """
        disabled = not self._state.enabled or not self._master_enabled_getter()
        _LOGGER.info(
            "Test enforce invoked on rule '%s' (user_id=%s, disabled=%s)",
            self._config.name,
            user_id,
            disabled,
        )
        # Restart semantics (latest-wins): bump the generation and capture it. A
        # second press over a multi-target rule would otherwise interleave with this
        # pass at the per-rule reentrance_lock (two loop coroutines, double commands,
        # status flicker). The loop-top check below bails this pass as soon as a newer
        # press supersedes it. Cooperative, NOT Task.cancel — avoids the enforce
        # cancel-race (PR #70).
        self._state.force_generation += 1
        my_gen = self._state.force_generation
        for entity_id in self._config.target_entities:
            # Superseded by a newer press → abandon the rest of this pass. Must sit
            # BEFORE _enforce: _enforce acquires the per-rule reentrance_lock and
            # would block here behind the winning pass otherwise.
            if self._state.force_generation != my_gen:
                return
            await self._enforce(
                entity_id, user_id=user_id, bypass_rate_limit=True, forced=True
            )
            if disabled:
                # Restore correct disabled status after each enforce so
                # intermediate ENFORCING/ARMED/COOLDOWN transitions never
                # appear in HA history for a rule that should be inactive.
                self._apply_idle_status()
        # Only the winning pass re-arms the suppression timer; a superseded pass must
        # not stomp the winner's handle. _schedule_suppression_timer self-cancels any
        # prior handle before arming.
        if self._state.force_generation == my_gen:
            self._schedule_suppression_timer()

    async def async_reset_cooldowns(self) -> None:
        """Clear all per-entity cooldowns immediately.

        Suppression state is preserved (use async_unsuppress to clear it). The
        EG-4 expiry timer is re-armed so a pending suppression window still
        fires its expiry callback after this reset.
        """
        _LOGGER.info("Resetting cooldowns for rule '%s'", self._config.name)
        self._state.cooldowns.clear()
        for cancel in list(self._cooldown_broadcast_unsubs.values()):
            try:
                cancel()
            except Exception:
                pass
        self._cooldown_broadcast_unsubs.clear()
        self._cancel_recently_enforced_timer()
        had_recently_enforced = self._recently_enforced
        self._recently_enforced = False
        self._cancel_recently_enforced_verified_timer()
        had_recently_enforced = (
            had_recently_enforced or self._recently_enforced_verified
        )
        self._recently_enforced_verified = False
        self._cancel_recently_enforced_unreachable_timer()
        had_recently_enforced = (
            had_recently_enforced or self._recently_enforced_unreachable
        )
        self._recently_enforced_unreachable = False
        self._cancel_recently_enforced_cancelled_timer()
        had_recently_enforced = (
            had_recently_enforced or self._recently_enforced_cancelled
        )
        self._recently_enforced_cancelled = False
        # Re-arm suppression timer so EG-4 expiry callback survives this reset.
        self._schedule_suppression_timer()
        # Resetting also clears the recovery counter so a fresh window starts.
        self._state.consecutive_success_count = 0
        self._persist()
        prev_status = self._current_status
        self._apply_idle_status()
        # If status didn't change (e.g. already ARMED) and recently_enforced was True,
        # _set_status skip-if-same guard suppressed the broadcast — fire it now so
        # sensors reflecting recently_enforced refresh.
        if had_recently_enforced and self._current_status == prev_status:
            self._broadcast_status()

    async def async_suppress(
        self, duration_minutes: float, user_id: str | None = None
    ) -> None:
        """Suppress the rule for duration_minutes."""
        until = dt_util.now() + timedelta(minutes=duration_minutes)
        _LOGGER.info(
            "Suppressing rule '%s' for %.1f min (until %s, user_id=%s)",
            self._config.name,
            duration_minutes,
            until.isoformat(),
            user_id,
        )
        self._state.suppressed_until = until
        self._state.suppression_reason = "manual"
        self._persist()
        self._schedule_suppression_timer()
        # Cancel any in-flight pending enforcements (delay_seconds countdown): a
        # suppression must stop a rule that is about to fire, and clear the
        # per-entity due times so the card countdown disappears. Without this the
        # already-scheduled _fire timers run and enforce despite suppression.
        self._cancel_pending_for_entities(self._config.target_entities)

        self._hass.bus.async_fire(
            EVENT_SUPPRESSED,
            {
                "rule_id": self._config.unique_id,
                "rule_name": self._config.name,
                # Suppression is rule-level (covers all targets), so entity_id is the
                # rule's own status sensor, not a target — same as EVENT_ENFORCED /
                # EVENT_CANCELLED. Lets the integration-level trigger + logbook filter
                # surface a suppressed event with a non-None entity_id like its siblings.
                "entity_id": self._get_rule_entity_id(),
                "suppressed_until": until.isoformat(),
                "user_id": user_id,
                # Suppression is a manual/loop rule-state change, not an enforce —
                # forced (manual force of the enforce path) is always False.
                "forced": False,
            },
        )
        if self._current_status != STATUS_ERROR:
            self._apply_idle_status()

    async def async_unsuppress(self) -> None:
        """Clear any active suppression."""
        _LOGGER.info("Unsuppressing rule '%s'", self._config.name)
        self._clear_suppression_state()
        self._apply_idle_status()
        # Re-arm on manual clear too (same inactive→active transition as expiry):
        # a still-non-compliant target that emits no new event would otherwise go
        # unguarded until its next state change.
        if (
            self._state.enabled
            and self._startup_complete
            and self._master_enabled_getter()
        ):
            self._sweep_targets()

    async def async_clear_history(self) -> None:
        """Reset persisted counters and cooldowns.

        Suppression state is preserved (use async_unsuppress to clear it). The
        EG-4 expiry timer is re-armed so a pending suppression window still
        fires its expiry callback after history is cleared.
        """
        _LOGGER.info("Clearing history for rule '%s'", self._config.name)
        self._state.cooldowns.clear()
        for cancel in list(self._cooldown_broadcast_unsubs.values()):
            try:
                cancel()
            except Exception:
                pass
        self._cooldown_broadcast_unsubs.clear()
        self._cancel_recently_enforced_timer()
        had_recently_enforced = self._recently_enforced
        self._recently_enforced = False
        self._cancel_recently_enforced_verified_timer()
        had_recently_enforced = (
            had_recently_enforced or self._recently_enforced_verified
        )
        self._recently_enforced_verified = False
        self._cancel_recently_enforced_unreachable_timer()
        had_recently_enforced = (
            had_recently_enforced or self._recently_enforced_unreachable
        )
        self._recently_enforced_unreachable = False
        self._cancel_recently_enforced_cancelled_timer()
        had_recently_enforced = (
            had_recently_enforced or self._recently_enforced_cancelled
        )
        self._recently_enforced_cancelled = False
        # Re-arm suppression timer so EG-4 expiry callback survives this clear.
        self._schedule_suppression_timer()
        self._state.enforcement_count_today = 0
        self._state.enforcement_count_total = 0
        self._state.skip_count_today = 0
        self._state.skip_count_total = 0
        self._state.last_enforced = None
        self._state.rate_limit_window.clear()
        self._state.consecutive_errors = 0
        self._state.last_error = None
        self._state.consecutive_success_count = 0
        self._state.counter_total_since = dt_util.now()
        # _persist() serializes the full canonical runtime blob (already carries
        # the cleared counters set above). Flush synchronously so the reset —
        # including counter_total_since — survives an immediate HA crash.
        self._persist()
        await self._store.async_save_now()
        if self._current_status == STATUS_ERROR:
            self._current_status = STATUS_STARTING  # break sticky before re-derive
        try:
            # Unconditional broadcast so counter sensors (today/total/last_enforced)
            # always reflect cleared values regardless of status or recently_enforced.
            self._broadcast_status()
            prev_status = self._current_status
            self._apply_idle_status()
            # Status didn't change → _set_status skip-if-same guard suppressed the
            # broadcast → fire explicitly so recently_enforced binary sensor refreshes.
            if had_recently_enforced and self._current_status == prev_status:
                self._broadcast_status()
        except Exception:  # pragma: no cover
            # Ensure sentinel STATUS_STARTING is never left stranded if derivation fails.
            if self._current_status == STATUS_STARTING:
                self._current_status = (
                    STATUS_DISABLED if not self._state.enabled else STATUS_ARMED
                )
            raise

    def set_enabled(self, enabled: bool) -> None:
        """Toggle the rule's enabled flag (driven by per-rule switch entity)."""
        _LOGGER.info(
            "Rule '%s' %s",
            self._config.name,
            "enabled" if enabled else "DISABLED",
        )
        self._state.enabled = enabled
        # Re-enabling clears stale suppression so we don't strand the rule.
        if enabled and self._state.suppressed_until:
            now = dt_util.now()
            if self._state.suppressed_until <= now:
                self._clear_suppression_state(persist=False)
        if not enabled:
            # Disabling cancels any pending suppression timer; suppression state
            # itself is preserved so re-enabling restores the remaining window.
            self._cancel_suppression_timer()
            # Also cancel any in-flight verify timers: _apply_idle_status below
            # cancels pending ENFORCEMENTS on the DISABLED branch but not verifies,
            # so a stale verify deadline would otherwise survive the disable and
            # fire later. This IS the real disabled-drop site: _state.enabled has
            # one writer (above), so a per-rule disable always lands here BEFORE
            # the deadline/event paths can fire — fire the cancelled signal and
            # clear the attempt counter here, or neither ever happens on disable.
            for eid in self._config.target_entities:
                had_pending = eid in self._pending_verifications
                # Capture forced-origin before _cancel_verify discards it (same
                # reason as the event path): a manual force cycle disabled
                # mid-verify still reports forced=true on its cancelled signal.
                was_forced = eid in self._forced_verifications
                self._cancel_verify(eid)
                # Drop the attempt counter so a quick re-enable + re-enforce starts
                # a fresh verify cycle instead of reading a stale high count and
                # latching exhaustion early (false EVENT_UNREACHABLE + a bogus
                # _consecutive_exhaustions tick). Twin of the deadline/event drops.
                self._verify_attempts.pop(eid, None)
                # Abandoned mid-window → the "disabled" cancelled signal. Skip on
                # unload (entry tearing down, matching the no-fire-on-unload rule).
                if had_pending and not self._is_unloaded:
                    self._fire_cancelled(eid, "disabled", was_forced)
        else:
            self._schedule_suppression_timer()
        self._persist()
        self._apply_idle_status()
        if enabled and self._startup_complete and self._master_enabled_getter():
            self._sweep_targets()

    @callback
    def _handle_master_changed(self) -> None:
        """Re-derive status when the master switch toggles."""
        self._apply_idle_status()
        if (
            self._master_enabled_getter()
            and self._state.enabled
            and self._startup_complete
        ):
            self._sweep_targets()

    def current_status(self) -> str:
        """Return the rule's current status string."""
        return self._current_status

    def follow_desired_on(self) -> bool | None:
        """Public view of the follow desired on/off (None if N/A or source offline).

        Used by the status sensor so the frontend card can show per-target
        compliance without re-implementing the FOLLOW_ON_STATES normalizer in JS.
        """
        if self._config.mode != MODE_DYNAMIC_STATE:
            return None
        return self._follow_desired_on()

    def attribute_desired_value(self) -> Any | None:
        """Public view of the DESIRED attribute value for the attribute modes, for
        the card's bound-row '→ <attr>: <current> → <desired>' + match indicator.

        fixed_attribute → the configured target_value.
        dynamic_attribute → the live value read from the source (None when the
        source is offline or the chosen attribute is missing — card shows holding).
        Other modes → None (no attribute desired).
        """
        if self._config.mode == MODE_FIXED_ATTRIBUTE:
            return self._config.target_value
        if self._config.mode == MODE_DYNAMIC_ATTRIBUTE:
            return self._dynamic_source_value()
        return None

    def pending_enforcement_due(self) -> dict[str, datetime]:
        """Return a snapshot of entity_id → fire time for pending enforcements."""
        return dict(self._pending_enforcement_due)

    def is_armed(self) -> bool:
        """Return True if the rule is armed."""
        return self._current_status == STATUS_ARMED

    def is_active(self) -> bool:
        """Return True if a service call is currently in flight."""
        return self._current_status == STATUS_ENFORCING

    def is_in_cooldown(self) -> bool:
        """Return True if any tracked entity is still in cooldown."""
        return self._current_status == STATUS_COOLDOWN

    def is_pending(self) -> bool:
        """Return True if a delayed enforcement is queued."""
        return self._current_status == STATUS_PENDING

    def is_recently_enforced(self) -> bool:
        """Return True if enforcement fired within the last 30 seconds."""
        return self._recently_enforced

    def is_recently_enforced_verified(self) -> bool:
        """Return True if a verify-confirmed enforcement fired in the last 30s."""
        return self._recently_enforced_verified

    def is_recently_enforced_unreachable(self) -> bool:
        """Return True if a verify-exhausted failure fired in the last 30s."""
        return self._recently_enforced_unreachable

    def is_recently_enforced_cancelled(self) -> bool:
        """Return True if a verify cycle was abandoned in the last 30s."""
        return self._recently_enforced_cancelled

    def cooldown_remaining_seconds(self) -> float:
        """Return the largest remaining cooldown across tracked entities."""
        now = dt_util.now()
        remaining = 0.0
        for end in self._state.cooldowns.values():
            delta = (end - now).total_seconds()
            remaining = max(remaining, delta)
        return remaining

    def cooldown_ends(self) -> dict[str, datetime]:
        """Return entity_id → absolute cooldown-end, future ends only.

        Mirrors cooldown_remaining_seconds' past-skipping: an end already in the
        past contributes 0 to the scalar, so it is omitted here too — the card
        derives its live countdown from these and must not see a stale past end.
        """
        now = dt_util.now()
        return {eid: end for eid, end in self._state.cooldowns.items() if end > now}

    def _set_status(self, status: str) -> None:
        """Update current status and broadcast if changed."""
        if status == self._current_status:
            return
        self._current_status = status
        self._broadcast_status()

    def _disabled_status(self) -> str:
        """Return MASTER_DISABLED if master is off, else DISABLED (per-rule)."""
        if not self._master_enabled_getter():
            return STATUS_MASTER_DISABLED
        return STATUS_DISABLED

    def _derive_idle_status(self, now: datetime | None = None) -> str:
        """Single source of truth for status when idle.

        Priority (highest first):
          1. Master switch off -> MASTER_DISABLED.
          2. Per-rule disabled -> DISABLED.
          3. STATUS_ERROR sticky -> ERROR (only when rule is active).
          4. Active suppression -> SUPPRESSED.
          5. Action in flight (PENDING/VERIFYING) -> that action.
          6. Conditional flags unmet -> CONDITIONAL.
          7. Otherwise -> derive ARMED / COOLDOWN.
        """
        if not self._master_enabled_getter():
            return STATUS_MASTER_DISABLED
        if not self._state.enabled:
            return STATUS_DISABLED
        if self._current_status == STATUS_ERROR:
            return STATUS_ERROR
        if now is None:
            now = dt_util.now()
        if self._state.suppressed_until and self._state.suppressed_until > now:
            return STATUS_SUPPRESSED
        # In-flight action outranks CONDITIONAL: a pending enforce or an armed
        # verify re-read means we ALREADY decided to act (a trigger matched) and
        # are mid-cycle. The flags were evaluated at trigger time; a flag that
        # flips back during the enforce/verify window must not mask the live
        # action as CONDITIONAL. Without this, a flag-gated rule shows
        # ENFORCING -> CONDITIONAL the instant the target reaches desired, hiding
        # the outstanding verify (which only surfaced later, backwards, when the
        # deadline fired a corrective re-enforce). DISABLED/MASTER/ERROR/
        # SUPPRESSED still outrank — those are "must not act" gates, not stale
        # flag state.
        if self._pending_enforcements:
            return STATUS_PENDING
        if self._pending_verifications:
            return STATUS_VERIFYING
        if not self._flags_match():
            return STATUS_CONDITIONAL
        return self._derive_armed_or_cooldown(now)

    def _apply_idle_status(
        self, now: datetime | None = None, *, broadcast_on_dedupe: bool = False
    ) -> None:
        """Compute and broadcast the idle status, cancelling pending if disabled.

        broadcast_on_dedupe: when the computed status equals the current one
        (so _set_status would swallow the broadcast), force a broadcast anyway.
        Used by the disabled-rule path in async_evaluate so a watched-entity
        change still refreshes the status sensor's live attributes on the card.
        """
        status = self._derive_idle_status(now)
        if status in (STATUS_DISABLED, STATUS_MASTER_DISABLED):
            self._cancel_pending_for_entities(self._config.target_entities)
            # Cancel cooldown-broadcast timers — they would fire later and emit a
            # redundant signal for a rule that is inactive (C5).
            for unsub in list(self._cooldown_broadcast_unsubs.values()):
                try:
                    unsub()
                except Exception:
                    pass
            self._cooldown_broadcast_unsubs.clear()
        prev = self._current_status
        self._set_status(status)
        if broadcast_on_dedupe and status == prev:
            self._broadcast_status()

    def _broadcast_status(self) -> None:
        """Tell platforms to refresh."""
        async_dispatcher_send(self._hass, signal_for_rule(self._config.unique_id))

    def _persist(self) -> None:
        """Push runtime state to the store (debounced internally)."""
        blob = EntityGuardStore.runtime_to_blob(self._state)
        self._store.set_rule_state(self._config.unique_id, blob)

    def _clear_suppression_state(self, *, persist: bool = True) -> None:
        """Clear suppression fields and cancel the expiry timer (EG-4)."""
        self._state.suppressed_until = None
        self._state.suppression_reason = None
        self._cancel_suppression_timer()
        if persist:
            self._persist()

    def _cancel_suppression_timer(self) -> None:
        """Cancel any pending suppression-expiry timer."""
        if self._suppression_timer_unsub is None:
            return
        try:
            self._suppression_timer_unsub()
        except Exception:
            _LOGGER.debug("Suppression timer cancel failed", exc_info=True)
        self._suppression_timer_unsub = None

    def _cancel_recently_enforced_timer(self) -> None:
        """Cancel any pending recently-enforced reset timer."""
        if self._recently_enforced_unsub is None:
            return
        try:
            self._recently_enforced_unsub()
        except Exception:
            pass
        self._recently_enforced_unsub = None

    def _arm_recently_enforced(self) -> None:
        """Set recently_enforced flag and schedule a 30s reset."""
        self._cancel_recently_enforced_timer()

        if self._recently_enforced:
            # Already on — pulse off so automations re-trigger on the rising edge.
            self._recently_enforced = False
            async_dispatcher_send(
                self._hass, signal_rule_update(self._config.unique_id)
            )

        self._recently_enforced = True

        @callback
        def _clear(_now: datetime) -> None:
            self._recently_enforced_unsub = None
            if self._is_unloaded:
                return
            self._recently_enforced = False
            async_dispatcher_send(
                self._hass, signal_rule_update(self._config.unique_id)
            )

        self._recently_enforced_unsub = async_call_later(
            self._hass,
            RECENTLY_ENFORCED_WINDOW_SECONDS,
            HassJob(
                _clear, "entity_guard_recently_enforced_reset", cancel_on_shutdown=True
            ),
        )

    def _cancel_recently_enforced_verified_timer(self) -> None:
        """Cancel any pending recently-enforced-verified reset timer."""
        if self._recently_enforced_verified_unsub is None:
            return
        try:
            self._recently_enforced_verified_unsub()
        except Exception:
            pass
        self._recently_enforced_verified_unsub = None

    def _clear_other_verdicts(self, keep: str) -> bool:
        """Force the two verdict flags other than `keep` OFF (mutual exclusion).

        The three verdict sensors — verified / unreachable / cancelled — are
        mutually exclusive: a cycle resolves to exactly one. For each sibling
        verdict whose flag is currently True, cancel its reset timer and drop its
        flag. Returns True if any sibling was dropped (the caller then forces one
        repaint — see the arm methods). No dispatch here; the base
        `recently_enforced` (command-sent axis) is deliberately NOT in the group.
        """
        verdicts = ("verified", "unreachable", "cancelled")
        dropped = False
        for name in verdicts:
            if name == keep:
                continue
            if getattr(self, f"_recently_enforced_{name}"):
                getattr(self, f"_cancel_recently_enforced_{name}_timer")()
                setattr(self, f"_recently_enforced_{name}", False)
                dropped = True
        return dropped

    def _arm_recently_enforced_verified(self) -> None:
        """Set recently_enforced_verified flag and schedule a 30s reset.

        Twin of _arm_recently_enforced for the verify-confirmed signal. Pulses off
        first so an automation gating on the rising edge re-triggers on a second
        confirm within the window.
        """
        dropped_sibling = self._clear_other_verdicts("verified")
        self._cancel_recently_enforced_verified_timer()

        if self._recently_enforced_verified:
            self._recently_enforced_verified = False
            async_dispatcher_send(
                self._hass, signal_rule_update(self._config.unique_id)
            )

        self._recently_enforced_verified = True

        @callback
        def _clear(_now: datetime) -> None:
            self._recently_enforced_verified_unsub = None
            if self._is_unloaded:
                return
            self._recently_enforced_verified = False
            async_dispatcher_send(
                self._hass, signal_rule_update(self._config.unique_id)
            )

        self._recently_enforced_verified_unsub = async_call_later(
            self._hass,
            RECENTLY_ENFORCED_WINDOW_SECONDS,
            HassJob(
                _clear,
                "entity_guard_recently_enforced_verified_reset",
                cancel_on_shutdown=True,
            ),
        )

        # A cold verdict switch (a sibling was on, this arm did not hit its warm
        # self-pulse branch) silently dropped the sibling flag; the trailing
        # idle-status derive swallows the broadcast when status is unchanged
        # (verdicts all sit ARMED/COOLDOWN). Force one repaint so the sibling
        # sensor turns off — mirrors the bulk-reset precedent (~2345).
        if dropped_sibling:
            async_dispatcher_send(
                self._hass, signal_rule_update(self._config.unique_id)
            )

    def _fire_verified(
        self, target_entity_id: str, attempts: int, forced: bool = False
    ) -> None:
        """Emit the confirmed-success event + arm the verified sensor.

        Called only from the verify-confirm branch (verify-on, reached desired).
        Payload mirrors EVENT_ENFORCED minus the provisional-only fields, plus
        `attempts` (1 = verified first read; N+1 = after N corrective retries).
        `forced` = this verify cycle was a manual force (async_test_enforce) —
        threaded in because the caller already discarded _forced_verifications
        membership by fire time (see _cancel_verify).
        """
        self._hass.bus.async_fire(
            EVENT_VERIFIED,
            {
                "rule_id": self._config.unique_id,
                "rule_name": self._config.name,
                "entity_id": self._get_rule_entity_id() or target_entity_id,
                "target_entity_id": target_entity_id,
                "domain": target_entity_id.split(".", 1)[0],
                "trigger": self._config.mode,
                "target": self._event_target(),
                "attempts": attempts,
                "forced": forced,
            },
        )
        self._arm_recently_enforced_verified()

    def _cancel_recently_enforced_unreachable_timer(self) -> None:
        """Cancel any pending recently-enforced-unreachable reset timer."""
        if self._recently_enforced_unreachable_unsub is None:
            return
        try:
            self._recently_enforced_unreachable_unsub()
        except Exception:
            pass
        self._recently_enforced_unreachable_unsub = None

    def _arm_recently_enforced_unreachable(self) -> None:
        """Set recently_enforced_unreachable flag and schedule a 30s reset.

        Failure twin of _arm_recently_enforced_verified. Pulses off first so an
        automation gating on the rising edge re-triggers on a second failure
        within the window.
        """
        dropped_sibling = self._clear_other_verdicts("unreachable")
        self._cancel_recently_enforced_unreachable_timer()

        if self._recently_enforced_unreachable:
            self._recently_enforced_unreachable = False
            async_dispatcher_send(
                self._hass, signal_rule_update(self._config.unique_id)
            )

        self._recently_enforced_unreachable = True

        @callback
        def _clear(_now: datetime) -> None:
            self._recently_enforced_unreachable_unsub = None
            if self._is_unloaded:
                return
            self._recently_enforced_unreachable = False
            async_dispatcher_send(
                self._hass, signal_rule_update(self._config.unique_id)
            )

        self._recently_enforced_unreachable_unsub = async_call_later(
            self._hass,
            RECENTLY_ENFORCED_WINDOW_SECONDS,
            HassJob(
                _clear,
                "entity_guard_recently_enforced_unreachable_reset",
                cancel_on_shutdown=True,
            ),
        )

        # Cold verdict switch repaint — see _arm_recently_enforced_verified.
        if dropped_sibling:
            async_dispatcher_send(
                self._hass, signal_rule_update(self._config.unique_id)
            )

    def _fire_unreachable(self, target_entity_id: str, forced: bool = False) -> None:
        """Emit the unreachable-failure event + arm the unreachable sensor.

        Failure twin of _fire_verified. Called once per fresh exhaustion streak
        (the count==1 boundary in _arm_verify), only under verify-on when the
        target never reached desired after the corrective retries. Payload
        mirrors EVENT_VERIFIED minus `attempts`, plus the failure `reason`.
        `forced` = manual force cycle (see _fire_verified).
        """
        self._hass.bus.async_fire(
            EVENT_UNREACHABLE,
            {
                "rule_id": self._config.unique_id,
                "rule_name": self._config.name,
                "entity_id": self._get_rule_entity_id() or target_entity_id,
                "target_entity_id": target_entity_id,
                "domain": target_entity_id.split(".", 1)[0],
                "trigger": self._config.mode,
                "target": self._event_target(),
                "reason": REASON_UNREACHABLE_AFTER_RETRIES,
                "forced": forced,
            },
        )
        self._arm_recently_enforced_unreachable()

    def _cancel_recently_enforced_cancelled_timer(self) -> None:
        """Cancel any pending recently-enforced-cancelled reset timer."""
        if self._recently_enforced_cancelled_unsub is None:
            return
        try:
            self._recently_enforced_cancelled_unsub()
        except Exception:
            pass
        self._recently_enforced_cancelled_unsub = None

    def _arm_recently_enforced_cancelled(self) -> None:
        """Set recently_enforced_cancelled flag and schedule a 30s reset.

        Abandoned-cycle twin of _arm_recently_enforced_verified. Pulses off first
        so an automation gating on the rising edge re-triggers on a second
        abandonment within the window.
        """
        dropped_sibling = self._clear_other_verdicts("cancelled")
        self._cancel_recently_enforced_cancelled_timer()

        if self._recently_enforced_cancelled:
            self._recently_enforced_cancelled = False
            async_dispatcher_send(
                self._hass, signal_rule_update(self._config.unique_id)
            )

        self._recently_enforced_cancelled = True

        @callback
        def _clear(_now: datetime) -> None:
            self._recently_enforced_cancelled_unsub = None
            if self._is_unloaded:
                return
            self._recently_enforced_cancelled = False
            async_dispatcher_send(
                self._hass, signal_rule_update(self._config.unique_id)
            )

        self._recently_enforced_cancelled_unsub = async_call_later(
            self._hass,
            RECENTLY_ENFORCED_WINDOW_SECONDS,
            HassJob(
                _clear,
                "entity_guard_recently_enforced_cancelled_reset",
                cancel_on_shutdown=True,
            ),
        )

        # Cold verdict switch repaint — see _arm_recently_enforced_verified.
        if dropped_sibling:
            async_dispatcher_send(
                self._hass, signal_rule_update(self._config.unique_id)
            )

    def _fire_cancelled(
        self, target_entity_id: str, reason: str, forced: bool = False
    ) -> None:
        """Emit the abandoned-cycle event + arm the cancelled sensor.

        Neutral sibling of _fire_verified / _fire_unreachable. Called at a verify
        DROP site (deadline _fire or event _verify_event_dropped) when an armed
        verify cycle is abandoned mid-window because an external condition changed
        — reason ∈ {disabled, master_off, suppressed, flags_unmet}. NOT fired on
        integration unload (the entry is tearing down). Payload mirrors
        EVENT_UNREACHABLE with the abandonment `reason` in place of the failure one.
        `forced` = manual force cycle (see _fire_verified).
        """
        self._hass.bus.async_fire(
            EVENT_CANCELLED,
            {
                "rule_id": self._config.unique_id,
                "rule_name": self._config.name,
                "entity_id": self._get_rule_entity_id() or target_entity_id,
                "target_entity_id": target_entity_id,
                "domain": target_entity_id.split(".", 1)[0],
                "trigger": self._config.mode,
                "target": self._event_target(),
                "reason": reason,
                "forced": forced,
            },
        )
        self._arm_recently_enforced_cancelled()

    def _schedule_suppression_timer(self) -> None:
        """Schedule an expiry callback to refresh UI when suppression ends (EG-4).

        Cancels any prior timer. No-op when there is no active suppression or the
        engine has been unloaded.
        """
        self._cancel_suppression_timer()
        if self._is_unloaded:
            return
        until = self._state.suppressed_until
        if until is None:
            return
        if until <= dt_util.now():
            return
        # async_track_point_in_time tracks wall-clock; survives HA time-skip / DST.
        self._suppression_timer_unsub = async_track_point_in_time(
            self._hass, self._handle_suppression_expired, until
        )

    @callback
    def _handle_suppression_expired(self, _now: datetime) -> None:
        """Suppression window elapsed — refresh status without waiting for an event."""
        self._suppression_timer_unsub = None
        if self._is_unloaded:
            return
        if (
            self._state.suppressed_until is not None
            and self._state.suppressed_until <= dt_util.now()
        ):
            self._clear_suppression_state()
        self._apply_idle_status()
        # Re-arm: suppression is an inactive→active transition, same as re-enabling
        # or the master switch coming back on (both sweep). Without this, a target
        # that was non-compliant when suppressed — and emits no new state event
        # while suppressed — is never re-evaluated after expiry, so the rule
        # silently stops guarding it. Mirror the guard used in set_enabled.
        if (
            self._state.enabled
            and self._startup_complete
            and self._master_enabled_getter()
        ):
            self._sweep_targets()


def _value_is_on(value: Any) -> bool:
    """Reduce an arbitrary LIVE source value to on/off for dynamic_state.

    Called only when dynamic_state reads a source ATTRIBUTE (never for None — the
    caller returns not-triggered for a missing attribute before calling here).
    Precedence is load-bearing:
      1. bool            → itself.
      2. number          → non-zero is on (0 / 0.0 off; any non-zero, incl negative,
                           is on — a non-zero signal).
      3. string          → numeric first ("0"/"0.0" off, other numbers on); then the
                           SAME FOLLOW_ON_STATES set the state path uses (one source
                           of truth for on-ness) — on-ish → on; anything else → OFF.
                           A non-on-ish word ("idle", "paused", "") is OFF, matching
                           the state path (not-in-FOLLOW_ON_STATES = off), NOT a
                           generic non-empty-truthy.
      4. other non-None  → Python truthiness (e.g. a non-empty list/tuple).
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        # Non-finite (nan/inf) is not a meaningful on signal → off.
        return math.isfinite(value) and value != 0
    if isinstance(value, str):
        s = value.strip().lower()
        try:
            f = float(s)
        except ValueError:
            return s in FOLLOW_ON_STATES
        # A numeric string that parsed to nan/inf falls through to the on-ish
        # check (so a literal "nan"/"inf" is not on unless it is in FOLLOW_ON_STATES,
        # which it is not) rather than reading as on.
        if not math.isfinite(f):
            return s in FOLLOW_ON_STATES
        return f != 0
    return bool(value)


def _compare(value: float, op: str, threshold: float) -> bool:
    """Apply the configured comparison operator."""
    if op == OPERATOR_LT:
        return value < threshold
    if op == OPERATOR_LE:
        return value <= threshold
    if op == OPERATOR_GT:
        return value > threshold
    if op == OPERATOR_GE:
        return value >= threshold
    return False


def _normalize_rgb_color(value: Any) -> tuple[int, int, int] | None:
    """Normalize an RGB value into a 3-tuple."""
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return None
    try:
        return tuple(int(channel) for channel in value)
    except (TypeError, ValueError):
        return None


def _normalize_kelvin(value: Any) -> int | None:
    """Normalize a Kelvin value to an int.

    Intentionally not range-checked: this runs at comparison time on both the
    configured target (already validated on load by models._to_kelvin_or_none)
    and the live device state, which we can't control — a device reporting an
    out-of-range Kelvin should still be compared, not silently ignored.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _rgb_matches(current: tuple[int, int, int], target: tuple[int, int, int]) -> bool:
    """Return True when every RGB channel is within tolerance."""
    return all(
        abs(current_channel - target_channel) <= COLOR_RGB_TOLERANCE
        for current_channel, target_channel in zip(current, target, strict=True)
    )


def _describe_color_target(attribute: Any, value: Any) -> str:
    """Return a log/event-friendly description for a color target."""
    if attribute == ATTR_RGB_COLOR:
        return f"{ATTR_RGB_COLOR}={list(value) if isinstance(value, tuple) else value}"
    if attribute == ATTR_COLOR_TEMP_KELVIN:
        return f"{ATTR_COLOR_TEMP_KELVIN}={value}K"
    return str(value)
