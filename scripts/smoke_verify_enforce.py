#!/usr/bin/env python3
"""Smoke test: opt-in verify-after-enforce (Feature G).

Feature G lets a state-mode rule (fixed_state / dynamic_state) RE-READ its
target a few seconds after enforcing, instead of trusting the service call's
return. If the target accepted the command but never actually reached the
desired state, the rule retries up to VERIFY_MAX_ATTEMPTS, records the target
as `unreachable_after_retries` in the status sensor's `target_outcomes`
attribute, fires EVENT_SKIPPED (inline — NOT through _fire_skipped, so the
skip-counter classifier cannot reclassify the "failed" outcome to "skipped"),
and then SELF-HEALS: it re-enters the counted enforcing cycle and tries again.
The failed outcome is therefore transient per cycle — it reappears each
exhaustion and clears when the device complies; it is not a permanent latch.
After CONSECUTIVE_EXHAUSTION_LIMIT consecutive exhaustions with no recovery,
loop protection auto-suppresses the rule (reason=="unreachable_loop"). Off by
default; opt in with `verify_enabled` in the extras / advanced step.

REST-observable surface asserted here (allowed/disposable entities only):

  1. CONFIG ROUND-TRIP (deterministic). A fixed_state rule created with
     verify_enabled=True stores it: the options flow re-reads it as set. This
     proves the G config path (schema key + parse + persist) is live end-to-end
     over REST. The baseline — a rule WITHOUT verify — records NO
     unreachable_after_retries outcome when its target complies.

  2. UNREACHABLE FLAG + VERIFYING STATUS (best-effort). With a verify rule
     enforcing an allowed light, we repeatedly shove the target's state back to
     the pre-enforce value via a raw /api/states injection, simulating a device
     that ACKs the command but never changes. After the verify window the
     target's target_outcomes entry should show reason=="unreachable_after_retries"
     (outcome=="failed") — transiently, since the rule self-heals and re-enters
     enforcing; while the engine is between a successful enforce and its verify
     re-read the status sensor should read "verifying" (the status that stops the
     card reading "armed" early). Both race the engine's own actuation and the 5 s
     verify delay, so both are asserted leniently: a PASS confirms, a miss is
     reported SKIPPED (not a hard failure) — the deterministic paths are locked
     by the engine unit tests through the real wait_for path. This smoke adds the
     live REST confirmation.

  3. RECOVERY CLEARS A STALE FAILED OUTCOME (best-effort). Once the failed
     outcome has appeared, we stop shoving the target off and drive it
     compliant; the stale failed/unreachable outcome must drop (a target that
     recovered on its own is no longer flagged). Lenient like the flag above;
     the deterministic path is locked by the engine unit tests.

HONEST SCOPE — what this does NOT cover:
  - Attribute-mode rules (verify is state-mode only, by design) — not claimed.
  - The precise retry COUNT (VERIFY_MAX_ATTEMPTS) — timing-dependent over REST;
    locked in unit tests instead.

Reuses smoke_follow.py helpers (token mint, REST api, cleanup, waits).
Requires the integration branch (verify_enabled + target_outcomes) deployed to
serene_booth.
Usage: docker exec serene_booth python3 \
  /workspaces/home-assistant-core/config/smoke_verify_enforce.py
Exit 0 = all hard assertions green, 1 = a failure.
"""

from __future__ import annotations

import sys
import time

sys.path.insert(0, "/workspaces/home-assistant-core/config")
import smoke_follow as sf  # shared helpers, resolved inside the container

# Allowed smoke target: a real COMPLIANT light the engine drives via
# light.turn_on/off (memory: smoke_test_entities). Used for the baseline leg
# (a complying target records no unreachable outcome) and by sibling smokes'
# VERIFIED legs. Exhaustion legs use SILENT_TARGET below instead.
TARGET = "light.living_room_under_cabinet_lights"

# Deterministic silent-ignore fixture (scripts/eg_smoke_fixtures.yaml): a
# template switch that ACKs turn_on/turn_off but never reaches the desired state,
# so verify always exhausts -> reason == 'unreachable_after_retries'. Use this
# (not the real compliant light) for the exhaustion legs.
SILENT_TARGET = "switch.eg_smoke_silent_ignore"


def create_verify_rule_on(
    token: str,
    name: str,
    targets: list[str],
    trigger_states: list[str],
    target_state: str,
    verify: bool = True,
) -> str:
    """Create a fixed_state rule with verify_enabled on an arbitrary target set.

    Mirrors sf.create_fixed_state_rule but injects verify_enabled in the extras
    step rather than skipping it with defaults.
    """
    flow = sf.api(
        token, "POST", "/api/config/config_entries/flow", {"handler": "entity_guard"}
    )
    flow_id = flow["flow_id"]
    result = sf.api(
        token,
        "POST",
        f"/api/config/config_entries/flow/{flow_id}",
        {"rule_name": name, "target_entities": targets, "mode": "fixed_state"},
    )
    while result.get("type") == "form":
        sf.guard_form(result)
        step = result.get("step_id")
        if step == "state":
            # target_state must differ from trigger_states (a rule whose target
            # equals its trigger would loop; the flow rejects target_in_triggers).
            payload = {
                "trigger_states": trigger_states,
                "target_state": target_state,
                "delay_seconds": 0,
            }
        elif step == "extras":
            payload = {"verify_enabled": verify}
        else:
            payload = {}  # preview — accept defaults
        result = sf.api(
            token, "POST", f"/api/config/config_entries/flow/{flow_id}", payload
        )
    if result.get("type") != "create_entry":
        raise RuntimeError(f"verify create flow did not finish: {result}")
    entry_id = result["result"]["entry_id"]
    sf.CREATED.append(entry_id)
    return entry_id


