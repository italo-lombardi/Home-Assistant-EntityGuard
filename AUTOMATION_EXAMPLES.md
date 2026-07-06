# Entity Guard — Automation Examples

All examples use `rule_id` (the stable config-entry ID) rather than `rule_name` so renames don't break automations.  
To find a rule's ID: enable `sensor.<rule>_rule_id` in the entity registry — its state is the ID to copy.

---

## Notifications

### Notify when a specific rule enforces

```yaml
automation:
  alias: Notify — balcony light enforced
  trigger:
    - platform: event
      event_type: entity_guard_enforced
      event_data:
        rule_id: "01KSXDM1GN9WGQCSTFAX0Y1HNS"
  action:
    - service: notify.mobile_app_my_phone
      data:
        message: >
          {{ trigger.event.data.rule_name }} enforced
          {{ trigger.event.data.entity_id }} → {{ trigger.event.data.target }}
```

### Notify when any rule enforces (all rules)

```yaml
automation:
  alias: Notify — any Entity Guard enforcement
  trigger:
    - platform: event
      event_type: entity_guard_enforced
  action:
    - service: notify.mobile_app_my_phone
      data:
        message: >
          [Entity Guard] {{ trigger.event.data.rule_name }}
          enforced {{ trigger.event.data.entity_id }}
```

### Notify when a rule enters error state

Fires when 3+ consecutive enforcement failures occur (target unavailable, service error, etc.).

```yaml
automation:
  alias: Notify — rule error
  trigger:
    - platform: state
      entity_id: sensor.balcony_light_off_rule_status
      to: error
  action:
    - service: notify.mobile_app_my_phone
      data:
        message: >
          Entity Guard rule "{{ state_attr('sensor.balcony_light_off_rule_status', 'friendly_name') }}"
          is in ERROR state. Check the entity is reachable.
```

### Notify when loop protection fires

```yaml
automation:
  alias: Notify — loop detected
  trigger:
    - platform: event
      event_type: entity_guard_loop_detected
  action:
    - service: notify.mobile_app_my_phone
      data:
        title: "Entity Guard loop detected"
        message: >
          Rule "{{ trigger.event.data.rule_name }}" hit its rate limit on
          {{ trigger.event.data.entity_id }} ({{ trigger.event.data.count }} enforcements/min).
          Rule auto-suppressed for {{ trigger.event.data.suppress_minutes }} min.
```

---

## Rule control

### Disable a rule during a scheduled window (e.g. guests overnight)

```yaml
automation:
  - alias: EG — disable kids TV rule at night
    trigger:
      - platform: time
        at: "22:00:00"
    action:
      - service: switch.turn_off
        target:
          entity_id: switch.kids_tv_off_rule_enabled

  - alias: EG — re-enable kids TV rule in the morning
    trigger:
      - platform: time
        at: "07:00:00"
    action:
      - service: switch.turn_on
        target:
          entity_id: switch.kids_tv_off_rule_enabled
```

### Enforce once — disable rule after first enforcement

Useful when you want a rule to fire exactly once (e.g. turn off the balcony light after occupancy clears for the first time) and stay dormant until you re-arm it manually.

Trigger on the `entity_guard_enforced` event and turn off the rule's enabled switch. The rule stays disabled until you flip the switch back on — either manually or via another automation.

```yaml
automation:
  alias: EG — balcony light rule fires once
  trigger:
    - platform: event
      event_type: entity_guard_enforced
      event_data:
        rule_id: "01KSXDM1GN9WGQCSTFAX0Y1HNS"   # replace with your rule_id
  action:
    - service: switch.turn_off
      target:
        entity_id: switch.balcony_light_off_rule_enabled
```

To re-arm automatically the next morning (so it fires once per day):

```yaml
automation:
  alias: EG — re-arm balcony rule at sunrise
  trigger:
    - platform: sun
      event: sunrise
  action:
    - service: switch.turn_on
      target:
        entity_id: switch.balcony_light_off_rule_enabled
```

To re-arm when occupancy returns (so it fires once per occupancy session):

```yaml
automation:
  alias: EG — re-arm balcony rule when occupied
  trigger:
    - platform: state
      entity_id: binary_sensor.balcony_occupancy
      to: "on"
  action:
    - service: switch.turn_on
      target:
        entity_id: switch.balcony_light_off_rule_enabled
```

> **Tip:** The rule switch entity id follows the pattern `switch.<rule_name_slug>_enabled`. If unsure, find it in **Settings → Devices & Services → Entity Guard → your rule**.

### Suppress a rule when someone arrives home

```yaml
automation:
  alias: EG — suppress diffuser rule when Alice arrives
  trigger:
    - platform: state
      entity_id: person.alice
      to: home
  action:
    - service: entity_guard.suppress
      data:
        entry_id: "01KSXDM1GN9WGQCSTFAX0Y1HNS"
        duration_minutes: 60
```

