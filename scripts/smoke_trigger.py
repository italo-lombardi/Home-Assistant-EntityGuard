"""Live smoke for the NEW integration-level trigger platform (trigger.py) —
run INSIDE the serene_booth container.

Entity Guard's seven rule events are now pickable as integration-level triggers
(`platform: entity_guard.<type>`) in the generic trigger picker, each with an
optional `config_entry_id` that scopes the trigger to ONE rule. This proves the
real `async_get_triggers` → Trigger → `async_attach_runner` path end to end over
a live HA — what tests/test_trigger.py mocks.

Two scopings, both forced through the VERIFIED event (forceable on a compliant
device: paint the target to the non-desired state → the engine enforces desired
via a real service → the verify re-read confirms → EVENT_VERIFIED):

  1. ANY-RULE — trigger with NO config_entry_id fires for the event regardless
     of which rule produced it. This is the capability device triggers cannot
     offer and the reason the platform exists.
  2. PER-RULE — trigger scoped to rule A's config entry fires for A's event but
     NOT for rule B's. Proves the entry_id → unique_id → rule_id filter.

Run inside the container:
  docker cp scripts/smoke_trigger.py \
    serene_booth:/workspaces/home-assistant-core/config/smoke_trigger.py
  docker exec serene_booth /home/vscode/.local/ha-venv/bin/python \
    /workspaces/home-assistant-core/config/smoke_trigger.py
Exit 0 = all hard assertions green, 1 = a failure.
"""

from __future__ import annotations

import sys
import time

sys.path.insert(0, "/workspaces/home-assistant-core/config")
import smoke_follow as sf  # shared helpers, resolved inside the container
import smoke_verify_enforce as sv  # reuse create_verify_rule + force_state

TARGET = sv.TARGET
# Action sink the trigger-built automations flip. A real input_boolean, distinct
# from the rule's trigger/source so flipping it can't feed back into the rule.
SINK = "input_boolean.ea_ctrl_kitchen_2"


def install_trigger_automation(
    token: str, aid: str, trigger_type: str, config_entry_id: str | None
) -> None:
    """Register an automation whose ONLY trigger is an integration trigger
    (`platform: entity_guard.<type>`), optionally scoped to one rule, action =
    turn SINK on. Reload so HA attaches it — the real async_attach_runner path."""
    trig: dict = {"platform": f"entity_guard.{trigger_type}"}
    if config_entry_id is not None:
        trig["config_entry_id"] = config_entry_id
    cfg = {
        "id": aid,
        "alias": aid,
        "trigger": [trig],
        "action": [{"service": "input_boolean.turn_on", "target": {"entity_id": SINK}}],
    }
    r = sf.api(token, "POST", f"/api/config/automation/config/{aid}", cfg)
    if r.get("result") != "ok":
        raise RuntimeError(f"automation create for {trigger_type} failed: {r}")
    sf.api(token, "POST", "/api/services/automation/reload", {})
    time.sleep(3)


def delete_automation(token: str, aid: str) -> None:
    try:
        sf.api(token, "DELETE", f"/api/config/automation/config/{aid}")
        sf.api(token, "POST", "/api/services/automation/reload", {})
    except Exception as e:
        print(f"  [WARN] automation cleanup failed for {aid}: {e}")


def reset_sink(token: str) -> None:
    sf.api(token, "POST", "/api/services/input_boolean/turn_off", {"entity_id": SINK})
    for _ in range(10):
        if sf.get_state(token, SINK).get("state") == "off":
            return
        time.sleep(0.5)


def sink_turned_on(token: str, timeout: int = 25) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if sf.get_state(token, SINK).get("state") == "on":
            return True
        time.sleep(1)
    return False


def force_verified(token: str) -> None:
    """Trip a verify-enabled rule into EVENT_VERIFIED without driving the target
    ourselves: paint it to the non-desired state; the engine enforces desired via
    a real service and the verify re-read confirms."""
    sv.force_state(token, TARGET, "off")


def main() -> int:
    token = sf.mint_token()
    sf.cleanup(token)

    autos: list[str] = []

    # Two verify-enabled rules on the SAME target so a per-rule trigger has a
    # sibling event to prove it filters OUT.
    name_a = "ea_smoke_inttrig_a"
    name_b = "ea_smoke_inttrig_b"
    entry_a = sv.create_verify_rule(token, name_a, verify=True)
    entry_b = sv.create_verify_rule(token, name_b, verify=True)
    sf.check("two verify-enabled rules created", bool(entry_a) and bool(entry_b))
    sv.status_sensor_for(token, name_a)
    sv.status_sensor_for(token, name_b)
    sf.check(
        "rules left startup grace (armed)",
        sf.wait_for_armed(token, "fixed_state", [TARGET]),
    )

    # --- (1) ANY-RULE: no config_entry_id → fires for any rule's event ---------
    reset_sink(token)
    aid_any = "eg_smoke_inttrig_any"
    autos.append(aid_any)
    install_trigger_automation(token, aid_any, "verified", config_entry_id=None)
    sf.check(
        "any-rule automation installed (platform entity_guard.verified accepted)",
        sf.get_state(token, f"automation.{aid_any}").get("state") == "on",
    )
    force_verified(token)
    sf.check("any-rule trigger fired its automation", sink_turned_on(token))
    delete_automation(token, aid_any)

    # --- (2) PER-RULE: scoped to rule A fires for A, not for B -----------------
    reset_sink(token)
    aid_scoped = "eg_smoke_inttrig_scoped"
    autos.append(aid_scoped)
    install_trigger_automation(token, aid_scoped, "verified", config_entry_id=entry_a)
    sf.check(
        "scoped automation installed",
        sf.get_state(token, f"automation.{aid_scoped}").get("state") == "on",
    )
    # Rule A's verify fires → scoped trigger fires.
    force_verified(token)
    fired_for_a = sink_turned_on(token)
    sf.check("per-rule trigger fired for its own rule (A)", fired_for_a)

    # Now prove it does NOT fire for rule B. Disable A so only B can enforce the
    # target, reset the sink, trip verified again: only B's event is on the bus.
    reset_sink(token)
    sf.api(
        token,
        "POST",
        "/api/services/switch/turn_off",
        {"entity_id": f"switch.{name_a}_enabled"},
    )
    # Wait for A's disable to land, not a blind sleep — else force_verified can
    # race the switch and A still enforces, masking a real leak.
    sf.check(
        "rule A disabled before B-isolation check",
        sf.wait_for_state(token, f"switch.{name_a}_enabled", "off"),
    )
    force_verified(token)
    # Give B's event the same window the positive case got, then assert SINK quiet.
    leaked = sink_turned_on(token, timeout=15)
    sf.check("per-rule trigger did NOT fire for a different rule (B)", not leaked)

    for aid in autos:
        delete_automation(token, aid)
    reset_sink(token)
    sf.cleanup(token)

    if sf.FAILS:
        print(f"\nFAILED: {sf.FAILS}")
        return 1
    print("\nALL GREEN")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
