# Changelog

## [0.2.3] — 2026-06-21

### Fixed

- **Error status now auto-recovers**: rules stuck in `error` after 3 failed enforcements now return to `armed` after 3 successful ones. Previously required HA restart or Clear History.
- **Suppressed sensor updates on time**: when a suppression window ends, the status sensor flips to `armed` immediately instead of waiting for the next state event.

### Changed

- During error-recovery, enforcement events and counters tick normally — automations bound to `entity_guard_enforced` will see firings while the status sensor still reads `error`. Loop protection can also trigger from `error` if a misconfigured rule thrashes during recovery.
- Clear History and Reset Cooldowns no longer cancel active suppression. Use Clear Suppression (or `entity_guard.unsuppress`) to end suppression early.

### Tests

- 11 new tests covering recovery, suppression timer, and reset/clear interactions.
- Full suite: 443 passed, 1 skipped. Coverage 99%.

---

## [0.2.2] — 2026-06-15

### Fixed

- **Storage migration crash**: `_async_migrate` raised uncaught `NotImplementedError` for any persisted store with `version < 1`. `async_load` now catches the error, resets storage to defaults, and creates a persistent notification — consistent with the existing blob-corruption recovery path.
- **Service leak on hub-present unload**: `async_unload_services` never fired while the hub entry existed because `remaining_entries` included the hub. Services (`suppress`, `unsuppress`, `clear_history`, `list_rules`, `panic_stop`) leaked indefinitely when the last rule entry was removed. Fix: filter `remaining_entries` to rule entries only.
- **Double enforcement race**: `_fire` popped `_pending_enforcements` before `_enforce` completed, allowing a second fire to re-enforce mid-flight. Cancel-handle identity check closes the residual race.
- **Cooldown stuck permanently**: `STATUS_COOLDOWN` cooldown-remaining calculation used a stale `now` captured at timer creation instead of re-reading at query time.
- **Engine listener leak on failed platform unload**: Listeners registered during platform setup were not released when `async_unload_platforms` returned `False` due to a partial unload failure.
- **`async_suppress` bypasses priority ladder**: Calling suppress while a higher-priority condition was active could force a spurious `DISABLED → SUPPRESSED` transition. Suppress now respects the full priority order.
- **Orphan cooldown broadcast timers**: `async_reset_cooldowns` / `async_clear_history` did not cancel in-flight cooldown broadcast timers, leaving stale callbacks that fired after reset.
- **Device registry name overwritten on restart**: Hub entry wrote the device name unconditionally on every HA restart, clobbering user renames. Write is now skipped when the name is already set.
- **Number slider triggers full entry reload**: Slider value changes wrote to `entry.data`, triggering a full reload. Values are now stored in `entry.options` (no reload side-effect).

### Tests

- 3 regression tests for storage migration crash and service-leak paths.
- 10 tests for `rule_engine` fixes (double enforcement, cooldown timer, listener leak, suppress priority, orphan timers, device name, number slider).
- Full suite: 432 passed, 1 skipped.

---

## [0.2.1] — 2026-06-13

### Fixed

- **Startup error**: Renamed `repairs.py` → `issue_helpers.py`. HA auto-discovers any file named `repairs.py` as a repairs platform and requires `async_create_fix_flow`. The file contains only issue-creation utilities, so HA raised `HomeAssistantError: Invalid repairs platform` on every startup.
- **Repair issue uses entity registry**: Missing-flag detection now checks the entity registry (`er.async_get`) instead of `hass.states`. The state machine still has the old state briefly after entity deletion, causing the check to incorrectly pass. The registry is updated synchronously before the event fires, so it is always authoritative.
- **Live repair detection**: Entity Guard now listens to `entity_registry_updated` events for all flag entities. Repair issues appear immediately when a flag entity is deleted and clear immediately when it is recreated — no restart required.
- **Logbook suppression message**: `EVENT_SUPPRESSED` payload carries `suppressed_until`, not `duration_minutes`. The describer always fell back to "rule suppressed" with no detail; now shows the suppression timestamp.
- **Reload error on startup**: `async_listen_once(EVENT_HOMEASSISTANT_STARTED)` unsub registered during first boot is consumed when HA starts. A subsequent reload called the dead unsub causing `ValueError: list.remove(x): x not in list` in `homeassistant.core`. The unsub is now wrapped to swallow the error.

### Changed

- Signal helpers (`signal_rule_update`, `signal_master`, `has_safety_target`) extracted to `const.py` — previously duplicated across 4 platform files.

### Tests

- `tests/test_issue_helpers.py` — 10 tests covering all check paths, live listener wiring, and listener unsubscribe on entry unload.
- Test coverage: 98% total; all platform files at 100%.

---

## [0.2.0] — 2026-06-08

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
