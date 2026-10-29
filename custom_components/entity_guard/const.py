"""Constants for the Entity Guard integration."""

from __future__ import annotations

DOMAIN = "entity_guard"

# Entry types
ENTRY_TYPE_HUB = "hub"
ENTRY_TYPE_RULE = "rule"

# Config keys
CONF_ENTRY_TYPE = "entry_type"
CONF_RULE_NAME = "rule_name"
CONF_RULE_ID = "rule_id"
CONF_TARGET_ENTITIES = "target_entities"
CONF_MODE = "mode"
CONF_TRIGGER_STATES = "trigger_states"
CONF_TARGET_STATE = "target_state"
CONF_DELAY_SECONDS = "delay_seconds"
# dynamic_state only: per-direction delay overrides. When set, they override
# delay_seconds for the matching target direction (on/off); when unset, the rule
# falls back to delay_seconds. See RuleConfig.effective_delay.
CONF_DELAY_ON_SECONDS = "delay_on_seconds"
CONF_DELAY_OFF_SECONDS = "delay_off_seconds"
CONF_ATTRIBUTE = "attribute"
CONF_OPERATOR = "operator"
CONF_THRESHOLD = "threshold"
CONF_TARGET_VALUE = "target_value"
CONF_FLAGS = "flags"
CONF_ADD_FLAGS = "add_flags"
CONF_ADD_DEBOUNCE = "add_debounce"
CONF_CUSTOM_RATE_LIMIT = "custom_rate_limit"
CONF_RATE_LIMIT_ENABLED = "rate_limit_enabled"
CONF_FLAG_ENTITY = "entity"
CONF_FLAG_MATCH_STATE = "match_state"
CONF_DEBOUNCE_ENABLED = "debounce_enabled"
CONF_DEBOUNCE_SECONDS = "debounce_seconds"
CONF_VERIFY_ENABLED = "verify_enabled"
CONF_MAX_ENFORCEMENTS_PER_MINUTE = "max_enforcements_per_minute"
CONF_SAFETY_ACKNOWLEDGED = "safety_acknowledged"
CONF_DURATION_MINUTES = "duration_minutes"
CONF_SOURCE_ENTITY = "source_entity"
CONF_FOLLOW_INVERT = "follow_invert"
# Dynamic-attribute source: read the value from the source entity's STATE or one
# of its ATTRIBUTES. CONF_SOURCE_ATTRIBUTE holds the attribute name when
# CONF_SOURCE_VALUE_FROM == SOURCE_VALUE_FROM_ATTRIBUTE.
CONF_SOURCE_VALUE_FROM = "source_value_from"
CONF_SOURCE_ATTRIBUTE = "source_attribute"
SOURCE_VALUE_FROM_STATE = "state"
SOURCE_VALUE_FROM_ATTRIBUTE = "attribute"

# Modes — 2x2 on the fixed/dynamic axis:
#   fixed_*   = the enforced target value is one you set in config.
#   dynamic_* = the target value is read live from a source entity (Follow mode).
# state vs attribute = which property is enforced. Dynamic modes reuse the matching
# fixed mode's enforcement path verbatim; only the target value is derived live.
MODE_FIXED_STATE = "fixed_state"
MODE_FIXED_ATTRIBUTE = "fixed_attribute"
MODE_DYNAMIC_STATE = "dynamic_state"
MODE_DYNAMIC_ATTRIBUTE = "dynamic_attribute"

# Attribute mode supported attributes
ATTR_BRIGHTNESS = "brightness"
ATTR_VOLUME_LEVEL = "volume_level"
ATTR_TEMPERATURE = "temperature"
ATTR_PERCENTAGE = "percentage"
ATTR_RGB_COLOR = "rgb_color"
ATTR_COLOR_TEMP_KELVIN = "color_temp_kelvin"

NUMERIC_ATTRIBUTES = [
    ATTR_BRIGHTNESS,
    ATTR_VOLUME_LEVEL,
    ATTR_TEMPERATURE,
    ATTR_PERCENTAGE,
]

COLOR_ATTRIBUTES = [
    ATTR_RGB_COLOR,
    ATTR_COLOR_TEMP_KELVIN,
]

