# Entity Guard for Home Assistant

Declarative state-enforcement rules. Bind one or more entities to a rule that defines a trigger condition and a desired target state — Entity Guard enforces it automatically, with cooldowns, suppression, rate limiting, and a custom dashboard card.

## Features

- **Multi-entity rules** — one rule binds 1+ target entities, each evaluated independently
- **Two modes** — `state` (force entity to a target state) and `attribute` (clamp brightness, volume, temperature, or percentage against a threshold)
- **Flag conditions** — optional `(entity, match_state)` AND-list; rule arms only when all flags match
- **Cooldown / debounce** — per-entity cooldown after enforcement to avoid loop fights
- **Rate limiting** — per-rule cap on enforcements per minute; exceeding it auto-suppresses the rule
- **Suppression** — temporary pause via service call or card button; permanent disable via the `enabled` switch
- **Logbook integration** — every enforcement is logged
- **Custom Lovelace card** — color-coded status badge, enforcement counters, bound entity compliance, and quick-action buttons

## Card

Add to any dashboard:

```yaml
type: custom:entity-guard-card
rule_id: <config_entry_id>
```

The card auto-registers when the integration loads. No manual resource configuration needed.

![Entity Guard card](https://raw.githubusercontent.com/italo-lombardi/Home-Assistant-EntityGuard/main/assets/10_card_idle.png)

## Setup

1. Install via HACS (custom repository: `italo-lombardi/Home-Assistant-EntityGuard`)
2. **Settings → Devices & Services → Add Integration → Entity Guard**
3. Follow the config flow to create your first rule
