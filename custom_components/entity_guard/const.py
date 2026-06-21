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
CONF_MAX_ENFORCEMENTS_PER_MINUTE = "max_enforcements_per_minute"
CONF_SAFETY_ACKNOWLEDGED = "safety_acknowledged"
CONF_DURATION_MINUTES = "duration_minutes"

# Modes
MODE_STATE = "state"
MODE_ATTRIBUTE = "attribute"

# Attribute mode supported attributes
ATTR_BRIGHTNESS = "brightness"
ATTR_VOLUME_LEVEL = "volume_level"
ATTR_TEMPERATURE = "temperature"
ATTR_PERCENTAGE = "percentage"

SUPPORTED_ATTRIBUTES = [
    ATTR_BRIGHTNESS,
    ATTR_VOLUME_LEVEL,
    ATTR_TEMPERATURE,
    ATTR_PERCENTAGE,
]

# Operators (no ==/!= due to float precision concerns)
OPERATOR_LT = "lt"
OPERATOR_LE = "lte"
OPERATOR_GT = "gt"
OPERATOR_GE = "gte"

SUPPORTED_OPERATORS = [OPERATOR_LT, OPERATOR_LE, OPERATOR_GT, OPERATOR_GE]

# Domains requiring safety acknowledgment
SAFETY_DOMAINS = ("cover", "lock", "climate")

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

# Status values
STATUS_ERROR = "error"
STATUS_DISABLED = "disabled"
STATUS_MASTER_DISABLED = "master_disabled"
STATUS_SUPPRESSED = "suppressed"
STATUS_STARTING = "starting"
STATUS_CONDITIONAL = "conditional"
STATUS_ENFORCING = "enforcing"
STATUS_PENDING = "pending"
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
    STATUS_COOLDOWN,
    STATUS_ARMED,
]

# Error tracking
ERROR_THRESHOLD = 3
# Number of consecutive successful enforcements required to auto-recover from STATUS_ERROR.
ERROR_RECOVERY_SUCCESS_THRESHOLD = 3

# Domain service map for state mode enforcement
DOMAIN_SERVICE_MAP: dict[str, dict[str, str]] = {
    "light": {"on": "light.turn_on", "off": "light.turn_off"},
    "switch": {"on": "switch.turn_on", "off": "switch.turn_off"},
    "lock": {"locked": "lock.lock", "unlocked": "lock.unlock"},
    "cover": {"open": "cover.open_cover", "closed": "cover.close_cover"},
    "media_player": {"on": "media_player.turn_on", "off": "media_player.turn_off"},
    "climate": {"off": "climate.turn_off"},
    "fan": {"on": "fan.turn_on", "off": "fan.turn_off"},
    "input_boolean": {"on": "input_boolean.turn_on", "off": "input_boolean.turn_off"},
}

# Per-domain typical state values for trigger/target dropdowns.
# custom_value=True remains on the selectors so users can still type unlisted states.
# 'unavailable'/'unknown' are intentionally excluded — enforcement requires a reachable entity.
DOMAIN_STATE_OPTIONS: dict[str, list[str]] = {
    "light": ["on", "off"],
    "switch": ["on", "off"],
    "input_boolean": ["on", "off"],
    "binary_sensor": ["on", "off"],
    "fan": ["on", "off"],
    "lock": ["locked", "unlocked", "locking", "unlocking", "jammed"],
    "cover": ["open", "closed", "opening", "closing", "stopped"],
    "media_player": ["playing", "paused", "idle", "off", "on", "standby"],
    "climate": ["off", "heat", "cool", "auto", "heat_cool", "fan_only", "dry"],
    "alarm_control_panel": [
        "disarmed",
        "armed_home",
        "armed_away",
        "armed_night",
        "armed_vacation",
        "pending",
        "triggered",
        "arming",
        "disarming",
    ],
    "person": ["home", "not_home"],
    "device_tracker": ["home", "not_home"],
    "input_select": [],  # state is user-defined option list
    "vacuum": ["cleaning", "docked", "paused", "idle", "returning", "error"],
    "humidifier": ["on", "off"],
    "water_heater": ["off", "eco", "electric", "gas", "heat_pump", "high_demand"],
    "timer": ["idle", "active", "paused"],
    "script": ["on", "off"],
    "automation": ["on", "off"],
    "remote": ["on", "off"],
    "siren": ["on", "off"],
    "valve": ["open", "closed", "opening", "closing"],
    "lawn_mower": ["mowing", "docked", "paused", "error"],
}

# Fallback when no recognized domain in target list.
FALLBACK_STATE_OPTIONS = ["on", "off"]

# States explicitly forbidden as trigger or target — entity is offline / state is undefined.
FORBIDDEN_STATES = frozenset({"unavailable", "unknown", "none", ""})

# Attribute clamp service map
ATTRIBUTE_SERVICE_MAP: dict[str, tuple[str, str]] = {
    ATTR_BRIGHTNESS: ("light.turn_on", "brightness"),
    ATTR_VOLUME_LEVEL: ("media_player.volume_set", "volume_level"),
    ATTR_TEMPERATURE: ("climate.set_temperature", "temperature"),
    ATTR_PERCENTAGE: ("fan.set_percentage", "percentage"),
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
    entity_ids = entry.data.get(CONF_TARGET_ENTITIES) or entry.options.get(
        CONF_TARGET_ENTITIES, []
    )
    return has_safety_target(entity_ids or [])