SUPPORTED_ATTRIBUTES = [
    *NUMERIC_ATTRIBUTES,
    *COLOR_ATTRIBUTES,
]

# Human display names for the known attributes. MUST mirror the labels in
# strings.json selector.attribute.options (the target-attribute dropdown resolves
# those via translation_key). The source-attribute picker lists LIVE/arbitrary
# attributes as SelectOptionDicts with explicit labels (which override
# translation_key), so it reuses this map for the known ones and falls back to a
# prettified key for anything live that is not listed here — keeping known attrs
# reading identically in both dropdowns. Keep in sync if a label changes.
ATTRIBUTE_DISPLAY_NAMES: dict[str, str] = {
    ATTR_BRIGHTNESS: "Brightness",
    ATTR_VOLUME_LEVEL: "Volume level",
    ATTR_TEMPERATURE: "Temperature",
    ATTR_PERCENTAGE: "Percentage",
    ATTR_RGB_COLOR: "RGB color",
    ATTR_COLOR_TEMP_KELVIN: "Color temperature (Kelvin)",
}

# Operators (no ==/!= due to float precision concerns)
OPERATOR_LT = "lt"
OPERATOR_LE = "lte"
OPERATOR_GT = "gt"
OPERATOR_GE = "gte"

SUPPORTED_OPERATORS = [OPERATOR_LT, OPERATOR_LE, OPERATOR_GT, OPERATOR_GE]

# Domains requiring safety acknowledgment. valve joins cover/lock/climate:
# unattended water actuation (irrigation, mains, radiator) carries the same
# physical-consequence risk the ack step exists to gate.
SAFETY_DOMAINS = ("cover", "lock", "climate", "valve")

# Defaults
DEFAULT_DELAY_SECONDS = 0
DEFAULT_DEBOUNCE_ENABLED = False
DEFAULT_DEBOUNCE_SECONDS = 60
DEFAULT_MAX_ENFORCEMENTS_PER_MINUTE = 10
DEFAULT_LOOP_SUPPRESS_MINUTES = 15
DEFAULT_TRIGGER_STATES: tuple[str, ...] = ("on",)
DEFAULT_TARGET_STATE = "off"

# Limits
MIN_DELAY_SECONDS = 0
MAX_DELAY_SECONDS = 86400
MIN_DEBOUNCE_SECONDS = 0
MAX_DEBOUNCE_SECONDS = 86400
MIN_RATE_LIMIT = 1
MAX_RATE_LIMIT = 600

# Behavior
STARTUP_GRACE_PERIOD_SECONDS = 60
STORE_VERSION = 1
STORE_SAVE_DELAY_SECONDS = 10
RECENTLY_ENFORCED_WINDOW_SECONDS = 30
# Hard cap on a single enforcement service call. A hung integration (offline-but-
# not-unavailable cloud device, stuck Zigbee coordinator) can make async_call block
# forever; without this bound the rule freezes while holding its reentrance lock and
# never reaches the error path (STATUS_ERROR is exception-only, a hang raises
# nothing). 30s sits above a slow-but-legit cloud call (5-15s under load) yet bounds
# a true hang. Tradeoff: too low false-fails a healthy-slow device; too high lets a
# hang hold the lock longer. Module const, not a schema field — tunable without a
# migration if a specific install runs a slower integration.
ENFORCE_TIMEOUT_SECONDS = 30

# Verify-after-enforce (opt-in, per-rule, state modes only). After a successful
# enforce the happy path confirms on the target's own state-change ACK (the
# reactive listener fires the instant it reaches desired, ~100ms after enforce);
# VERIFY_DELAY_SECONDS is the DEADLINE, not a dwell — the fallback for a device
# that accepted the command but silently ignored it (no state_changed event, so
# the event-driven confirm never lands). On deadline-with-no-ACK the engine
# issues up to VERIFY_MAX_ATTEMPTS corrective re-enforces. The deadline floors
# above Zigbee/cloud readback latency (1-3s) so a still-settling device is not
# misread as "not reached". Module consts, hardware-tuned, tunable without a
# schema change.
VERIFY_DELAY_SECONDS = 5
VERIFY_MAX_ATTEMPTS = 2

