# Changelog

## [0.4.0] — 2026-10-26

### Added

- **Dynamic rule modes — bind a target to a live source entity.** Rules now come in a clean 2×2: **Fixed state**, **Fixed attribute**, **Dynamic state (Follow mode)**, **Dynamic attribute (Follow mode)**. A *dynamic* mode behaves exactly like its *fixed* counterpart — same enforcement, debounce, delay, conditions (flags), rate limit, loop protection, suppression, counters, status model — except the target value is read **live from a source entity** instead of a value you type.
  - **Dynamic state (Follow mode):** the target mirrors (or, with *invert*, opposes) a source entity's on/off. The classic "light follows motion sensor" setup that needed two rules is now one. By default the source's **state** decides on/off (on-ish states — `on`, `home`, `open`, `unlocked`, `playing`, `active`, `cleaning`, `mowing`, `true`, `1` — are on; anything else off); you can instead read one of the source's **attributes**, whose value is reduced to on/off (a non-zero number, or an on-ish state string, is on). `invert` flips the result.
  - **Dynamic attribute (Follow mode):** the target's attribute (brightness, volume, temperature, percentage, color) tracks a live value read from the source — either the source entity's **state** or one of its **attributes** (you choose). Raw value copy (v1, no range scaling — you ensure the value type/range matches the target attribute). Behaves like Fixed attribute otherwise: acts while the target is on, color matching within tolerance.
  - When the source is unavailable/unknown or a chosen source attribute is missing, the rule holds — no enforcement, no error — matching the fixed modes' offline behaviour.
  - Enforcement is idempotent: a target changes only when its current value differs from the live source value, so a steady source produces no repeated service calls.
- **Per-direction delay for Dynamic state (Follow mode).** A follow rule can now wait a *different* amount of time before driving its target **on** vs **off** — set **Delay (on)** and **Delay (off)** independently (each `0` = immediate). An unset direction falls back to the shared delay, so existing rules are unchanged. Only Dynamic state is affected; the other modes keep their single delay. The status sensor exposes `delay_on_seconds` / `delay_off_seconds` and the Lovelace card shows the two rows for dynamic_state rules.
- The status sensor (`sensor.<rule>_status`) exposes `mode`, `source_entity`, `follow_invert`, `follow_desired_on`, and `attribute`. The `mode` and `attribute` keys are present for all rules.
- **Lovelace card renders dynamic rules** with a follow summary and per-target compliance driven by the engine's authoritative value.

### Changed

- **Rule modes renamed for a clear fixed-vs-dynamic axis:**
  | Old | New (label) |
  | `State` | `Fixed state` |
  | `Attribute` | `Fixed attribute` |
  | *(new)* | `Dynamic state (Follow mode)` |
  | *(new)* | `Dynamic attribute (Follow mode)` |
  Existing rules migrate automatically on upgrade (config-entry schema v1 → v2): `state` → `fixed_state`, `attribute` → `fixed_attribute`. Rule behaviour is unchanged by the rename; no user action required.
- Card version bumped to `v0.4.0`; the integration version bump cache-busts the auto-registered Lovelace resource.
- **Translation polish.** The Dynamic state delay help text was reworded in several locales to idiomatic native phrasing (removing a literal "drive a target on/off" calque), and the Turkish hours abbreviation was disambiguated (`24s` → `24 sa.`, since `s` reads as seconds).

### Fixed

