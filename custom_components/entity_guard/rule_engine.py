"""Per-rule runtime engine for the Entity Guard integration."""

from __future__ import annotations

import asyncio
import bisect
import logging
from datetime import datetime, timedelta
from typing import Any, Callable

from homeassistant.components import persistent_notification
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Context, Event, HomeAssistant, callback
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_change,
)
from homeassistant.util import dt as dt_util

from .const import (
    ATTRIBUTE_SERVICE_MAP,
    DEFAULT_LOOP_SUPPRESS_MINUTES,
    DOMAIN,
    DOMAIN_SERVICE_MAP,
    ERROR_THRESHOLD,
    EVENT_ENFORCED,
    EVENT_LOOP_DETECTED,
    EVENT_SKIPPED,
    EVENT_SUPPRESSED,
    MODE_ATTRIBUTE,
    MODE_STATE,
    OPERATOR_GE,
    OPERATOR_GT,
    OPERATOR_LE,
    OPERATOR_LT,
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
    ) -> None:
        """Initialize the engine."""
        self._hass = hass
        self._config = config
        self._store = store
        self._master_enabled_getter = master_enabled_getter
        self._state: RuleRuntimeState = RuleRuntimeState()
        self._unsub_callbacks: list[Callable[[], None]] = []
        self._pending_enforcements: dict[str, Callable[[], None]] = {}
        self._pending_eval_tasks: dict[str, asyncio.Task] = {}
        self._startup_complete = False
        self._is_unloaded: bool = False
        self._current_status: str = STATUS_STARTING
        self._flag_entity_ids: frozenset[str] = frozenset(
            f.entity for f in config.flags
        )
        self._cooldown_broadcast_unsubs: dict[str, Callable[[], None]] = {}

    @property
    def config(self) -> RuleConfig:
        """Return the rule's static config."""
        return self._config

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

        watched: set[str] = set(self._config.target_entities)
        watched.update(f.entity for f in self._config.flags)
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

        self._broadcast_status()

    async def async_unload(self) -> None:
        """Cancel listeners and pending enforcement timers."""
        self._is_unloaded = True
        _LOGGER.debug("Engine unload: rule=%s", self._config.name)
        for unsub in self._unsub_callbacks:
            try:
                unsub()
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Listener unsubscribe failed", exc_info=True)
        self._unsub_callbacks.clear()

        for cancel in list(self._pending_enforcements.values()):
            try:
                cancel()
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Pending enforcement cancel failed", exc_info=True)
        self._pending_enforcements.clear()

        for task in list(self._pending_eval_tasks.values()):
            task.cancel()
        self._pending_eval_tasks.clear()

        for cancel in list(self._cooldown_broadcast_unsubs.values()):
            try:
                cancel()
            except Exception:  # noqa: BLE001
                pass
        self._cooldown_broadcast_unsubs.clear()

        self._persist()
        await self._store.async_save_now()

    @callback
    def _handle_state_event(self, event: Event) -> None:
        """State change listener — schedules async evaluation, deduplicating per entity."""
        entity_id = event.data.get("entity_id")
        new_state = event.data.get("new_state")
        if entity_id is None:
            return
        self._schedule_eval_task(entity_id, new_state)

    def _schedule_eval_task(self, entity_id: str, new_state: Any) -> None:
        """Schedule async_evaluate for entity_id, cancelling any prior pending task."""
        existing = self._pending_eval_tasks.pop(entity_id, None)
        if existing is not None and not existing.done():
            existing.cancel()
        task = self._hass.async_create_task(self.async_evaluate(entity_id, new_state))
        if task is not None:
            self._pending_eval_tasks[entity_id] = task
            task.add_done_callback(
                lambda t, eid=entity_id: self._pending_eval_tasks.pop(eid, None)
            )

    @callback
    def _handle_midnight(self, _now: datetime) -> None:
        """Zero today counter at HA-local midnight."""
        self._state.enforcement_count_today = 0
        self._state.today_reset_date = dt_util.now().date()
        self._persist()
        self._broadcast_status()

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

        for entity_id in self._config.target_entities:
            current = self._hass.states.get(entity_id)
            self._schedule_eval_task(entity_id, current)

    def _maybe_reset_today_counter(self) -> None:
        """Reset today counter if persisted reset date is stale."""
        today = dt_util.now().date()
        if self._state.today_reset_date != today:
            self._state.enforcement_count_today = 0
            self._state.today_reset_date = today

    async def async_evaluate(self, entity_id: str, new_state: Any) -> None:
        """Evaluate a state change against the rule and enforce if appropriate."""
        now = dt_util.now()
        self._prune_expired_cooldowns(now)
        if not self._startup_complete and entity_id in self._config.target_entities:
            # During grace, ignore target state events; flag changes still re-evaluate.
            if entity_id not in self._flag_entity_ids:
                return

        if self._current_status == STATUS_ERROR:
            return

        if not self._state.enabled or not self._master_enabled_getter():
            self._apply_idle_status()
            return

        if self._state.suppressed_until and self._state.suppressed_until > now:
            self._set_status(STATUS_SUPPRESSED)
            return
        if self._state.suppressed_until and self._state.suppressed_until <= now:
            self._state.suppressed_until = None
            self._state.suppression_reason = None
            self._persist()

        if not self._flags_match():
            self._set_status(STATUS_CONDITIONAL)
            self._cancel_pending_for_entities(self._config.target_entities)
            return

        flag_entity_ids = self._flag_entity_ids

        # Flag entity change: flags just became satisfied — sweep all targets.
        if entity_id in flag_entity_ids:
            self._set_status(self._derive_armed_or_cooldown(now))
            for target in self._config.target_entities:
                if target == entity_id:
                    continue  # handled below as a target if also a target entity
                current = self._hass.states.get(target)
                if current is not None and self._is_triggered(target, current):
                    self._schedule_eval_task(target, current)
            # If entity is flag-only, stop here; if also a target, fall through.
            if entity_id not in self._config.target_entities:
                return

        triggered = self._is_triggered(entity_id, new_state)

        if not triggered:
            # State no longer matches; cancel any pending delayed enforcement.
            self._cancel_pending(entity_id)
            self._set_status(self._derive_armed_or_cooldown(now))
            return

        if self._config.debounce_enabled and self._in_cooldown(entity_id, now):
            self._set_status(STATUS_COOLDOWN)
            return

        if self._config.delay_seconds > 0:
            self._schedule_delayed_enforcement(entity_id)
            self._set_status(STATUS_PENDING)
            return

        await self._enforce(entity_id)

    def _flags_match(self) -> bool:
        """Return True only if every configured flag entity matches its target state."""
        if not self._config.flags:
            return True
        for flag in self._config.flags:
            state_obj = self._hass.states.get(flag.entity)
            if state_obj is None:
                return False
            if state_obj.state in (STATE_UNAVAILABLE, STATE_UNKNOWN, None):
                return False
            if state_obj.state != flag.match_state:
                return False
        return True

    def _is_triggered(self, entity_id: str, new_state: Any) -> bool:
        """Return True if the rule's trigger condition holds for entity_id's state."""
        if new_state is None:
            return False

        if self._config.mode == MODE_STATE:
            if new_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                return False
            return new_state.state in self._config.trigger_states

        if self._config.mode == MODE_ATTRIBUTE:
            attr = self._config.attribute
            op = self._config.operator
            threshold = self._config.threshold
            if attr is None or op is None or threshold is None:
                return False
            value = (
                new_state.attributes.get(attr)
                if hasattr(new_state, "attributes")
                else None
            )
            try:
                value_f = float(value)
            except (TypeError, ValueError):
                return False
            return _compare(value_f, op, threshold)

        return False

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

    def _prune_expired_cooldowns(self, now: datetime) -> None:
        """Remove expired per-entity cooldown entries."""
        expired = [eid for eid, end in self._state.cooldowns.items() if end <= now]
        for eid in expired:
            del self._state.cooldowns[eid]

    def _schedule_delayed_enforcement(self, entity_id: str) -> None:
        """Arm a delayed enforcement; cancels any prior pending one for this entity."""
        self._cancel_pending(entity_id)

        async def _fire(_now: datetime) -> None:
            if self._is_unloaded:
                return
            self._pending_enforcements.pop(entity_id, None)
            current = self._hass.states.get(entity_id)
            if current is None or not self._is_triggered(entity_id, current):
                return
            if not self._flags_match():
                return
            await self._enforce(entity_id)

        @callback
        def _fire_cb(now: datetime) -> None:
            # async_call_later expects a sync callback; bridge to the coroutine.
            self._hass.async_create_task(_fire(now))

        cancel = async_call_later(self._hass, self._config.delay_seconds, _fire_cb)
        self._pending_enforcements[entity_id] = cancel

    def _cancel_pending(self, entity_id: str) -> None:
        """Cancel any pending delayed enforcement for entity_id."""
        cancel = self._pending_enforcements.pop(entity_id, None)
        if cancel is not None:
            try:
                cancel()
            except Exception:  # noqa: BLE001
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

            self._set_status(STATUS_ENFORCING)

            service_call = self._resolve_service(entity_id)
            if service_call is None:
                self._hass.bus.async_fire(
                    EVENT_SKIPPED,
                    {
                        "rule_id": self._config.unique_id,
                        "rule_name": self._config.name,
                        "entity_id": entity_id,
                        "reason": "no_service_mapping",
                    },
                )
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
            ctx = Context(user_id=user_id) if user_id else None
            try:
                await self._hass.services.async_call(
                    domain, service, data, blocking=True, context=ctx
                )
            except Exception as err:  # noqa: BLE001
                # Spec: target unavailable / service errors → skip silently + debug log.
                self._hass.bus.async_fire(
                    EVENT_SKIPPED,
                    {
                        "rule_id": self._config.unique_id,
                        "rule_name": self._config.name,
                        "entity_id": entity_id,
                        "reason": "service_call_failed",
                        "error": str(err),
                    },
                )
                _LOGGER.warning(
                    "Enforcement failed for %s on rule '%s': %s",
                    entity_id,
                    self._config.name,
                    err,
                )
                self._state.consecutive_errors += 1
                self._state.last_error = str(err)
                self._persist()
                if self._state.consecutive_errors >= ERROR_THRESHOLD:
                    self._set_status(STATUS_ERROR)
                else:
                    self._set_status(self._derive_armed_or_cooldown(now))
                return

            self._state.enforcement_count_today += 1
            self._state.enforcement_count_total += 1
            self._state.last_enforced = now
            self._state.rate_limit_window.append(now)
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
                    "entity_id": entity_id,
                    "domain": entity_id.split(".", 1)[0],
                    "trigger": self._config.mode,
                    "target": self._config.target_state
                    if self._config.mode == MODE_STATE
                    else self._config.target_value,
                    "reason": "rule_match",
                    "user_id": user_id,
                },
            )

            self._persist()

        if self._config.debounce_enabled and self._config.debounce_seconds > 0:
            self._set_status(STATUS_COOLDOWN)
            cooldown_end = self._state.cooldowns.get(entity_id)
            if cooldown_end is not None:
                remaining = (cooldown_end - dt_util.now()).total_seconds()
                if remaining > 0:
                    old_unsub = self._cooldown_broadcast_unsubs.pop(entity_id, None)
                    if old_unsub is not None:
                        try:
                            old_unsub()
                        except Exception:  # noqa: BLE001
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
        else:
            self._set_status(self._derive_armed_or_cooldown(dt_util.now()))

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
        self, entity_id: str
    ) -> tuple[str, str, dict[str, Any]] | None:
        """Pick the right service call for the entity given the rule mode."""
        domain = entity_id.split(".", 1)[0]

        if self._config.mode == MODE_STATE:
            domain_map = DOMAIN_SERVICE_MAP.get(domain)
            target_state = self._config.target_state
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

        if self._config.mode == MODE_ATTRIBUTE:
            attr = self._config.attribute
            value = self._config.target_value
            if attr is None or value is None:
                return None
            mapping = ATTRIBUTE_SERVICE_MAP.get(attr)
            if mapping is None:
                return None
            full, kwarg = mapping
            svc_domain, svc_name = full.split(".", 1)
            return svc_domain, svc_name, {"entity_id": entity_id, kwarg: value}

        return None

    async def async_test_enforce(self, user_id: str | None = None) -> None:
        """Force an enforcement run against every target entity."""
        _LOGGER.info(
            "Test enforce invoked on rule '%s' (user_id=%s)",
            self._config.name,
            user_id,
        )
        for entity_id in self._config.target_entities:
            await self._enforce(entity_id, user_id=user_id, bypass_rate_limit=True)

    async def async_reset_cooldowns(self) -> None:
        """Clear all per-entity cooldowns immediately."""
        _LOGGER.info("Resetting cooldowns for rule '%s'", self._config.name)
        self._state.cooldowns.clear()
        self._persist()
        self._apply_idle_status()

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
            self._set_status(STATUS_SUPPRESSED)

    async def async_unsuppress(self) -> None:
        """Clear any active suppression."""
        _LOGGER.info("Unsuppressing rule '%s'", self._config.name)
        self._state.suppressed_until = None
        self._state.suppression_reason = None
        self._persist()
        self._apply_idle_status()

    async def async_clear_history(self) -> None:
        """Reset persisted counters and cooldowns."""
        _LOGGER.info("Clearing history for rule '%s'", self._config.name)
        self._state.cooldowns.clear()
        self._state.enforcement_count_today = 0
        self._state.enforcement_count_total = 0
        self._state.last_enforced = None
        self._state.rate_limit_window.clear()
        self._state.consecutive_errors = 0
        self._state.last_error = None
        self._store.clear_rule_history(self._config.unique_id)
        if self._current_status == STATUS_ERROR:
            self._current_status = STATUS_STARTING  # break sticky before re-derive
            self._apply_idle_status()
        else:
            self._broadcast_status()

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
                self._state.suppressed_until = None
                self._state.suppression_reason = None
        self._persist()
        self._apply_idle_status()

    @callback
    def _handle_master_changed(self) -> None:
        """Re-derive status when the master switch toggles."""
        self._apply_idle_status()

    def current_status(self) -> str:
        """Return the rule's current status string."""
        return self._current_status

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

    def cooldown_remaining_seconds(self) -> float:
        """Return the largest remaining cooldown across tracked entities."""
        now = dt_util.now()
        remaining = 0.0
        for end in self._state.cooldowns.values():
            delta = (end - now).total_seconds()
            if delta > remaining:
                remaining = delta
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
          1. STATUS_ERROR sticky -> ERROR.
          2. Master switch off -> MASTER_DISABLED.
          3. Per-rule disabled -> DISABLED.
          4. Active suppression -> SUPPRESSED.
          5. Conditional flags unmet -> CONDITIONAL.
          6. Otherwise -> derive ARMED / COOLDOWN.
        """
        if self._current_status == STATUS_ERROR:
            return STATUS_ERROR
        if not self._master_enabled_getter():
            return STATUS_MASTER_DISABLED
        if not self._state.enabled:
            return STATUS_DISABLED
        if now is None:
            now = dt_util.now()
        if self._state.suppressed_until and self._state.suppressed_until > now:
            return STATUS_SUPPRESSED
        if not self._flags_match():
            return STATUS_CONDITIONAL
        return self._derive_armed_or_cooldown(now)

    def _apply_idle_status(self, now: datetime | None = None) -> None:
        """Compute and broadcast the idle status, cancelling pending if disabled."""
        status = self._derive_idle_status(now)
        if status in (STATUS_DISABLED, STATUS_MASTER_DISABLED):
            self._cancel_pending_for_entities(self._config.target_entities)
        self._set_status(status)

    def _broadcast_status(self) -> None:
        """Tell platforms to refresh."""
        async_dispatcher_send(self._hass, signal_for_rule(self._config.unique_id))

    def _persist(self) -> None:
        """Push runtime state to the store (debounced internally)."""
        blob = EntityGuardStore.runtime_to_blob(self._state)
        self._store.set_rule_state(self._config.unique_id, blob)


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
