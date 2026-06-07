# Changelog

## [0.2.0-beta.1] — 2026-06-07

### Added

- **Flag replacement action**: New 'replace' button in options→edit_flags to delete all existing conditions and save only the new one
- **Repair issue detection**: Proactive validation detects missing flag entities and creates repair issues (non-fixable warning, user must update or remove rule). Check is deferred until HA is fully started to avoid false positives during startup
- **Missing flag entity repair**: New repair flow in Settings → System → Repairs displays missing flag entities with rule context
- **Logging for debugging**: Added debug/info/warning logs to flag action dispatch, repair creation/deletion, and statistics cleanup for easier troubleshooting

### Fixed

- **Statistics cleanup runtime fix**: Use `recorder.get_instance(hass).async_clear_statistics(ids)` instead of non-existent `recorder.async_clear_statistics`. The previous attempt would `AttributeError` at runtime — never actually cleared stats
- **Statistics cleanup robustness**: Rule removal now uses entity registry lookup instead of suffix reconstruction. Handles user-renamed entities correctly
- **Edge case guard**: Added check for empty statistics list to prevent silent failures during statistics cleanup
- **Repair issue creation**: Use `IssueSeverity.WARNING` enum and proper positional `issue_id` argument (previous code passed `issue_id` both positionally and as kwarg → `TypeError`)
- **Translation diacritics**: Restored proper accents in es/pt/nb/pl/sv translations that were stripped during smart-quote fix
- **da.json typo**: "Tilføj flagg" (Norwegian) → "Tilføj flag" (Danish)
- **Exception logging in repairs**: Use `_LOGGER.exception` instead of `_LOGGER.debug` to preserve traceback

### Changed

- **UI label**: 'Save without changes' → 'Exit without changes' for clarity
- **Flag action dispatch**: Refactored if/elif chain to match/case for readability
- **Code formatting**: Entire codebase formatted with ruff
- **Manifest**: Now declares `recorder` in `after_dependencies` (previously implicit)

### Technical

- Fixed JSON translation formatting (placeholder quotes must use double quotes per Home Assistant standards)
- Test coverage: Fixed test mocking for entity registry lookups + recorder instance in statistics cleanup
- Repair check deferred via `EVENT_HOMEASSISTANT_STARTED` listener to avoid false-positive missing-flag warnings on restart

## [0.1.1] — 2026-06-01

### Lovelace card

- Card now renders a shimmer skeleton during the first 4 seconds when registry data is still being populated, instead of flashing the "No Entity Guard rule entities found" error. The previous loader only triggered when `hass.entities` / `hass.devices` were entirely empty, so a partially-populated registry could fall through to the error message before the rule's device entry resolved.
- Skeleton replaces the prior `!hass` error placeholder as well, giving a uniform load experience.

## [0.1.0] — 2026-05-31

First stable release of Entity Guard.

### Core

- Declarative rules keep entities in a desired state — lights off, switches locked, volume capped, and more.
- Rules activate only when configured flag entities match their target states (conditions / flags).
- Built-in debounce, configurable delay, and rate limiting with automatic loop protection.
- Suppress a rule temporarily from the dashboard or via service call (`suppress`, `unsuppress`).
- `panic_stop` service immediately disables every rule via the master switch.
- `clear_history` service / button per rule — zeros today/total counters, clears cooldowns, resets last-enforced.
- Every enforcement is recorded in the HA logbook.

### Status state machine

- Statuses: `master_disabled`, `disabled`, `suppressed`, `conditional`, `armed`, `cooldown`, `enforcing`, `error`, `unavailable`, `unknown`.
- Strict priority funnels through a single `_derive_idle_status` helper: `master_disabled` > `disabled` > `suppressed` > `conditional` > `armed`/`cooldown`. Toggling a per-rule enable while master is OFF cannot flip the rule away from `master_disabled`.
- `error` status surfaces after 3 consecutive enforcement failures; auto-clears on next success or via `clear_history`. `consecutive_errors` and `last_error` exposed on the status sensor.
- Master switch toggle re-derives every rule's status instantly via dispatcher signal — no waiting for next state event.

### Sensors and entities

- Status sensor with `flags` attribute — each configured flag exposes `entity`, `required`, `current`, `matches`.
- `enforcement_count_today` resets at HA-local midnight via `async_track_time_change`, with date-stale catch-up on init.
- Per-rule device + entities (status sensor, switches, buttons, binary sensors) follow the rule's name; renames propagate to device-registry and trigger a reload.

### Lovelace card

- Live rule status, enforcement counters (today/total), last-enforced timestamp, bound entity compliance.
- Editor with rule selector and toggles: `show_stats`, `show_last_enforced`, `show_entities`, `show_conditions`, `show_actions` — order matches card render order.
- Conditions section renders each flag's current vs required state when `show_conditions` is enabled.
- Action buttons (lifecycle order): Test Enforce → Reset Cooldowns (when active) → Clear History → Suppress 1h (right-aligned).
- Card matches rule entities via device-registry identifier (not the display-only `config_entry_id` on `hass.entities`); shows a Loading skeleton until both `hass.entities` and `hass.devices` populate. Eliminates the "No rule entities found" flash on load.
- `_renderInfo` reuses `_stateValue` so transient `unknown`/`unavailable` states no longer flash on the "Last enforced" row.

### Config / options flow

- Multi-step config flow with summary recap on the final creation step.
- Re-opening a rule's options shows the same recap above the "Pick what to change" menu.
- Conditions / flags step shows the flag entity's current state inline (`{entity_id} is currently: {state}`).
- Debounce switch (`switch.<rule>_debounce_enabled`) persists to entry **options** so options-flow values do not shadow the toggle.

### Reliability

- State events deduplicate per entity, cancelling any still-pending evaluation before scheduling a new one — prevents HA crash from unbounded task creation on rapid state changes.
- Startup grace sweep and flag-change target sweep share the same deduplication logic.
- `rate_limit_window` cleanup uses `bisect` (O(log n)) instead of list comprehension (O(n)).
- Pending evaluation tasks are cancelled on engine unload.
- `engine.async_setup()` failure returns `False` from `async_setup_entry` instead of crashing HA.
- `_async_ensure_hub` spawned task is wrapped with an exception handler.
- Services raise `HomeAssistantError` on failure instead of propagating raw exceptions.
- `async_unload_services` is called when the last engine unloads — no stale service handlers on reload.
- Binary sensors and rule/master switches push initial state via `async_write_ha_state()` immediately on registration.
- Cooldown post-broadcast callback no longer leaks into `_unsub_callbacks`.
- `flag_entity_ids` cached as `frozenset` on engine init.

### i18n

- 11 locales: da, de, en, es, fr, it, nb, nl, pl, pt, sv.
