#!/usr/bin/env python3
"""Smoke test: dynamic_attribute color rule does NOT power an OFF light on.

Bug (#48): the enforce-time color guard that skips a color write when the
target light is off only applied to fixed_attribute rules; a dynamic_attribute
rule tracking rgb_color could call light.turn_on and turn an off light ON.
Fix widened the guard to dynamic_attribute too.

Strategy: create a dynamic_attribute rule that tracks rgb_color from a source
light onto an OFF target light. Drive the source's rgb_color. The guard must
SKIP (target stays off) — never turn the off target on.

Reuses smoke_follow.py helpers. Targets are from the allowed smoke set.
Usage: docker exec serene_booth python3 \
  /workspaces/home-assistant-core/config/smoke_color_guard.py
Exit 0 = pass, 1 = fail.
"""

from __future__ import annotations

import sys
import time

sys.path.insert(0, "/workspaces/home-assistant-core/config")
import smoke_follow as sf  # shared helpers, resolved inside the container

SOURCE = "light.living_room_diffuser"  # allowed; supplies rgb_color
TARGET = "light.living_room_under_cabinet_lights"  # allowed; kept OFF
MASTER = "switch.entity_guard_hub_master_enabled"
MASTER_WAS: str | None = None


def main() -> int:
    token = sf.mint_token()

    global MASTER_WAS
    MASTER_WAS = sf.get_state(token, MASTER).get("state")
    if MASTER_WAS != "on":
        sf.api(token, "POST", "/api/services/switch/turn_on", {"entity_id": MASTER})
        time.sleep(2)

    # Source carries an rgb_color; TARGET starts OFF (the condition under test).
    sf.api(
        token,
        "POST",
        "/api/services/light/turn_on",
        {"entity_id": SOURCE, "rgb_color": [255, 0, 0]},
    )
    sf.api(token, "POST", "/api/services/light/turn_off", {"entity_id": TARGET})
    time.sleep(1)

    run_token = str(int(time.time()))
    rule_name = f"EG Color Guard Smoke {run_token}"
    print(f"Creating dynamic_attribute rgb_color rule '{rule_name}'…")
    entry_id = sf.create_dynamic_attribute_rule(
        token,
        rule_name,
        SOURCE,
        [TARGET],
        target_attribute="rgb_color",
        value_from="attribute",
        source_attribute="rgb_color",
    )
    print(f"  created entry {entry_id}")

    try:
        # Wait past startup grace so the rule is actually enforcing.
        armed = sf.wait_for_armed(token, "dynamic_attribute", [TARGET])
        sf.check("rule armed past startup grace", armed, "never armed")

        # Ensure target is OFF right before we provoke enforcement.
        sf.api(token, "POST", "/api/services/light/turn_off", {"entity_id": TARGET})
        time.sleep(1)
        sf.check(
            "pre: target is OFF",
            sf.get_state(token, TARGET).get("state") == "off",
            f"target={sf.get_state(token, TARGET).get('state')}",
        )

        # Drive the source color — this would (pre-fix) enforce rgb onto the
        # OFF target via light.turn_on, powering it on. The guard must skip.
        sf.api(
            token,
            "POST",
            "/api/services/light/turn_on",
            {"entity_id": SOURCE, "rgb_color": [0, 128, 255]},
        )
        time.sleep(5)  # give the engine ample time to (wrongly) enforce

        final = sf.get_state(token, TARGET).get("state")
        sf.check(
            "target still OFF — color guard skipped (not powered on)",
            final == "off",
            f"target={final} — guard did NOT skip; off light wrongly turned on",
        )

    finally:
        print("Cleanup: delete rule, restore")
        for eid in dict.fromkeys([entry_id, *sf.CREATED]):
            try:
                sf.api(token, "DELETE", f"/api/config/config_entries/entry/{eid}")
            except Exception as e:
                print(f"  [WARN] cleanup failed for {eid}: {e}")
        try:
            sf.api(token, "POST", "/api/services/light/turn_off", {"entity_id": TARGET})
        except Exception as e:
            print(f"  [WARN] target reset failed: {e}")
        if MASTER_WAS and MASTER_WAS != "on":
            try:
                sf.api(
                    token,
                    "POST",
                    "/api/services/switch/turn_off",
                    {"entity_id": MASTER},
                )
            except Exception as e:
                print(f"  [WARN] master restore failed: {e}")

    if sf.FAILS:
        print(f"\nSMOKE RESULT: FAILED — {sf.FAILS}")
        return 1
    print("\nSMOKE RESULT: PASSED — dynamic_attribute color guard skips off light")
    return 0


if __name__ == "__main__":
    sys.exit(main())