# Self-heal ceiling. A verify-enabled target that exhausts VERIFY_MAX_ATTEMPTS
# re-enters the counted enforcing cycle instead of dead-ending. After this many
# consecutive full exhaustions with no real recovery, trip loop protection
# (the dead-device kill-switch floor — fires even when the rate limit is disabled).
CONSECUTIVE_EXHAUSTION_LIMIT = 5

# Color enforcement tolerances. Small tolerances prevent repeat enforcement loops
# caused by minor device drift and lossy RGB/color-temperature conversions.
COLOR_RGB_TOLERANCE = 10
COLOR_TEMP_KELVIN_TOLERANCE = 50
MIN_COLOR_TEMP_KELVIN = 2000
MAX_COLOR_TEMP_KELVIN = 6500

# Status values
STATUS_ERROR = "error"
STATUS_DISABLED = "disabled"
STATUS_MASTER_DISABLED = "master_disabled"
STATUS_SUPPRESSED = "suppressed"
STATUS_STARTING = "starting"
STATUS_CONDITIONAL = "conditional"
STATUS_ENFORCING = "enforcing"
STATUS_PENDING = "pending"
STATUS_VERIFYING = "verifying"
STATUS_COOLDOWN = "cooldown"
STATUS_ARMED = "armed"

STATUS_VALUES = [
    STATUS_ERROR,
    STATUS_DISABLED,
    STATUS_MASTER_DISABLED,
    STATUS_SUPPRESSED,
    STATUS_STARTING,
    STATUS_CONDITIONAL,
    STATUS_ENFORCING,
    STATUS_PENDING,
    STATUS_VERIFYING,
    STATUS_COOLDOWN,
    STATUS_ARMED,
]

# Error tracking
ERROR_THRESHOLD = 3
# Number of consecutive successful enforcements required to auto-recover from STATUS_ERROR.
ERROR_RECOVERY_SUCCESS_THRESHOLD = 3

# Skip reason emitted when the enforcement service call itself raised (vs a
# genuine skip). _fire_skipped routes this through the same path as real skips,
# but the per-target outcome classifies it as "failed" not "skipped" — kept as a
# shared const so the producer (except block) and that classifier can't drift.
REASON_SERVICE_CALL_FAILED = "service_call_failed"
# Verify-after-enforce exhausted all corrective attempts and the target still never
# reached desired — the genuine "cannot be reached". Classified "failed" in
# target_outcomes (the real failure signal the visibility feature otherwise only got
# from exception-fails). Shared const so the verify writer and any reader can't drift.
REASON_UNREACHABLE_AFTER_RETRIES = "unreachable_after_retries"

# Domain service map for state mode enforcement.
# Shape: {domain: {target_state: "service.name"}}. Each entry is a parameterless
# service — the call carries only {entity_id}. States that need a DATA PAYLOAD
# (hvac_mode=, operation_mode=, option=) live in DOMAIN_MODE_SERVICE_MAP instead,
# because their payload value is the runtime target_state, not a static string.
DOMAIN_SERVICE_MAP: dict[str, dict[str, str]] = {
    "light": {"on": "light.turn_on", "off": "light.turn_off"},
    "switch": {"on": "switch.turn_on", "off": "switch.turn_off"},
    "lock": {"locked": "lock.lock", "unlocked": "lock.unlock"},
    "cover": {
        "open": "cover.open_cover",
        "closed": "cover.close_cover",
        "stopped": "cover.stop_cover",
    },
    "media_player": {
        "on": "media_player.turn_on",
        "off": "media_player.turn_off",
        "playing": "media_player.media_play",
        "paused": "media_player.media_pause",
        "idle": "media_player.media_stop",
    },
    "climate": {"off": "climate.turn_off"},
    "fan": {"on": "fan.turn_on", "off": "fan.turn_off"},
    "input_boolean": {"on": "input_boolean.turn_on", "off": "input_boolean.turn_off"},
    "vacuum": {
        "cleaning": "vacuum.start",
        "docked": "vacuum.return_to_base",
        "returning": "vacuum.return_to_base",
        "paused": "vacuum.pause",
    },
    "valve": {"open": "valve.open_valve", "closed": "valve.close_valve"},
    "lawn_mower": {"mowing": "lawn_mower.start_mowing", "docked": "lawn_mower.dock"},
}

