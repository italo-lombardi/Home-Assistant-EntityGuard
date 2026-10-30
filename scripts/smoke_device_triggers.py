"""Live smoke for the NEW device-automation triggers (verified / unreachable /
suppressed) added in v0.4.x — run INSIDE the serene_booth container.

Entity Guard fires six rule-scoped events; three (enforced / skipped /
loop_detected) already had device triggers. This proves the three NEW ones are
real, attachable device triggers end to end: an automation built with
`platform: device, domain: entity_guard, type: <new>` on a rule's device actually
FIRES when that rule produces the event. That exercises `async_attach_trigger`'s
event_trigger delegation + the rule_id filter over a live HA — the path a unit
test mocks.

Two of the three are forceable against a compliant device, so they fire live:

  1. VERIFIED — verify-ON rule + a genuine enforce → the verify re-read confirms
     → EVENT_VERIFIED. Forced by painting the target to the non-desired state
     (engine enforces the desired state via a real service → confirm).
  2. SUPPRESSED — the `entity_guard.suppress` service fires EVENT_SUPPRESSED
     directly. Forced by calling that service on the rule.

  unreachable — EVENT_UNREACHABLE needs verify EXHAUSTION on a device that
     ignores the command. The container's targets are compliant (they obey
     light.turn_on), so exhaustion can't be forced without a fake non-compliant
     device. That attach path is identical to the other two (same generic
     TRIGGER_TYPE_EVENT lookup → event_trigger) and is unit-locked in
     tests/test_device_trigger.py::test_attach_trigger_fires_on_new_event_types.
     This smoke SKIPS it rather than fake a pass. (Same rationale as
     smoke_unreachable_signal: the signal twin is also unit-locked, not live.)

Run inside the container:
  docker cp scripts/smoke_device_triggers.py \
    serene_booth:/workspaces/home-assistant-core/config/smoke_device_triggers.py
  docker exec serene_booth /home/vscode/.local/ha-venv/bin/python \
    /workspaces/home-assistant-core/config/smoke_device_triggers.py
Exit 0 = all hard assertions green, 1 = a failure.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request

sys.path.insert(0, "/workspaces/home-assistant-core/config")
import smoke_follow as sf  # shared helpers, resolved inside the container
import smoke_verify_enforce as sv  # reuse create_verify_rule + status_sensor_for

TARGET = sv.TARGET
# Action sink the trigger-built automations flip. A real input_boolean, distinct
# from the rule's trigger/source so flipping it can't feed back into the rule.
SINK = "input_boolean.ea_ctrl_kitchen_2"


def render_template(token: str, template: str) -> str:
    """Render a Jinja template via the REST /api/template route (used to resolve
    the rule's device_id from its status sensor — device_id() is template-only)."""
    req = urllib.request.Request(
        f"{sf.BASE}/api/template",
        method="POST",
        data=json.dumps({"template": template}).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read().decode()


def rule_device_id(token: str, name_slug: str) -> str:
    """The rule's DEVICE id — what a device trigger keys on. The rule's status
    sensor belongs to that device, so device_id('sensor.<slug>_status') resolves
    it. (The device identifier is (DOMAIN, entry_id); device triggers take the
    device_id, not the entry_id or the event rule_id.)"""
    dev = render_template(token, f"{{{{ device_id('sensor.{name_slug}_status') }}}}")
    if not dev or dev in ("None", "none"):
        raise RuntimeError(f"could not resolve device_id for {name_slug}")
    return dev


def install_device_trigger_automation(
    token: str, aid: str, device_id: str, trigger_type: str
) -> None:
    """Register an automation whose ONLY trigger is a device trigger of
    `trigger_type` on the rule device, action = turn SINK on. Then reload so HA
    attaches it. This is the real `async_attach_trigger` path under test."""
    cfg = {
        "id": aid,
        "alias": aid,
        "trigger": [
            {
                "platform": "device",
                "domain": "entity_guard",
                "device_id": device_id,
                "type": trigger_type,
            }
        ],
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
    # settle: the state write is async
    for _ in range(10):
        if sf.get_state(token, SINK).get("state") == "off":
            return
        time.sleep(0.5)


def sink_turned_on(token: str, timeout: int = 25) -> bool:
    """Poll SINK until the trigger-built automation flips it on."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if sf.get_state(token, SINK).get("state") == "on":
            return True
        time.sleep(1)
    return False


def main() -> int:
    token = sf.mint_token()
    sf.cleanup(token)

    name = "ea_smoke_devtrig"
    autos: list[str] = []  # automations to tear down at the end

    entry_id = sv.create_verify_rule(token, name, verify=True)
    sf.check("verify-enabled rule created", bool(entry_id))
    sv.status_sensor_for(token, name)  # wait past startup grace
    armed = sf.wait_for_armed(token, "fixed_state", [TARGET])
    sf.check("rule left startup grace (armed)", armed)

    device_id = rule_device_id(token, name)
    sf.check("resolved rule device_id for triggers", bool(device_id))

    # --- (1) VERIFIED device trigger fires live --------------------------------
    reset_sink(token)
    aid_v = "eg_smoke_devtrig_verified"
    autos.append(aid_v)
    install_device_trigger_automation(token, aid_v, device_id, "verified")
    sf.check(
        "verified automation installed (type accepted by device_trigger schema)",
        sf.get_state(token, f"automation.{aid_v}").get("state") == "on",
    )
    # Paint target to the NON-desired state ("off"): trips the trigger (rule:
    # trigger off -> target on) WITHOUT driving the target ourselves. The ENGINE
    # enforces "on" via the real service; the verify re-read confirms "on" ->
    # EVENT_VERIFIED -> our device trigger -> SINK on.
    sv.force_state(token, TARGET, "off")
    sf.check("verified device trigger fired its automation", sink_turned_on(token))

    # --- (2) SUPPRESSED device trigger fires live ------------------------------
    reset_sink(token)
    aid_s = "eg_smoke_devtrig_suppressed"
    autos.append(aid_s)
    install_device_trigger_automation(token, aid_s, device_id, "suppressed")
    sf.check(
        "suppressed automation installed (type accepted by device_trigger schema)",
        sf.get_state(token, f"automation.{aid_s}").get("state") == "on",
    )
    # The suppress service fires EVENT_SUPPRESSED directly — the cleanest forcing
    # path (no loop-protection rate-limit race). The service's rule_id accepts the
    # config entry_id (its fast-path in _resolve_engine), so no unique_id lookup.
    sf.api(
        token,
        "POST",
        "/api/services/entity_guard/suppress",
        {"rule_id": entry_id, "duration_minutes": 1},
    )
    sf.check("suppressed device trigger fired its automation", sink_turned_on(token))
    # Lift the suppression so cleanup/delete of the rule is clean.
    try:
        sf.api(
            token,
            "POST",
            "/api/services/entity_guard/unsuppress",
            {"rule_id": entry_id},
        )
    except Exception as e:
        print(f"  [WARN] unsuppress failed: {e}")

    # --- unreachable: SKIPPED (see module docstring) ---------------------------
    print(
        "  [SKIP] unreachable device trigger — live verify-exhaustion needs a "
        "non-compliant device; attach path is unit-locked "
        "(test_attach_trigger_fires_on_new_event_types)."
    )

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