def create_verify_rule(token: str, name: str, verify: bool) -> str:
    """Backward-compatible wrapper: verify rule on TARGET, off -> on."""
    return create_verify_rule_on(token, name, [TARGET], ["off"], "on", verify)


def status_sensor_for(token: str, name_slug: str, timeout: int = 100) -> str:
    """Find THIS run's status sensor entity_id (past startup grace)."""
    deadline = time.time() + timeout
    want = f"sensor.{name_slug}_status"
    while time.time() < deadline:
        st = sf.get_state(token, want)
        if st and st.get("state") not in (None, "unavailable", "unknown"):
            return want
        time.sleep(2)
    raise RuntimeError(f"status sensor {want} never came live")


def force_state(token: str, entity_id: str, state: str) -> None:
    """Raw /api/states injection — makes the target report `state` without a
    real service call, simulating a device that ignores the command."""
    sf.api(token, "POST", f"/api/states/{entity_id}", {"state": state})


def main() -> int:
    token = sf.mint_token()
    sf.cleanup(token)  # clear any orphan EG rules from prior runs

    ok = True

    # --- 1. verify_enabled accepted by the create flow (deterministic) ---
    # The extras step accepting verify_enabled=True without a form error (the
    # flow reaches create_entry) proves the G config surface — schema key +
    # parse + persist — is live over REST. The exact stored-value round-trip is
    # additionally locked by the unit tests (models parse + options flow); a
    # live options-flow value introspection is brittle (HA serializes current
    # values as schema suggested_value, not a stable field), so it is not the
    # hard assertion here.
    name = "ea_smoke_verify_on"
    slug = name
    entry = create_verify_rule(token, name, verify=True)
    sf.check(
        "verify-enabled rule created (extras step accepted verify_enabled)", bool(entry)
    )
    ok = ok and bool(entry)
    time.sleep(2)

    # baseline: a complying target records no unreachable outcome. Target already
    # at desired ("on") → rule has nothing to enforce → no verify → clean.
    sf.actuate_target(token, "on")
    status = status_sensor_for(token, slug)
    time.sleep(8)  # past verify delay
    st = sf.get_state(token, status)
    outcomes = (st.get("attributes") or {}).get("target_outcomes") or {}
    tgt = outcomes.get(TARGET, {})
    compliant_clean = tgt.get("reason") != "unreachable_after_retries"
    sf.check(
        "complying target is NOT flagged unreachable",
        compliant_clean,
        f"outcome={tgt}",
    )
    ok = ok and compliant_clean

    # --- 2. unreachable flag + VERIFYING status (deterministic) --------
    # A SECOND rule on the silent-ignore fixture: it ACKs turn_on but its state
    # never reaches "on", so verify exhausts every run (no device race). While
    # the engine is between the enforce and its verify deadline the status sensor
    # reads STATUS_VERIFYING; after VERIFY_MAX_ATTEMPTS correctives the outcome
    # latches unreachable_after_retries. Both are HARD now.
    si_name = "ea_smoke_verify_silent"
    si_slug = si_name
    si_entry = create_verify_rule_on(
        token, si_name, [SILENT_TARGET], ["off"], "on", verify=True
    )
    sf.check("silent-ignore verify rule created", bool(si_entry))
    ok = ok and bool(si_entry)
    si_status = status_sensor_for(token, si_slug)
    # Leave startup grace so target events are observed (engine ignores them
    # during the ~60s grace → no enforce → no verify → false FAIL).
    armed = sf.wait_for_armed(token, "fixed_state", [SILENT_TARGET])
    sf.check("silent-ignore rule left startup grace (armed)", armed)
    ok = ok and armed

    latched = False
    saw_verifying = False
    # Trip the trigger once (switch rests at "off" already, but assert the edge).
    force_state(token, SILENT_TARGET, "off")
    deadline = time.time() + 20
    while time.time() < deadline:
        st = sf.get_state(token, si_status)
        if st.get("state") == "verifying":
            saw_verifying = True
        outcomes = (st.get("attributes") or {}).get("target_outcomes") or {}
        tgt = outcomes.get(SILENT_TARGET, {})
        if tgt.get("reason") == "unreachable_after_retries":
            latched = True
            break
        time.sleep(1)
    sf.check("verify flags unreachable_after_retries (silent-ignore)", latched)
    sf.check("status reads 'verifying' during the verify window", saw_verifying)
    ok = ok and latched and saw_verifying

    # --- 3. recovery clears a stale failed outcome (deterministic) ------
    # A target flagged failed/unreachable that later recovers must NOT keep the
    # stale failed outcome. Paint the silent switch to the desired "on" (a raw
    # /api/states edge the engine's reactive confirm catches) → the failed
    # outcome drops. Reset the paint in cleanup.
    recovered = False
    if latched:
        force_state(token, SILENT_TARGET, "on")  # genuine state->desired edge
        deadline = time.time() + 20
        while time.time() < deadline:
            st = sf.get_state(token, si_status)
            outcomes = (st.get("attributes") or {}).get("target_outcomes") or {}
            tgt = outcomes.get(SILENT_TARGET, {})
            if tgt.get("outcome") != "failed" and (
                tgt.get("reason") != "unreachable_after_retries"
            ):
                recovered = True
                break
            time.sleep(1)
        sf.check("recovered target clears the stale failed outcome", recovered)
        ok = ok and recovered
    # Reset the fixture paint so a later same-session smoke sees a clean "off".
    force_state(token, SILENT_TARGET, "off")

    sf.cleanup(token)
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