# Mode-style domains: ONE service for the whole domain, the target_state is passed
# as a DATA KWARG value (e.g. climate heat/cool/auto → climate.set_hvac_mode
# hvac_mode=<state>). Shape: {domain: (service, kwarg)}. Resolved AFTER
# DOMAIN_SERVICE_MAP, so a domain can keep a parameterless entry there (climate
# "off" → climate.turn_off) and fall through to its mode service for every other
# state. The kwarg value is always the runtime target_state, which is why these
# can't be baked into the static-payload DOMAIN_SERVICE_MAP.
DOMAIN_MODE_SERVICE_MAP: dict[str, tuple[str, str]] = {
    "climate": ("climate.set_hvac_mode", "hvac_mode"),
    "water_heater": ("water_heater.set_operation_mode", "operation_mode"),
    "input_select": ("input_select.select_option", "option"),
}

# Per-domain ACTIONABLE target state values for trigger/target dropdowns. Only
# states this integration can actually DRIVE are listed — a state here resolves to
# a real service via DOMAIN_SERVICE_MAP, DOMAIN_MODE_SERVICE_MAP, or the on/off
# fallback. Transitional states (locking/opening/arming…), read-only domains
# (binary_sensor/person/device_tracker/timer have no setter service), and the
# deferred alarm_control_panel domain are deliberately omitted so the UI never
# advertises a target it would silently skip (no_service_mapping).
# custom_value=True remains on the selectors so an expert can still type an
# unlisted state. 'unavailable'/'unknown' are excluded — enforcement requires a
# reachable entity.
DOMAIN_STATE_OPTIONS: dict[str, list[str]] = {
    "light": ["on", "off"],
    "switch": ["on", "off"],
    "input_boolean": ["on", "off"],
    "fan": ["on", "off"],
    "lock": ["locked", "unlocked"],
    "cover": ["open", "closed", "stopped"],
    "media_player": ["playing", "paused", "idle", "off", "on"],
    "climate": ["off", "heat", "cool", "auto", "heat_cool", "fan_only", "dry"],
    "input_select": [],  # state is user-defined option list
    "vacuum": ["cleaning", "docked", "returning", "paused"],
    "humidifier": ["on", "off"],
    "water_heater": ["off", "eco", "electric", "gas", "heat_pump", "high_demand"],
    "script": ["on", "off"],
    "automation": ["on", "off"],
    "remote": ["on", "off"],
    "siren": ["on", "off"],
    "valve": ["open", "closed"],
    "lawn_mower": ["mowing", "docked"],
}

# Fallback when no recognized domain in target list.
FALLBACK_STATE_OPTIONS = ["on", "off"]

# Drivable target domains — the keys of DOMAIN_STATE_OPTIONS, i.e. every domain
# this integration can actually enforce (each resolves to a real service via
# DOMAIN_SERVICE_MAP, DOMAIN_MODE_SERVICE_MAP, or the on/off fallback). Read-only
# domains (binary_sensor, sensor, person, device_tracker, timer) are absent, so
# the target-entity picker and the submit validation reject them — a read-only
# entity can never be driven and would only ever skip (no_service_mapping).
TARGET_DOMAINS: list[str] = sorted(DOMAIN_STATE_OPTIONS)

# Follow mode: which LIVE source states count as "on" (see DOMAIN_STATE_OPTIONS /
# DOMAIN_SERVICE_MAP above for the domain vocab this draws from). Any live state NOT
# in this set is treated as OFF. Ambiguous states are deliberately pinned OFF
# (paused/idle/standby/docked/stopped). Offline (STATE_UNAVAILABLE/STATE_UNKNOWN or
# a missing source) is handled separately by the engine as not-triggered — it is NOT
# "off". Flat set by design: a per-domain table is deferred until a real domain's
# semantics require it (ponytail). Engine-only; the config flow does not import this.
FOLLOW_ON_STATES = frozenset(
    {
        "on",
        "home",
        "open",
        "unlocked",
        "playing",
        "active",
        "cleaning",
        "mowing",
        "true",
        "1",
    }
)

