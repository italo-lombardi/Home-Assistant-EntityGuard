#!/usr/bin/env python3
"""Repro probe: does recently_enforced_verified pulse for a GROUP target that
went ON->OFF via a real enforce? (serene_booth, REST)

User bug report: an automation gated on
`binary_sensor.<rule>_recently_enforced_verified` off->on never fired. History
CSV showed the base `recently_enforced` sensor pulse, status walk
enforcing->conditional->verifying->conditional, but the VERIFIED twin stayed
off the whole run. Target was a light GROUP (light.global_all_lights) that was
ON and the enforce turned it OFF — a genuine transition verify should confirm.

HYPOTHESIS under test: for a group target, the verify event-confirm in
_handle_state_event gates on `entity_id in target_entities` matching the GROUP
entity's own reached state_changed. If that group-level reached event does not
land while the verify is pending (ordering: the group's on->off dispatch races
the _arm_verify continuation, or the group re-derives from member events that
are NOT in target_entities), the event-confirm is skipped and only the ~5s
deadline timer can confirm — and if something pops _pending_verifications early
without confirming, the verified sensor never arms.

This probe drives the exact shape: a 2-member live light group, group forced
ON, verify-ON fixed_state rule (trigger on 'on', enforce 'off'), then
test_enforce. It asserts the verified sensor pulses ON within the deadline.
A FAIL here reproduces the user bug on serene_booth; a PASS means the prod
failure is environment-specific (dead member, timing) and needs the CSV's
exact member states.

Run inside the container:
  docker cp scripts/smoke_group_verified_pulse.py \
    serene_booth:/workspaces/home-assistant-core/config/smoke_group_verified_pulse.py
  docker exec serene_booth /home/vscode/.local/ha-venv/bin/python \
    /workspaces/home-assistant-core/config/smoke_group_verified_pulse.py
Exit 0 = verified pulsed (no repro), 1 = did NOT pulse (bug reproduced) or setup fail.
"""

from __future__ import annotations

import sys
import time

sys.path.insert(0, "/workspaces/home-assistant-core/config")
import smoke_follow as sf
import smoke_group_dead_member as sg  # make_light_group, create_verify_rule_on
import smoke_verify_enforce as sv  # force_state, status_sensor_for

M_A = "light.living_room_under_cabinet_lights"  # live member
M_B = "light.living_room_diffuser"  # second member (seeded live)
POLL = 0.2


def _sweep_orphan_groups(token: str, name_prefix: str) -> None:
    """Delete leftover disposable group-helper entries from a killed prior run.

    make_light_group resolves the group entity by member-list match; two live
    helpers over the same members (a fresh one + an orphan a killed run never
    tore down) make that match ambiguous — it can bind the rule to the stale
    group, which then never leaves startup grace. Clear them first so each run
    starts from a clean slate (mirrors sf.cleanup for EG rules).
    """
    for e in sf.api(token, "GET", "/api/config/config_entries/entry") or []:
        if e.get("domain") == "group" and str(e.get("title", "")).startswith(
            name_prefix
        ):
            try:
                sf.api(
                    token, "DELETE", f"/api/config/config_entries/entry/{e['entry_id']}"
                )
            except Exception as err:
                print(f"WARN: orphan group sweep failed for {e.get('title')}: {err}")


def main() -> int:
    token = sf.mint_token()
    sf.cleanup(token)
    # Clear ALL disposable smoke group helpers (ea_smoke_grp*), not only this
    # probe's own ea_smoke_grp_v: make_light_group resolves the group by member
    # list, and the sibling dead-member smoke builds ea_smoke_grp over the SAME
    # two lights — a leftover from either run makes the match ambiguous and can
    # bind the rule to the stale group (never leaves startup grace).
    _sweep_orphan_groups(token, "ea_smoke_grp")

    name = "ea_smoke_grp_verified"
    slug = name
    verified_eid = f"binary_sensor.{slug}_recently_enforced_verified"
    base_eid = f"binary_sensor.{slug}_recently_enforced"

    entry_id = None
    rule_entry = None
    try:
        # Both members live, then group ON (the ON->OFF transition verify confirms).
        sv.force_state(token, M_A, "off")
        sv.force_state(token, M_B, "off")
        time.sleep(1.0)
        entry_id, gid = sg.make_light_group(token, "ea_smoke_grp_v", [M_A, M_B])
        sf.check("disposable light group created", bool(gid))

        # Verify-ON rule: trigger when group reads 'on', enforce 'off'.
        rule_entry = sg.create_verify_rule_on(token, name, [gid], ["on"], "off")
        sf.check("verify-ON group rule created", bool(rule_entry))
        status_eid = sv.status_sensor_for(token, slug)
        armed = sf.wait_for_armed(token, "fixed_state", [gid])
        sf.check("group rule left startup grace (armed)", armed)

        # Confirm the verified twin even EXISTS (verify-on should create it).
        vpre = sf.get_state(token, verified_eid)
        sf.check(
            "recently_enforced_verified sensor present (verify on)",
            bool(vpre),
            f"{verified_eid} missing — verify twin not created",
        )

        # Drive the group ON (both members on -> any-on aggregate 'on' = violates
        # desired 'off'), so the pending test_enforce is a REAL on->off correction.
        # Poll for the aggregate to re-derive rather than a blind sleep: the group
        # integration recomputes off a member state_changed, which lags the two
        # force_state writes by an unbounded amount on a busy loop (back-to-back
        # runs false-FAILed here on a fixed 1s).
        sv.force_state(token, M_A, "on")
        sv.force_state(token, M_B, "on")
        agg = None
        gdl = time.time() + 10
        while time.time() < gdl:
            agg = sf.get_state(token, gid)
            if agg and agg.get("state") == "on":
                break
            time.sleep(POLL)
        sf.check(
            "group reads 'on' before enforce (real transition to confirm)",
            agg and agg.get("state") == "on",
            f"agg={agg.get('state') if agg else None}",
        )

        # Fire the enforce exactly as the user did: the Test Enforce button.
        sf.api(
            token,
            "POST",
            "/api/services/button/press",
            {"entity_id": f"button.{slug}_test_enforce"},
        )

        # Watch BOTH pulse sensors + status for the full deadline + pulse window.
        saw_base = False
        saw_verified = False
        saw_verifying = False
        statuses = []
        deadline = time.time() + 15
        while time.time() < deadline:
            if sf.get_state(token, base_eid).get("state") == "on":
                saw_base = True
            if sf.get_state(token, verified_eid).get("state") == "on":
                saw_verified = True
            st = sf.get_state(token, status_eid).get("state")
            if st == "verifying":
                saw_verifying = True
            if st and (not statuses or statuses[-1] != st):
                statuses.append(st)
            if saw_verified:
                break
            time.sleep(POLL)

        sf.check("base recently_enforced pulsed ON (sanity)", saw_base)
        sf.check("status passed through verifying (verify ran)", saw_verifying)
        # THE BUG CHECK: the verified twin MUST pulse for a confirmed group enforce.
        sf.check(
            "recently_enforced_verified pulsed ON for a confirmed group enforce",
            saw_verified,
            f"verified never armed; status walk={statuses}",
        )
    finally:
        # Tear down the rule first, then the group helper entry.
        sf.cleanup(token)
        if entry_id:
            sf.api(token, "DELETE", f"/api/config/config_entries/entry/{entry_id}")

    if sf.FAILS:
        print(f"\nFAILED: {sf.FAILS}")
        return 1
    print("\nALL GREEN")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