- **Suppressing a rule now cancels a pending enforcement.** Pressing **Suppress** while a rule was counting down to enforce (the `delay_seconds` window) no longer lets that enforcement fire — the scheduled action is cancelled and the card countdown clears immediately. Manual suppression and loop-protection auto-suppression both cancel any in-flight pending enforcement, and the enforcement path re-checks suppression before acting (covering the case where suppression lands in the same instant the timer fires). When suppression ends — whether the window expires or you clear it manually — the rule re-evaluates its targets, so a target still non-compliant from the cancelled enforcement is enforced again rather than left unguarded.
- **Condition rows show the correct state label.** A condition (flag) row in the Lovelace card paired a live-formatted state label with the snapshot value, so an off-state could render with the on-state's label — e.g. a connectivity sensor read `Connected (off) → Connected (on)` instead of `Disconnected (off) → Connected (on)`. The card now formats each shown value against that value (not the entity's current live state), so the label always matches the raw state next to it.
- **The card stays live while a rule is inactive.** When a rule was **disabled** or **suppressed**, a watched entity changing (a condition flag, or a dynamic-mode source) no longer refreshed the card: the engine deduplicates its status broadcast when the status string does not move, so the status sensor's live attributes (`flags[].current`, `follow_desired_on`, `attribute_desired_value`) stayed frozen at their pre-inactive values. The engine now forces a refresh on these paths, so the card reflects current conditions even though an inactive rule still (correctly) does not enforce.
- **Dynamic-attribute color rules no longer turn an off light on.** The enforce-time guard that skips a color write when the target light is off (or its color attribute is unavailable) only applied to *Fixed attribute* rules; a *Dynamic attribute* rule tracking `rgb_color`/`color_temp_kelvin` could call `light.turn_on` and power an off light on. The guard now covers both.
- **Ambiguous rule-name service calls are rejected.** The `entity_guard.suppress`/`unsuppress`/… services accept a rule name, but names are not unique; when two rules shared a name the service silently acted on whichever matched first. It now raises a clear error telling you to use the rule's entry ID instead (a unique ID or entry ID still resolves directly).
- **Migrating a pre-release Follow rule without a source entity raises a repair issue.** Such a rule would migrate to Dynamic state with no source and silently never enforce; the integration now surfaces a repair notification so you can recreate it.
- **The auto-registered card logs a real failure.** A static-path registration error other than "already registered" was swallowed at debug level, so a genuinely broken card registration showed no warning and silently 404'd; unexpected errors now log a warning.
- **The card recovers from a transient rule-list load failure.** If the editor's initial websocket call failed (e.g. during a Home Assistant restart) the rule picker stuck on "No rules configured" until the card was recreated; it now retries on the next update.
- Internal robustness: the condition-row renderer tolerates a malformed flag entry and non-string state labels without throwing the whole card; the rate-limit window stays correctly ordered under concurrent enforcement; sibling-target sweeps no longer mis-attribute the triggering entity's context.

## [0.3.1] — 2026-10-19

### Fixed

- **All non-compliant bound entities now show their individual "Enforcing in …" countdown.** When a rule with `delay_seconds > 0` has multiple non-compliant target entities, only the first entity that triggered the delay showed a countdown in the Lovelace card — all subsequent entities were silently missing from the `pending_enforcement_due` sensor attribute. The root cause was that `_set_status` deduplicates broadcasts (skip-if-same), so when the rule was already `pending` from the first entity, adding a second entity to the pending dict never triggered a sensor update. The engine now forces an explicit broadcast whenever a new entity is scheduled while the rule is already in `pending` state, so every entity's due timestamp reaches the card independently.
- **Pending countdown clears immediately when an entity returns to its target state.** When one entity in a multi-target rule became compliant while others were still pending, its "Enforcing in …" countdown remained visible in the Lovelace card until the next unrelated state change. Root cause: `_cancel_pending` correctly removed the entity from the internal due-times dict, but `_set_status` deduplicated the broadcast (rule status stayed `pending`), so the sensor never received the updated dict. The engine now forces an explicit broadcast on the compliant/skip paths whenever the rule remains `pending` after cancellation.

### Added

- **Pending enforcement countdown in the Lovelace card.** When a rule's status is `pending` (conditions met, delay timer running), each bound entity that is waiting to be enforced now shows a live countdown row beneath it — e.g. `⏱ Enforcing in 14 s` — ticking every second. The row uses the same duration formatting as the Configuration section (`s`, `min s`, `h min s`). It disappears as soon as the rule fires, conditions clear, or status leaves `pending`. The countdown is computed entirely in the browser from a `pending_enforcement_due` attribute (ISO timestamp keyed by entity ID) added to `sensor.<rule>_status`; nothing is persisted to storage.
- **"Show configuration" section in the Lovelace card.** A new `show_config` toggle (off by default, available in the card editor below "Show last enforced") reveals a "Configuration" section with four rows: Delay, Debounce (shows "Off" when disabled), Rate limit (shows "Off" when set to 0), and Loop protection (On/Off).
- The status sensor (`sensor.<rule>_status`) now exposes six new attributes: `delay_seconds`, `debounce_enabled`, `debounce_seconds`, `rate_limit_enabled`, `max_enforcements_per_minute`, `pending_enforcement_due`. These are read by the card and are also available for automations/templates.

### Changed