### Unsuppress a rule manually via automation

```yaml
automation:
  alias: EG — unsuppress all rules when leaving home
  trigger:
    - platform: state
      entity_id: person.alice
      from: home
  action:
    - service: entity_guard.unsuppress
      data:
        entry_id: "01KSXDM1GN9WGQCSTFAX0Y1HNS"
```

### Disable all rules temporarily (panic stop)

```yaml
automation:
  alias: EG — panic stop via input button
  trigger:
    - platform: state
      entity_id: input_button.emergency_stop
  action:
    - service: entity_guard.panic_stop
```

---

## Reacting to enforcement

### Trigger on recently_enforced sensor (simple binary trigger)

`binary_sensor.<rule>_recently_enforced` stays ON for 30 seconds after enforcement; pulses off→on on repeated enforcements so each one re-triggers.

The sensor exposes these attributes — no need for a separate event listener:

| Attribute | Example value |
|-----------|---------------|
| `rule_name` | `"Balcony Light OFF"` |
| `target_entities` | `["light.balcony_light"]` |
| `target_entity_names` | `["Balcony Light"]` |
| `target` | `"off"` (state mode) or `64` (attribute mode) |
| `delay_seconds` | `0` |

```yaml
automation:
  alias: React to balcony rule enforcement
  trigger:
    - platform: state
      entity_id: binary_sensor.balcony_light_off_rule_recently_enforced
      to: "on"
  action:
    - service: notify.mobile_app_my_phone
      data:
        message: >
          {{ state_attr('binary_sensor.balcony_light_off_rule_recently_enforced', 'rule_name') }}
          enforced {{ state_attr('binary_sensor.balcony_light_off_rule_recently_enforced', 'target_entities') | join(', ') }}
          → {{ state_attr('binary_sensor.balcony_light_off_rule_recently_enforced', 'target') }}
```

To handle **any rule** with a single automation, listen to the event instead (see [Notify when any rule enforces](#notify-when-any-rule-enforces-all-rules)).

### Flash a light when any rule enforces

```yaml
automation:
  alias: EG — flash indicator light on enforcement
  trigger:
    - platform: event
      event_type: entity_guard_enforced
  action:
    - service: light.turn_on
      target:
        entity_id: light.hallway_indicator
      data:
        flash: short
```

### Log all enforcements to a persistent notification

```yaml
automation:
  alias: EG — log enforcements to persistent notification
  trigger:
    - platform: event
      event_type: entity_guard_enforced
  action:
    - service: persistent_notification.create
      data:
        notification_id: "eg_enforcement_log"
        title: "Entity Guard — last enforcement"
        message: >
          {{ now().strftime('%H:%M:%S') }}
          {{ trigger.event.data.rule_name }}
          → {{ trigger.event.data.entity_id }} set to {{ trigger.event.data.target }}
```

### Increment a counter on every enforcement (for dashboards)

```yaml
# configuration.yaml
counter:
  entity_guard_total:
    name: "Entity Guard enforcements"
    initial: 0
    step: 1

# automation
automation:
  alias: EG — increment enforcement counter
  trigger:
    - platform: event
      event_type: entity_guard_enforced
  action:
    - service: counter.increment
      target:
        entity_id: counter.entity_guard_total
```

---

## Conditional rules via automations

### Enable a rule only when night mode is active

```yaml
automation:
  alias: EG — enable brightness cap at night
  trigger:
    - platform: state
      entity_id: input_boolean.night_mode
  action:
    - choose:
        - conditions:
            - condition: state
              entity_id: input_boolean.night_mode
              state: "on"
          sequence:
            - service: switch.turn_on
              target:
                entity_id: switch.bedroom_brightness_cap_enabled
        default:
          - service: switch.turn_off
            target:
              entity_id: switch.bedroom_brightness_cap_enabled
```

> **Tip:** For condition-based activation, prefer using **Flags** in the rule configuration instead — they are evaluated atomically and don't require a separate automation. Use the above pattern only when the condition involves complex logic that flags cannot express.

---

## Event payload reference

All `entity_guard_enforced` event fields:

| Field | Example | Notes |
|-------|---------|-------|
| `rule_id` | `01KSXDM1GN9WGQCSTFAX0Y1HNS` | Stable config-entry ID. Use this for filtering. |
| `rule_name` | `Balcony Light OFF` | Display name — can change if rule is renamed |
| `entity_id` | `light.balcony_light` | The entity that was enforced |
| `domain` | `light` | Domain of the enforced entity |
| `trigger` | `state` | `state` or `attribute` |
| `target` | `off` | Target state or attribute value that was set |
| `reason` | `rule_match` | Always `rule_match` for normal enforcement |
| `user_id` | `abc123` or `null` | HA user who triggered the change (if identifiable) |
