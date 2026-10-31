# Entity Guard — Behavior Reference

How the rule engine decides, acts, verifies, and signals. This is the runtime
truth behind the events in the trigger picker and the status shown on the card.
For ready-made automations that consume these events see
[`AUTOMATION_EXAMPLES.md`](AUTOMATION_EXAMPLES.md).

Source of truth: `custom_components/entity_guard/rule_engine.py`. Event names:
`const.py` (`EVENT_*`, lines 368-392).

---

## 1. What a rule does

A rule watches its target entities and drives them to a desired state when they
drift. The desired state comes from the rule's **mode**:

- **static** — hold a fixed state (e.g. "this switch must stay `on`").
- **follow** — mirror a source entity's state/attribute onto the target.

When the target drifts from desired, the rule **enforces** (sends the matching
service call). Everything below is what happens around that single act.

---

## 2. The seven events

All payloads carry `rule_id`, `rule_name`, `entity_id`, and `forced` — `true`
when the enforce came from a manual force (`test_enforce` / card button),
`false` for an automatic trigger. `entity_id` is the rule's status sensor on
**enforced**, **verified**, **unreachable**, **cancelled**, and **suppressed**;
on **skipped** and **loop_detected** it is instead the target entity the skip or
loop is about. The verify family (`verified` / `unreachable` / `cancelled`)
fires **only when verify is enabled on the rule**.