- **Bound entities and condition flags in the Lovelace card are now clickable.** Clicking (or pressing Enter/Space on) an entity row in the "Bound entities" or "Conditions" sections opens the standard Home Assistant more-info dialog for that entity, giving instant access to its history, attributes, and controls without leaving the dashboard. Both rows have keyboard focus support (`role="button"`, `tabindex="0"`) and show an underline-on-hover visual cue consistent with the EntityStateTracker card.
- Card version bumped to `v0.3.1`.

## [0.3.0] — 2026-09-11

### Fixed

- **The "Enforce loop protection" toggle no longer shows its raw key (`rate_limit_enabled`) when editing a rule.** The options flow's "Edit advanced settings" step (`edit_advanced`) was missing the `rate_limit_enabled` label in every translation file, so Home Assistant rendered the raw config key instead of a friendly label. The label is now present in all 30 locales plus `strings.json`, reusing each language's existing wording from the create flow's "advanced" step. The step also now shows the same field helper text (`data_description`) as the create flow for `debounce_seconds`, `rate_limit_enabled`, and `max_enforcements_per_minute`, restoring create/edit parity — plus a new helper for the `debounce_enabled` toggle (worded for the edit screen, where the debounce window field sits directly below it rather than on a later step).
- **Editing a rule in the options flow no longer reverts a slider value you set from its number entity.** A slider (e.g. `number.<rule>_delay_seconds`) persists its value to the config entry's *options*, but the options flow seeded its forms from — and saved back to — the entry's *data*. Opening "Configure → edit mode-specific settings" therefore showed the stale creation-time value, and saving overwrote the live options with it, silently reverting the slider. The options flow now treats options as the single source of truth: it seeds every form from the merged `{**data, **options}` view (options winning) and writes the result to options, so an unedited field keeps its persisted value and a one-field edit no longer clobbers the others.
- **Number sliders no longer reset on restart/reload.** The `number.<rule>_delay_seconds`, `number.<rule>_debounce_seconds`, and `number.<rule>_max_enforcements_per_minute` entities previously applied a change only to the running engine — the value was lost on the next Home Assistant restart or config-entry reload, silently reverting to the value from the original config flow. Slider values are now persisted to the config entry's options (debounced and coalesced so a drag collapses to a single write) and restored on setup, so an adjustment survives a restart.
- **Re-enabled rules no longer silently re-disable after a panic stop + restart.** Per-rule enabled state is owned by the runtime Store (written by the enable switch and by `panic_stop`). Setup also restored `enabled` from `entry.options`, but only `panic_stop` ever wrote that key — so after a panic stop, turning a rule back on, and restarting, the stale `options["enabled"]=False` re-disabled the rule. Setup no longer reads `enabled` from options; the Store is the single owner, and `panic_stop` no longer writes the redundant options key.
- **A slider adjustment no longer reloads the rule.** Persisting the slider value to the config entry's options fires the entry's update listener, which would otherwise reload the entry — tearing down and rebuilding all platforms, possibly mid-enforcement — even though the value was already applied live. The flush now marks the entry so the listener skips that reload; a re-set to the value already stored writes nothing at all. The debounce-enabled switch takes the same live-apply + skip-reload path.
- **Safety-target detection now reflects a live-edited target list.** `entry_has_safety_target` read `entry.data` and only fell back to `entry.options`, so once the options flow wrote a target-entities list the check could still consult the stale data value. It now reads the merged `{**data, **options}` view, matching how the rule engine resolves the rule's configuration.
- **Renaming a rule now reloads it once, not twice.** The options-flow save wrote the title and the options in two separate `async_update_entry` calls, and setup ran a device-registry name-sync block that did a third `async_update_entry` — each firing the entry's update listener and reloading the entry, tearing all platforms down and back up two-to-three times per rename. Title and options are now written in a single update (the flow's own finalizing write of the same options is a no-op), and the setup-time sync block is gone: every platform already sets `DeviceInfo(name=entry.title)`, so the one reload renames the device, and a user's own "Rename device" override (`name_by_user`) is a separate attribute that integration name-writes never touch.

### Changed

- **Max-enforcements-per-minute slider can now reach `0` to disable rate limiting.** The slider's minimum was `1`, which could not express the "no limit" value (`0`) that the options flow already allowed and the engine already honored — so persisting a slider adjustment would have permanently clamped a deliberately-disabled rate limiter back up to `1`. The slider floor is now `0`, matching the flow and the engine.

