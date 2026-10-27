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
    DEFAULT_LOOP_SUPPRESS_MINUTES,
    DOMAIN,
    DOMAIN_SERVICE_MAP,
    ERROR_RECOVERY_SUCCESS_THRESHOLD,
    ERROR_THRESHOLD,
    EVENT_ENFORCED,
    EVENT_LOOP_DETECTED,
    EVENT_SKIPPED,
    EVENT_SUPPRESSED,
    FOLLOW_ON_STATES,
    MODE_DYNAMIC_ATTRIBUTE,
    MODE_DYNAMIC_STATE,
    MODE_FIXED_ATTRIBUTE,
    MODE_FIXED_STATE,
    OPERATOR_GE,
    OPERATOR_GT,
    OPERATOR_LE,
    OPERATOR_LT,
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
    signal_master,
    signal_rule_update,
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
        self._startup_complete = False
        self._is_unloaded: bool = False
        self._current_status: str = STATUS_STARTING
        self._flag_entity_ids: frozenset[str] = frozenset(
            f.entity for f in config.flags
        )
        self._cooldown_broadcast_unsubs: dict[str, Callable[[], None]] = {}
        self._recently_enforced: bool = False
        self._recently_enforced_unsub: Callable[[], None] | None = None
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

        self._persist()
        await self._store.async_save_now()

    @callback
    def _handle_state_event(self, event: Event) -> None:
        """State change listener — schedules async evaluation, deduplicating per entity."""
        entity_id = event.data.get("entity_id")
        new_state = event.data.get("new_state")
        if entity_id is None:
            return
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
        """Pick between PENDING, ARMED, and COOLDOWN based on current state."""
        if self._pending_enforcements:
            return STATUS_PENDING
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
                if self._pending_enforcements.get(entity_id) is my_cancel:
                    self._pending_enforcements.pop(entity_id, None)
                    self._pending_enforcement_due.pop(entity_id, None)
                    self._set_or_force_broadcast(self._derive_armed_or_cooldown(_now))
                self._fire_skipped(entity_id, skip_reason)
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
                    # _derive_armed_or_cooldown returned PENDING and the broadcast
                    # was a no-op. Re-broadcast now that the dict is clean.
                    self._broadcast_status()

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

    async def _enforce(
        self,
        entity_id: str,
        *,
        user_id: str | None = None,
        bypass_rate_limit: bool = False,
        trigger_context_id: str | None = None,
    ) -> None:
        """Rate-limit, lock, and execute the enforcement service call."""
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
                    self._fire_skipped(entity_id, skip_reason)
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
                    self._fire_skipped(entity_id, "source_offline")
                    self._set_status(self._derive_armed_or_cooldown(now))
                    return
                state_override = "on" if desired_on else "off"
            elif self._config.mode == MODE_DYNAMIC_ATTRIBUTE:
                value_override = self._dynamic_source_value()
                if value_override is None:
                    self._fire_skipped(entity_id, "source_value_unavailable")
                    self._set_status(self._derive_armed_or_cooldown(now))
                    return

            service_call = self._resolve_service(
                entity_id, state_override=state_override, value_override=value_override
            )
            if service_call is None:
                self._fire_skipped(entity_id, "no_service_mapping")
                _LOGGER.debug("No service mapping resolved for %s", entity_id)
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
            try:
                await self._hass.services.async_call(
                    domain, service, data, blocking=True, context=ctx
                )
            except Exception as err:
                # Spec: target unavailable / service errors → skip silently + debug log.
                self._fire_skipped(entity_id, "service_call_failed", error=str(err))
                _LOGGER.warning(
                    "Enforcement failed for %s on rule '%s': %s",
                    entity_id,
                    self._config.name,
                    err,
                )
                self._state.consecutive_errors += 1
                self._state.last_error = str(err)
                # Reset recovery counter on any failure (EG-6).
                self._state.consecutive_success_count = 0
                self._persist()
                if self._state.consecutive_errors >= ERROR_THRESHOLD:
                    self._set_status(STATUS_ERROR)
                else:
                    self._set_status(self._derive_armed_or_cooldown(now))
                return

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
            recovered_from_error = False
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
                },
                context=ctx,
            )

            self._arm_recently_enforced()
            self._persist()
            # Per-target success — overwrites any prior skipped/failed entry so the
            # card row for this target clears once it enforces cleanly again.
            self._state.target_outcomes[entity_id] = {
                "outcome": "ok",
                "reason": None,
                "error": None,
                "at": now,
            }

        # Pick the post-enforce status once and emit a single _set_status so the
        # dispatcher fires exactly one update — avoids ENFORCING→ARMED→ERROR
        # flicker on each non-final recovery success (EG-6).
        if was_in_error and not recovered_from_error:
            post_status = STATUS_ERROR
        elif self._config.debounce_enabled and self._config.debounce_seconds > 0:
            post_status = STATUS_COOLDOWN
        else:
            post_status = self._derive_armed_or_cooldown(dt_util.now())
        self._set_status(post_status)

        # Arm the cooldown-expiry broadcast (independent of status flicker).
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

    async def _trigger_loop_protection(self, entity_id: str) -> None:
        """Auto-suppress on rate-limit breach and notify the user."""
        _LOGGER.warning(
            "Loop protection triggered: rule='%s' entity=%s limit=%d/min — "
            "auto-suppressing for %d min",
            self._config.name,
            entity_id,
            self._config.max_enforcements_per_minute,
            DEFAULT_LOOP_SUPPRESS_MINUTES,
        )
        suppress_until = dt_util.now() + timedelta(
            minutes=DEFAULT_LOOP_SUPPRESS_MINUTES
        )
        self._state.suppressed_until = suppress_until
        self._state.suppression_reason = "loop_protection"
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
            },
        )
        persistent_notification.async_create(
            self._hass,
            f"Entity Guard rule `{self._config.name}` exceeded "
            f"{self._config.max_enforcements_per_minute} enforcements/min and was "
            f"auto-suppressed for {DEFAULT_LOOP_SUPPRESS_MINUTES} minutes.",
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

        # Fallback: homeassistant.turn_on / turn_off when target maps to on/off.
        if target_state in ("on", "off"):
            return (
                "homeassistant",
                f"turn_{target_state}",
                {"entity_id": entity_id},
            )
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
        self, entity_id: str, reason: str, *, error: str | None = None
    ) -> None:
        """Emit a skipped-enforcement event."""
        data: dict[str, Any] = {
            "rule_id": self._config.unique_id,
            "rule_name": self._config.name,
            "entity_id": entity_id,
            "reason": reason,
        }
        if error is not None:
            data["error"] = error
        self._hass.bus.async_fire(EVENT_SKIPPED, data)
        # Record the per-target outcome. "service_call_failed" is routed through
        # _fire_skipped but is semantically a FAIL, not a skip — derive from the
        # reason so this one write correctly labels every non-ok call site.
        self._state.target_outcomes[entity_id] = {
            "outcome": "failed" if reason == "service_call_failed" else "skipped",
            "reason": reason,
            "error": error,
            "at": dt_util.now(),
        }

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
        for entity_id in self._config.target_entities:
            await self._enforce(entity_id, user_id=user_id, bypass_rate_limit=True)
            if disabled:
                # Restore correct disabled status after each enforce so
                # intermediate ENFORCING/ARMED/COOLDOWN transitions never
                # appear in HA history for a rule that should be inactive.
                self._apply_idle_status()
        # _schedule_suppression_timer self-cancels any prior handle before arming.
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
                "suppressed_until": until.isoformat(),
                "user_id": user_id,
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
        # Re-arm suppression timer so EG-4 expiry callback survives this clear.
        self._schedule_suppression_timer()
        self._state.enforcement_count_today = 0
        self._state.enforcement_count_total = 0
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

    def cooldown_remaining_seconds(self) -> float:
        """Return the largest remaining cooldown across tracked entities."""
        now = dt_util.now()
        remaining = 0.0
        for end in self._state.cooldowns.values():
            delta = (end - now).total_seconds()
            remaining = max(remaining, delta)
        return remaining

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
          5. Conditional flags unmet -> CONDITIONAL.
          6. Otherwise -> derive ARMED / COOLDOWN.
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