| Event | Fires when | Example | Outcome |
|---|---|---|---|
| **enforced** | Rule acts: commands target to desired (service call sent and returned without error). Fires *after* the call, not when sent. It does not confirm that the command was actually executed (async ACK ≠ state change). If verify is on and the target doesn't obey, Entity Guard tries the command again (a "corrective"). Each successful retry fires `enforced` again, but the "Enforcements" count only goes up once. | Rule "lamp on"; lamp turns off → `light.turn_on`. | `entity_guard_enforced`. Lamp commanded on (but not confirmed that turned on). |
| **skipped** (overview) | Rule decided not to act, or tried and failed. Reasons split out in the rows below. Many not-triggered cases — source offline, below threshold — fire *nothing* at all. | Follow rule; source sensor `unavailable`. | `entity_guard_skipped` (`reason` names which of the 9 below). No write. |
| **skipped** · `source_is_target` | target entity is also the source — never enforce the source onto itself | Follow rule where the picked target is the same entity as the source. | `entity_guard_skipped` (`source_is_target`). No write. |
| **skipped** · `light_unavailable` | color rule only — target light is `unavailable`/`unknown`, so its color can't be set; a fixed-state rule hitting an unavailable target doesn't use this reason, it's one of the silent not-triggered cases that fire nothing | Color rule holds a bulb at red; the bulb drops to `unavailable`. | `entity_guard_skipped` (`light_unavailable`). No write. |
| **skipped** · `light_off` | color rule only — target light is off; color can only be set on a light that's on | Color rule holds a bulb at red; the bulb is switched off. | `entity_guard_skipped` (`light_off`). No write. |
| **skipped** · `source_offline` | follow (dynamic_state) rule only. At enforce time the engine re-reads the source and finds it offline/unknown, so it can't tell what on/off to copy — skips instead of guessing. No service call sent. | Follow-state rule; the source sensor goes `unavailable` between trigger and enforce. | `entity_guard_skipped` (`source_offline`). No write. |
| **skipped** · `source_value_unavailable` | follow (dynamic_attribute) rule only. Twin of `source_offline` but for an attribute mirror: at enforce time the engine re-reads the source value and gets nothing — either the source is offline, or the specific attribute it copies is missing. Won't write an empty value — skips. No service call sent. | Follow-brightness rule; the source's `brightness` attribute is missing at enforce. | `entity_guard_skipped` (`source_value_unavailable`). No write. |
| **skipped** · `no_service_mapping` | the engine couldn't find which Home Assistant service to call for this target and desired state. Two shapes: the target's **domain** has no mapping at all (Entity Guard can't drive it), or the domain IS mapped but this specific **state** has none — a non-on/off state with no recipe, or a mode state the device won't accept (e.g. `climate` → `banana`). No service call sent. | Rule targets a domain Entity Guard has no recipe for; or a `climate` rule asks for an hvac mode the device doesn't list. | `entity_guard_skipped` (`no_service_mapping`). No write. Raises a repair issue — a domain card when the whole domain is unsupported, a state card when only the state is. |
| **skipped** · `member_unavailable` | the target is a group, and the group as a whole already reached the desired state, but one dead member in it raised an error when commanded. Entity Guard treats that as a harmless skip (the group is correct) instead of a failure — it does not climb toward the red error banner. The dead members (capped, with a "+N more" tail past three) ride the event payload and show on the rule's status card. | Light group commanded off; one bulb is offline and raises, but the group already reads off. | `entity_guard_skipped` (`member_unavailable`, `members`). No error climb. |
| **skipped** · `service_call_failed` | the command was sent but the service raised an error or timed out. Unlike the others, this is a genuine failure: it carries the error, counts toward the error threshold, and enough in a row turns the rule's card red. Recorded with outcome "failed" (every other reason records "skipped"). | Switch rule enforces on; the integration throws on the `turn_on` call. | `entity_guard_skipped` (`service_call_failed`, `error`). Outcome "failed"; climbs toward red banner. |
| **skipped** · `unreachable_after_retries` | verify only. The target was re-commanded up to `VERIFY_MAX_ATTEMPTS` (2) correctives and never reached the desired state, with no error raised — silent non-compliance (a device that acknowledges the command but doesn't move). Fires alongside the `unreachable` event, and is throttled to once per dead-device streak so it doesn't flood. | Dead bulb commanded on, then re-commanded 2× (VERIFY_MAX_ATTEMPTS), still off. | `entity_guard_skipped` (`unreachable_after_retries`). Also fires `unreachable`. |
| **loop_detected** · `rate_limit` | Counted enforces in a 60s window exceed `max_enforcements_per_minute` → rule self-suppresses. Counted = real triggers + self-heal re-enters; the verify cycle's own correctives are not counted. | Light flaps 11×/min, limit 10. | Fires `entity_guard_loop_detected`; payload `cause="rate_limit"`, plus `suppressed_until` and `limit` (the `max_enforcements_per_minute` it breached). Rule auto-suppressed 15 min. |
| **loop_detected** · `unreachable_loop` | Dead-device kill-switch (**verify only**): a verify target never reached desired after `CONSECUTIVE_EXHAUSTION_LIMIT` (5) self-heal cycles. Backstop for when the rate limit is high or off — a low `max_enforcements_per_minute` trips `rate_limit` first (whichever threshold is lower wins). | Verify rule on a dead bulb exhausts 5 full self-heal cycles. | Fires `entity_guard_loop_detected`; payload `cause="unreachable_loop"`, plus `suppressed_until` and `limit`. Note `limit` is still `max_enforcements_per_minute`, not what tripped this path (the 5-cycle `CONSECUTIVE_EXHAUSTION_LIMIT`). Rule auto-suppressed 15 min. |
| **suppressed** | User calls `entity_guard.suppress` (manual pause); duration 1–1440 min (max 24h). For longer, disable the rule's switch (indefinite, no auto-resume). | `suppress` for 480 min (8h) overnight. | Fires `entity_guard_suppressed`; payload `suppressed_until` + `user_id`. Rule paused (all targets, pending enforces cancelled) until then. |
| **verified** | Verify-on + target re-read **reached desired**. State-based (doesn't care who moved it). One pulse at confirm. `attempts`: 1 = first read, N+1 = after N correctives. | Force switch off → engine enforces on → re-read sees on. | `entity_guard_enforced_verified` (attempts). Confirmed success. |
| **unreachable** | Verify-on + target **never reached desired** after `VERIFY_MAX_ATTEMPTS` (2) correctives, service call did NOT raise (silent non-compliance). Fires on the first failure, then stays quiet until the target recovers (so repeated failures don't spam the event). | Dead bulb commanded on, re-commanded 2×, still off. | `entity_guard_enforced_unreachable` (reason `unreachable_after_retries`). Also fires `skipped` (`unreachable_after_retries`). |
| **cancelled** | Verify-on + an armed verify cycle **abandoned mid-window before any verdict** — an external condition changed (the rule was disabled, master went off, it was suppressed, or its conditions lapsed). Not a device fault, not a success; no verdict was reached. Fires once at the drop. A further reason, `unloaded` (on teardown), drops silently. | Rule disabled, or the hub master switched off, while a verify cycle is armed; a `suppress` call lands mid-window; or a condition the rule depends on (e.g. "only while Home") flips to false before the re-read confirms. | `entity_guard_enforced_cancelled` (reason). |

> **Gate automations on `verified`, not `enforced`.** Under verify, `enforced`
> is provisional — the command was sent but not yet confirmed landed.

---

## 3. The verify lifecycle

When verify is enabled, a successful enforce **arms a verify cycle** instead of
ending there. The cycle resolves exactly one way:

```
enforce (verify on)
   │
   ├─ target reaches desired ──────────────► VERIFIED        (success)
   │
   ├─ VERIFY_MAX_ATTEMPTS (2) correctives, still not reached ──► UNREACHABLE (silent fail)
   │
   └─ external condition changes mid-window ──────────────► CANCELLED   (no verdict)
```

**Confirmed fast or slow, same verdict.** A cycle confirms the instant the
target reports it reached desired, or at a short fallback deadline (~5 s) if no
report arrives first. A fast device confirms immediately; a slow one confirms at
the deadline. Either way the verdict is the same — timing changes only how long
it takes, never the outcome. Manual force is no different: a forced run verifies
for real (it only skips the rule's conditions), confirming the same two ways.

**Example — manual force on a "waiting on conditions" rule (verify on).**
A manual force bypasses the rule's conditions, so the cycle runs to a real
verdict instead of dropping `cancelled(flags_unmet)`. Shown below as
`status sensor` (raw key) → `card badge` (friendly label).

*Fast target* — obeys in-tick, verify confirms same-tick, no `verifying`:

| step | status sensor | card badge |
|---|---|---|
| before press | `conditional` | Waiting on conditions |
| force fires enforce | `enforcing` | Enforcing |
| device obeys in-tick, verify confirms same tick | — | — |
| done, conditions still unmet | `conditional` | Waiting on conditions |

*Slow target* — same press, parks at `verifying` for the deadline (~5 s):

| step | status sensor | card badge |
|---|---|---|
| before press | `conditional` | Waiting on conditions |
| force fires enforce | `enforcing` | Enforcing |
| no in-tick ACK → verify parks | `verifying` | Verifying |
| ~5 s: state event lands reached → `verified` | — | — |
| done, conditions still unmet | `conditional` | Waiting on conditions |

Same outcome (`VERIFIED`); the only difference is whether `verifying` is
visible — it parks **only** when a target doesn't confirm in-tick.

---

## 4. `cancelled` — the four reasons

Checked in this order; first match wins; **all checked before the reached-check**
(a target that already reached desired is still dropped if a reason trips):

| Order | reason | Condition | Fires event? | Bypassed by force? |
|---|---|---|---|---|
| 0 | `unloaded` | integration tearing down | **No** — silent | No |
| 1 | `disabled` | rule's own switch off during window | Yes | **No** — drops even forced (can't re-enable an off rule) |
| 2 | `master_off` | hub master switch off during window | Yes | **No** — same |
| 3 | `suppressed` | suppression active during window | Yes | **Yes** — force skips it |
| 4 | `flags_unmet` | the rule's conditions lapsed mid-window | Yes | **Yes** — force skips it |

`unloaded` is checked first but fires **no** `cancelled` event (the entry is
going away), so there are **four visible reasons, not five**.

**reached-but-dropped.** A target that physically reached desired still resolves
`CANCELLED` if a reason tripped mid-window (e.g. conditions lapse + reached →
`cancelled(flags_unmet)`, not `verified`). The device being correct does not
save the cycle.

---

## 5. Forced cycles (manual `test_enforce`)

A manual force (the `test_enforce` service, or the card button) runs the rule
against every target immediately, overriding the conditions. A forced cycle
**judges the device only**:

- it skips the `suppressed` and `flags_unmet` drops — so if verify is on, the
  cycle still runs and resolves to a real `VERIFIED` or `UNREACHABLE` instead of
  dropping `cancelled`;
- it still honors `disabled` and `master_off` — a force can test a device but
  cannot act for a rule whose own switch, or the hub master, is off.

A force only verifies if the rule has **verify enabled** (and is a state-mode
rule). Force overrides the conditions, not the verify setting — on a verify-off
rule the force just enforces once and stops, with no `verifying` step and no
verdict.

Once a forced cycle self-heals into an automatic re-enforce, it **drops the
forced status** so the re-enter no longer bypasses anything.

A forced run fires the same events as an automatic one (`enforced`, and when
verify is on, `verified` / `unreachable`), each carrying `forced: true` so an
automation can tell a manual test run from a real trigger and act only on one.

---

## 6. Suppression

`entity_guard.suppress` pauses a rule for a set duration (1–1440 minutes, max
24 h). It is **rule-scoped** — it covers all of the rule's target entities at
once and cancels any in-flight pending enforcements, so a rule about to fire is
stopped. The `suppressed` event's `entity_id` is the rule's own status sensor
(suppression isn't tied to one target), the unambiguous id for logbook and
trigger filters.

Suppression catches a rule at any point in its act-and-verify cycle, with one
exception — a command already on the wire:

| Rule state when suppressed | Stopped? | What happens |
|---|---|---|
| About to enforce (delay countdown) | **Yes** | the pending enforce is cancelled before it fires |
| Enforcing (command in flight) | **No** | a command already sent runs to completion — suppression can't recall it |
| About to verify (command sent, verify arming) | **Yes** | the verify arms but never confirms — it resolves `cancelled`, not `verified` |
| Verifying (verify cycle armed) | **Yes** | at its next check the cycle sees the suppression and ends `cancelled`, no `verified` |

A manually forced cycle is the exception to the last two rows: it judges the
device regardless of suppression, still resolving to a real `verified` or
`unreachable`.

`loop_detected` is the engine's own automatic suppression: when enforcements
exceed `max_enforcements_per_minute`, the rule suppresses itself and emits
`loop_detected` with `suppressed_until` and `limit`.

---

## 7. Conditions (a.k.a. flags)

A rule may carry optional **conditions** — extra entity/state pairs that must
**all** be true for the rule to act (config flow: "Conditions (optional)"). A
condition whose entity is offline (`unavailable`/`unknown`) or missing counts
as **unmet**, so the rule holds off rather than acting on a half-known state.
When a condition goes unmet during a verify window the cycle drops with reason
`flags_unmet`.

> **Terminology note.** "conditions" is the user-facing term throughout the UI.
> Internally the code calls these `flags` (the config keys, the `Flag` model,
> `flags_match`) — same feature, two names: one for users, one in the source.

---

## 8. Dead-device flood guard

On a dead device the verify cycle would exhaust and retry every ~5 s. To keep
that off the event bus, `unreachable` and its sibling
`skipped(unreachable_after_retries)` fire **once per fresh exhaustion streak** —
one pulse when the target first goes silent, then quiet however long it stays
dead. A real recovery resets the streak, so a later death fires them again.