### Tests

- Added coverage for slider persistence (debounced flush writes to options), two sliders moved within the debounce window coalescing into one write, unload cancelling a pending write (and a remove-while-loaded flush that preserves the coalesced value), the max-enforcements `0` floor, the panic-stop/enabled ownership change, and the update listener skipping the reload on a slider write (while still reloading on a real change).
- Added options-flow coverage: a save preserves an unedited slider value persisted to options, an edit shows the persisted option (not the stale data value) as its form default, and the rate-limit toggle writes `0` when disabled. Added a live smoke test (`scripts/smoke.py`) driving the number/switch entities and the REST options flow against a real Home Assistant across restarts.

## [0.2.9] — 2026-09-08

### Added

- **Friendly state labels in the Lovelace card**: the status chip now shows translated labels ("Armed", "Waiting on conditions") using HA's `formatEntityState` API with `STATUS_LABELS` as fallback for older HA versions. Bound-entity and condition rows show contextual labels where HA's device_class provides one — e.g. `Clear (off)` for a smoke sensor, `Detected (on)` for a motion sensor; entities where the label is just a capitalised form of the raw value (e.g. groups) show the label only (`Off`). Non-compliant rows translate both sides of the arrow: `Detected (on) → Clear (off) ⚠`. Raw state values are unchanged everywhere — automations and templates are unaffected.

### Changed

- Device-registry lookup now prefers `async_get_device_by_identifier` (added in HA 2026.8), falling back to `async_get_device(identifiers=)` on older Home Assistant. HA's frame helper flags the `identifiers=` form as deprecated and slated to stop working in HA 2027.8; the fallback keeps the declared minimum (HA 2024.1) working while silencing the warning on newer cores. Note: the new method scopes the lookup to the config entry, but for this integration (one device per entry, identifier == entry id) the resolved device is identical.

### Fixed

- `master_disabled` status value was missing from the English (`strings.json`) selector translations; other locales (de, ja, etc.) already had it. The config flow rule-status filter now shows "Master disabled" in English.
- Device lookup no longer trips the Home Assistant deprecation warning about `device_registry.async_get_device` on newer cores.

### CI

- Standardized development and CI on Python 3.14.
- Fixed a test that leaked a hub auto-create import flow into teardown, scheduling a startup-grace timer that `verify_cleanup` flagged as a lingering timer under Python 3.14's task scheduling.

### Tests

- Added `test_status_values_have_selector_translations` in `test_const.py`: asserts every `STATUS_VALUE` constant has a corresponding entry in `strings.json` `selector.status.options`, catching future sync gaps at CI time.
- Silenced the recurring `coroutine 'RuleEngine.async_evaluate' was never awaited` warning by closing coroutines handed to a patched `hass.async_create_task`.

### i18n

- Added 18 new locales: zh-Hans, zh-Hant, ru, ja, ko, uk, cs, sk, hu, ro, bg, hr, fi, el, tr, lt, lv, ca. Translation coverage grows from 11 → 29 languages.

---

## [0.2.8] — 2026-07-22

### Added

- **Color enforcement for light entities**: attribute mode now supports `rgb_color` and `color_temp_kelvin` in addition to the existing numeric clamp attributes. Color rules use a tolerance-aware match model and call `light.turn_on` only when the light is already on and its current color differs materially from the configured target.

### Changed

- Out-of-range `color_temp_kelvin` targets (outside 2000–6500 K) are now rejected on load, matching the RGB channel range check — a bad hand-edited/imported value leaves the rule inert instead of sending an invalid Kelvin to `light.turn_on`.

### Docs

- Documented the `entity_guard_skipped` `reason` values, including the color-specific `light_off`, `light_unavailable`, and `attribute_unavailable` reasons.

### Tests

- Added coverage for color target parsing, config-flow creation/editing, tolerance boundaries, skipped enforcement on off/unavailable lights, and debounce behavior for color rules.

---

## [0.2.7] — 2026-07-08

### Added

- **`counter_since` / `counter_days` attributes** on `sensor.<rule>_enforcements_total`. `counter_since` is the ISO timestamp of when the total counter window opened — either rule creation (backfilled from `ConfigEntry.created_at`) or the last **Clear History** action. `counter_days` is the whole-day difference from `counter_since` to now, useful for card templates (e.g. "156 enforcements · 118 days").

