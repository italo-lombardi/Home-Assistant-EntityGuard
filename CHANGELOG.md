# Changelog

## [0.4.2] — 2026-10-30

### Added

- **Cancelled-signal sensor + event** (neutral third verify-outcome twin, alongside verified/unreachable). `binary_sensor.<rule>_recently_enforced_cancelled` + `entity_guard_enforced_cancelled` event fire when an armed verify cycle is **abandoned mid-window with no verdict** because an external condition changed — the rule was suppressed, disabled, the hub master switched off, or its conditional flags lapsed during the verify window. This is distinct from *verified* (confirmed success) and *unreachable* (confirmed device failure): the cycle reached no conclusion, so neither signal is honest. Fires at all four drop reasons across both the deadline and event confirm paths, deliberately **not** on integration unload. Event carries `rule_id`, `rule_name`, `entity_id`, `target_entity_id`, `domain`, `trigger`, `target`, `reason`; has a logbook describer. Verify-only: sensor created when verify on, disabled (not removed) when off, re-enabled when back on.
- **Cancelled device-automation trigger.** Each rule device gains a seventh visual trigger — *cancelled* — so the abandoned-cycle event is pickable from the automation editor without hand-typing an `event` trigger. Two new translation keys (menu label + help) across en + 28 locales.

### Fixed