# States explicitly forbidden as trigger or target — entity is offline / state is undefined.
FORBIDDEN_STATES = frozenset({"unavailable", "unknown", "none", ""})

# Attribute clamp service map
ATTRIBUTE_SERVICE_MAP: dict[str, tuple[str, str]] = {
    ATTR_BRIGHTNESS: ("light.turn_on", "brightness"),
    ATTR_VOLUME_LEVEL: ("media_player.volume_set", "volume_level"),
    ATTR_TEMPERATURE: ("climate.set_temperature", "temperature"),
    ATTR_PERCENTAGE: ("fan.set_percentage", "percentage"),
    ATTR_RGB_COLOR: ("light.turn_on", "rgb_color"),
    ATTR_COLOR_TEMP_KELVIN: ("light.turn_on", "color_temp_kelvin"),
}

# Reverse index: domain -> list[attr] (built from ATTRIBUTE_SERVICE_MAP service prefix).
# Used by config flow to show only attributes valid for the chosen target entities.
ATTRIBUTES_BY_DOMAIN: dict[str, list[str]] = {}
for _attr, (_service, _key) in ATTRIBUTE_SERVICE_MAP.items():
    _domain = _service.split(".", 1)[0]
    ATTRIBUTES_BY_DOMAIN.setdefault(_domain, []).append(_attr)
del _attr, _service, _key, _domain

# Events
EVENT_ENFORCED = "entity_guard_enforced"
EVENT_SKIPPED = "entity_guard_skipped"
EVENT_LOOP_DETECTED = "entity_guard_loop_detected"
EVENT_SUPPRESSED = "entity_guard_suppressed"
# Fired only when verify is enabled, only on a confirmed success (the verify
# re-read reached desired). One pulse per enforcement cycle at confirm time —
# never on verify-off, never on a fail/suppress/kill-switch. A clean automation
# gate: wait for this instead of EVENT_ENFORCED (which is provisional under verify).
EVENT_VERIFIED = "entity_guard_enforced_verified"
# The failure twin of EVENT_VERIFIED. Fired only when verify is enabled, only
# when the target never reached desired after VERIFY_MAX_ATTEMPTS corrective
# re-enforces (service call did NOT raise — a silent non-compliance the plain
# skipped/error paths can't see). One pulse per fresh exhaustion streak, at the
# same count==1 boundary as the skipped event, so a dead device doesn't flood
# the bus. A recovery resets the streak, so a later death re-fires.
EVENT_UNREACHABLE = "entity_guard_enforced_unreachable"

# Services
SERVICE_SUPPRESS = "suppress"
SERVICE_UNSUPPRESS = "unsuppress"
SERVICE_LIST_RULES = "list_rules"
SERVICE_CLEAR_HISTORY = "clear_history"
SERVICE_PANIC_STOP = "panic_stop"

# Storage
STORAGE_KEY = "entity_guard"


# Signal name helpers — single source of truth used by all platforms.
def signal_rule_update(rule_id: str) -> str:
    """Return dispatcher signal name for a per-rule status update."""
    return f"entity_guard_rule_update_{rule_id}"


def signal_master() -> str:
    """Return dispatcher signal name for hub master switch updates."""
    return "entity_guard_master_update"


def has_safety_target(entity_ids: list) -> bool:
    """Return True if any entity_id is in a safety-sensitive domain."""
    return any(
        isinstance(e, str) and e.split(".", 1)[0] in SAFETY_DOMAINS for e in entity_ids
    )


def entry_has_safety_target(entry) -> bool:
    """Return True if a config entry targets a safety-sensitive domain."""
    # Merge like parse_rule_config (options wins): an options-flow edit of the target
    # entities persists to entry.options, so reading data alone (which keeps the
    # creation-time entities) would miss a lock/cover added after creation and skip
    # the safety-status sensor.
    merged = {**entry.data, **(entry.options or {})}
    return has_safety_target(merged.get(CONF_TARGET_ENTITIES) or [])