### Fixed

- **Card condition rows stale when overall status is unchanged**: toggling a flag entity while the rule's overall status string didn't move (e.g. `conditional`→`conditional` with another flag still blocking, or `armed`→`armed` on a multi-flag rule where a non-critical flag flips) left the frontend showing the flag's previous value indefinitely. `_set_status` skips re-broadcast when the status string is unchanged; the status sensor's `extra_state_attributes` (which the card reads for flag `current` values) was therefore never re-read by HA. `async_evaluate` now emits an explicit `_broadcast_status()` on flag-entity events whenever the overall status doesn't change, so the card sees fresh flag `current` values on every flag toggle.

### Tests

- 12 new tests (1 removed). Full suite: 515 passed. Line coverage 100%, branch coverage 100%.

---

## [0.2.5] — 2026-07-06

### Added

- **`binary_sensor.<rule>_recently_enforced`**: turns `on` for 30 seconds after any enforcement — including Test Enforce. On repeated enforcements while already `on`, pulses `off → on` so each enforcement re-triggers automations. Resets immediately when Reset Cooldowns or Clear History is called. Enabled by default. Exposes a `rule_name` state attribute for use in automation templates (`state_attr(..., 'rule_name')`).
- **`sensor.<rule>_rule_id`** (hidden by default, diagnostic): exposes the rule's stable config-entry ID. Use this in automation `event_data` filters instead of `rule_name` so renames never break automations.
- **`AUTOMATION_EXAMPLES.md`**: copy-paste automation library covering notifications, rule control, enforcement reactions, loop-detection handling, dashboard counters, and a full event-payload reference.

### Fixed

- **`async_reset_cooldowns` left `recently_enforced` stale**: cancelling cooldowns cleared the flag but did not broadcast, so the binary sensor stayed `on` indefinitely when status was already `ARMED` (skip-if-same guard blocked the signal). An unconditional `_broadcast_status()` call now precedes `_apply_idle_status()` in the reset path, mirroring `async_clear_history`.

### Tests

- 4 new tests. Full suite: 487 passed, 1 skipped. Line coverage 100%, branch coverage 100%.

---

## [0.2.4] — 2026-07-01

### Fixed

- **Disabled rule shows `starting` on reload**: when a rule's `Enabled` switch was off, any config reload (e.g. changing debounce toggle) briefly flashed `starting` status before settling to `disabled`. The engine now derives the correct `disabled`/`master_disabled` status at the first broadcast during `async_setup`, so the UI never transiently shows `starting` for a disabled rule.
- **Test Enforce ignores `Enabled` switch**: pressing Test Enforce while the rule's `Enabled` switch was off would run enforcement and leave status as `armed`/`enforcing`. The `Enabled` switch is now the highest-priority gate: Test Enforce still calls the service (so the rule can be validated while disabled) but calls `_apply_idle_status()` after each entity so no intermediate `armed`/`enforcing` broadcasts appear in HA history.
- **Delayed enforcement fires after rule disabled**: if a rule was disabled between queuing a delayed-enforcement timer and the timer firing, `_enforce` ran unconditionally. `_fire` now checks `enabled`/master at entry and returns early (calling `_apply_idle_status()`) if the rule is inactive.
- **`async_clear_history` broadcasts stale status on non-ERROR rules**: the else branch called `_broadcast_status()` unconditionally, which could emit `starting` during grace. Replaced with `_apply_idle_status()` for correct status derivation in all paths.
- **Startup grace schedules sweep tasks for disabled rules**: `_handle_startup_grace_done` always queued eval tasks for every target entity, even when `enabled=False`. Disabled/master-off rules now return early after `_apply_idle_status()`, skipping the wasteful sweep.
- **Disabled check ordered after grace guard in `async_evaluate`**: the startup-grace guard was evaluated before the `enabled`/master check. A disabled rule receiving a state event during grace would skip the fast-path `_apply_idle_status()` call. Guard order corrected: disabled check is now first.
- **Cooldown-broadcast race with `async_reset_cooldowns`**: `pragma: no branch` removed from the `cooldown_end is not None` guard — `async_reset_cooldowns` can clear `_state.cooldowns` between the cooldown being set and the timer being armed, making the `None` branch reachable.

### Tests

- 15 new tests (net). Full suite: 483 passed, 1 skipped. Line coverage 100%, branch coverage 100%.

---

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