- **Verify corrective re-enforce acted while the rule was CONDITIONAL.** On a verify-enabled state rule with conditional flags, if the watched flag went unmet during the verify window (between the enforce and the re-read deadline), the corrective re-enforce fired anyway — driving the target to the desired state while the rule showed CONDITIONAL (flags unmet), i.e. acting when it should not. Observed as a rule repeatedly forcing a light off while cycling conditional↔enforcing. Cause: the verify corrective path re-read the target and re-enforced without re-checking `_flags_match()`, unlike the normal enforce paths (`async_evaluate`, the delayed `_fire`) which gate on it. The verify timer now drops the cycle when flags are unmet (clears the attempt counter, re-derives idle status to CONDITIONAL) — mirroring the existing suppressed-drop — so a later flag-met state event re-enters via the normal enforce path and re-arms verify. No config change; the suppressed, reached, and exhaustion paths are unchanged.
- **An armed verify was hidden behind CONDITIONAL on the card.** On a flag-gated verify-enabled rule, the status sensor flipped `enforcing → conditional` the instant the target reached desired — masking the outstanding verify re-check, which only surfaced later (backwards) when the deadline fired a corrective. Cause: `_derive_idle_status` ranked CONDITIONAL above the in-flight verify/pending-enforce states. In-flight action (PENDING / VERIFYING) now outranks CONDITIONAL — a cycle that already decided to act (a trigger matched) stays visible through its enforce/verify window; a flag that flips back mid-cycle no longer masks it. The "must not act" gates (DISABLED, MASTER_DISABLED, ERROR, SUPPRESSED) still outrank. Display only; no behaviour change.
- **Long delay before verify validated a responsive target.** On a device that accepts `blocking=True` and commits its new state during the service call (most local lights/switches), verify waited out the full `VERIFY_DELAY_SECONDS` (5 s) blind deadline before re-reading — even though the target's own post-enforce `state_changed` had already confirmed it reached desired. Cause: the verify was armed *after* the service call returned, so the confirming event landed while nothing was listening and the event-driven confirm was skipped. The enforce tail now re-reads live state synchronously the instant the verify is armed: an already-reached target confirms in the same tick (fires `entity_guard_enforced_verified` immediately) instead of after 5 s. A device that silently ignored the command still reads not-reached and keeps its deadline for the corrective loop — unchanged.
- **Card stuck ARMED after disabling a rule mid-enforce.** Toggling a rule off while it was mid-enforce (e.g. parked in the service-call await during a slow device ACK, or showing VERIFYING) left the card stuck at ARMED instead of DISABLED. Cause: `set_enabled()` is synchronous and takes no reentrance lock, so the disable interleaved while `_enforce` was awaiting; on resume the enforce tail repainted the status with the raw armed/cooldown derivation, which has no enabled/master gate, overwriting the correct DISABLED. Three coordinated fixes: the enforce tail now paints through `_derive_idle_status` (which honours the enabled/master flags); `set_enabled(False)` cancels any in-flight verify timers; and the verify-arm guard re-checks enabled/master so a rule disabled mid-enforce never arms a verify. The card now settles on DISABLED (or MASTER_DISABLED) as expected.
- **Disable mid-verify leaked the attempt counter and never signalled.** Switching a rule off while a verify cycle was armed left `_verify_attempts` populated (`set_enabled`'s cancel loop cancelled the timer but dropped only the pending handle), so a quick re-enable + re-enforce read a stale high count and could latch exhaustion a cycle early — a premature `entity_guard_enforced_unreachable` on a device that only "failed" because the rule had been off. The same cancel loop also meant the new cancelled signal's most common cause — a per-rule disable — never fired it: the timer was gone before any drop site could run. `set_enabled(False)` now clears the attempt counter and fires `entity_guard_enforced_cancelled` (reason `disabled`) for any target whose verify it abandons (skipped on unload, and only when a verify was actually in flight).
- **Manual `test_enforce` on a CONDITIONAL rule resolved CANCELLED instead of VERIFIED.** Pressing a rule's *Test enforce* button while its conditional flags were unmet (e.g. an "away" rule while someone is home) drove the target correctly but the verify cycle resolved `cancelled` (reason `flags_unmet`) instead of `verified`, even though the device reached desired — and whether it landed verified or cancelled was pure device-ACK timing luck (a fast `blocking=True` target sometimes confirmed in-tick while a slower ACK fell to the deadline/event path and was gated off). A manual press is a deliberate user override, so verify must judge the **device** (verified / unreachable), not re-apply the flag/suppression gates. A `forced` marker now threads from `async_test_enforce` through the verify arm and is honoured identically by all three confirm paths (synchronous fast-confirm, the deadline re-read, and the event-driven confirm): a forced cycle bypasses the `suppressed` and `flags_unmet` drops and resolves on real device state. It still honours `unloaded` (teardown) and `disabled`/`master_off` (a forced cycle cannot re-enable a rule or hub that is genuinely off). The automatic path is unchanged.

## [0.4.1] — 2026-10-30

### Fixed

- **Enforcements stopped counting while the switches still flipped.** On a rule driving slow battery/RF targets (e.g. SwitchBot), the target switches moved correctly but `enforcements_today`/`enforcements_total` froze, `last_enforced` stuck days old, no `entity_guard_enforced` event fired, and `binary_sensor.<rule>_recently_enforced` never pulsed. Cause: the target's own post-enforce `state_changed` — delayed by the device's multi-second ACK — re-entered the per-entity cancel-and-replace and cancelled the still-awaiting enforce task mid service-call, so the success bookkeeping (counters, `last_enforced`, the enforced event, persistence) was silently dropped after the device had already moved. The service call is now shielded: the device move and the full bookkeeping tail complete atomically before the deferred cancel is honoured, so counters, `last_enforced`, the event, and the `recently_enforced` pulse all land and persist across a restart. No config change; verify, cooldown broadcast, and the corrective/unreachable paths are unchanged.

## [0.4.0] — 2026-10-26

### Added

- **Dynamic rule modes (Follow mode).** Rules now form a 2×2 — **Fixed state**, **Fixed attribute**, **Dynamic state**, **Dynamic attribute** — where a dynamic mode reads its target value live from a source entity instead of a typed value. *Dynamic state* mirrors (or, with `invert`, opposes) a source's on/off (via state or a chosen attribute); *Dynamic attribute* copies a source's live value (brightness/volume/temp/color, raw, no scaling). Source offline/missing attribute → rule holds; enforcement idempotent (no write when already matching).
- **Per-direction delay for Dynamic state.** Independent **Delay (on)** / **Delay (off)** (each `0` = immediate; unset falls back to shared delay). Status sensor exposes `delay_on_seconds` / `delay_off_seconds`; card shows both rows.
- Status sensor (`sensor.<rule>_status`) exposes `mode`, `source_entity`, `follow_invert`, `follow_desired_on`, `attribute` (`mode`/`attribute` present for all rules).
- **Lovelace card renders dynamic rules** — follow summary + per-target compliance from the engine's authoritative value.
- **Per-target outcome visibility.** Engine records each target's last outcome (`ok`/`skipped`/`failed`) on the `target_outcomes` attribute; card's **"Targets needing attention"** panel lists only targets needing action (failure or `no_service_mapping`), self-hiding when all healthy. Diagnostic only; excluded from recorder.
- **"Why is this rule idle?" reason line.** Card shows one line under the badge (*Waiting on `<entity>=<state>`*, *Cooldown*, *Suppressed until … — reason*, *Master switch off*); Conditions section auto-expands when `conditional`.
- **Enforce non-on/off target states.** State-mode rules can drive climate HVAC modes, water_heater ops, input_select options, media_player transport, vacuum, cover `stopped`, valve, lawn_mower — previously silently skipped (`no_service_mapping`). Target-state picker pruned to drivable states (read-only domains + transitional states dropped; `custom_value` kept); `valve` now needs safety ack. (`alarm_control_panel` deferred — needs a code field.)
- **Opt-in verify-after-enforce.** Enable **Verify enforcement worked** on a state-mode rule; engine re-reads the target after enforcing, retries, then records `unreachable_after_retries` in `target_outcomes` + fires a skipped event if it never reached desired. Off by default; attribute modes unaffected.
- **Config-entry diagnostics.** Rule/hub Diagnostics return a structured dump (hub: `master_enabled` + rule IDs; rule: full config + runtime state, ISO timestamps). User identifiers redacted; entity IDs kept.
- **Skip counters.** `sensor.<rule>_skips_today` / `sensor.<rule>_skips_total` count declined enforcements (already compliant, cooldown, offline). Failures not counted; `skips_today` resets at local midnight; both persist.
- **Device-automation triggers.** Each rule device exposes six visual triggers — enforced / skipped / loop-detected / verified / unreachable / suppressed — for the automation editor, so every rule-scoped event is pickable from the device card without hand-typing an `event` trigger. Hub offers none.
- **Verify-after-enforce device-page switch.** `switch.<rule>_verify_enabled` in the rule's Configuration card, two-way synced with the flow option, state-mode only. Card gained a **Verify** row.
- **Verify defaults on for new state-mode rules** (opt-out). Existing rules untouched; pre-feature rules still default off. No silent migration.
- **Verified-signal sensor + event.** `binary_sensor.<rule>_recently_enforced_verified` + `entity_guard_enforced_verified` event, both only on *confirmed* success (verify re-read reached desired), one pulse per cycle. Event carries `rule_id`, `rule_name`, `entity_id`, `target_entity_id`, `domain`, `trigger`, `target`, `attempts`; has a logbook describer. Sensor created only when verify on; disabled (not removed) when off, re-enabled when back on. Never fires on verify-off, verify-failure, suppression, or kill-switch.
- **Unreachable-signal sensor + event** (failure twin of the verified signal). `binary_sensor.<rule>_recently_enforced_unreachable` + `entity_guard_enforced_unreachable` event fire only when a verify-enabled target never reached desired after the corrective re-enforces (a silent non-compliance — the service call didn't raise). One pulse per fresh exhaustion streak (same `count==1` gate as the skipped event, so a dead device can't flood the bus); a recovery resets the streak so a later death re-fires. Event carries `rule_id`, `rule_name`, `entity_id`, `target_entity_id`, `domain`, `trigger`, `target`, `reason`; has a logbook describer. Verify-only: sensor created when verify on, disabled (not removed) when off, re-enabled when back on.

### Changed

- **Rule modes renamed (fixed-vs-dynamic axis):** `State`→`Fixed state`, `Attribute`→`Fixed attribute`, plus new `Dynamic state` / `Dynamic attribute`. Auto-migrates (schema v1→v2); behaviour unchanged, no user action.
- Card bumped to `v0.4.0`; version bump cache-busts the auto-registered Lovelace resource.
- **Translation polish.** Dynamic-state delay help reworded to idiomatic native phrasing in several locales; Turkish hours abbreviation disambiguated (`24s`→`24 sa.`).
- **Verify confirms on device ACK, not a blind 5 s wait.** The happy-path verify now confirms the moment the target's `state_changed` reports it reached desired (~ms), instead of dwelling on the full `VERIFY_DELAY_SECONDS` snapshot. That delay is now a *deadline* — the fallback for a device that ignores the command — so corrective-retry, exhaustion kill-switch, and the unreachable signal are unchanged for non-compliant devices.

### Fixed

- **The verified pulse now fires for a group target.** `binary_sensor.<rule>_recently_enforced_verified` never pulsed for a light *group* target even when the enforce landed: a group reads any-on `on` until its *last* member turns off, so each intermediate member-off emitted a group `state_changed` still reading `on` (not yet desired), and the reactive path treated that as an away-move and cancelled the in-flight verify. The verify is now kept armed while the target is still online but not-yet-reached, and confirms on the final reached event. A reached-move that re-asserts a single target mid-verify likewise keeps the verify armed (its corrective loop re-drives to desired) rather than silently dropping it; a genuinely offline (`unavailable`/`unknown`) target still cancels as before.
- **Automations triggered on the verified/unreachable pulse sensors no longer crash.** `binary_sensor.<rule>_recently_enforced_verified` and `_recently_enforced_unreachable` now expose the same rule context (`rule_name`, `target_entities`, `target_entity_names`, `target`, `delay_seconds`) as the base `recently_enforced` sensor — previously only the base sensor carried them, so an automation reading `state_attr(trigger.entity_id, 'target_entities')` off a verified/unreachable trigger got `None` and failed with `TypeError: 'NoneType' object is not iterable`.
- **A group target with one dead member no longer flags a false error.** Enforcing a light group whose aggregate already reached the desired state, but where one offline member's integration *raises* (e.g. a SwitchBot Cloud device offline), is now a clean `member_unavailable` skip instead of climbing `consecutive_errors` into a sticky red "error" banner — the group as a whole obeyed; only a dead member couldn't be commanded. The card's "Targets needing attention" panel lists the offline members (up to 3 names + "+N more", one line) so the problem is visible without the false fault. A single (non-group) target that raises still counts as a real error, and a group whose aggregate is *not* at desired when the call raises still errors.
- **Suppressing a rule cancels a pending enforcement.** Manual + loop-protection suppression cancel an in-flight pending enforce and clear the countdown; the enforce path re-checks suppression before acting; ending suppression re-evaluates targets.
- **Condition rows show the correct state label.** Card formats each shown value against *that* value, not the live state, so an off-state no longer renders with the on-state's label.
- **Card stays live while a rule is inactive.** Engine forces a status refresh on disabled/suppressed paths so live `flags[].current` no longer freezes while watched entities change.
- **Ambiguous rule-name service calls rejected.** `entity_guard.suppress`/`unsuppress`/… now raise a clear error on a duplicate name (use the entry ID); a unique/entry ID still resolves.
- **Auto-registered card logs a real failure** — a non-"already registered" static-path error now logs a warning instead of being swallowed at debug.
- **Card recovers from a transient rule-list load failure** — retries on next update instead of sticking on "No rules configured".
- Internal robustness: condition-row renderer tolerates malformed flags / non-string labels; rate-limit window stays ordered under concurrent enforce; sibling-target sweeps don't mis-attribute trigger context.
- **A renamed or removed target entity raises a repair issue** (WARNING, naming the missing targets; no auto-rewrite). Also fixes a latent bug where the flag-entity check caught removals but missed renames. State-machine-only targets (YAML `group.*`, unique_id-less template/MQTT) correctly not flagged.
- **A hung integration can no longer freeze a rule forever.** Enforcement service call bounded by a 30 s timeout, treated as a failure (logged, counted, surfaced per-target) so the lock releases.
- **Live cooldown/suppression countdowns on the card.** Reason line ticks down in real time for `cooldown` and `suppressed`, matching the pending countdown. (Status sensor gained `cooldown_ends`, excluded from recorder.)
- **Create-flow verify help now matches the edit flow and the event-driven behaviour**, across `en` + all 28 locales. The *Verify enforcement worked* toggle help shown while creating a rule carried the old blind-delay wording; it now reads identically to the edit-flow help (confirms on the device's own state-change ACK; the few-second timer is a deadline backstop, not a mandatory dwell). A native-speaker pass also corrected two locales whose existing wording misstated the behaviour — Czech ("until the state settles" → "until the desired state is reached") and Latvian (a medical false-friend for "recover" + a weaker backstop framing) — in both the create and edit help.
- **Translation tests guard against untranslated locales.** `tests/test_translations.py` fails CI if any locale file is a wholesale English paste (>15% of strings identical to English) or is missing a key present in `en.json` (which would silently fall back to English at runtime).

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
