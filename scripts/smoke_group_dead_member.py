#!/usr/bin/env python3
"""Live check (serene_booth, REST) for the group dead-member false-error fix.

PR #63 / branch fix/group-dead-member-false-error (engine commit 2e270b7).

THE BUG: a verify-enabled fixed_state rule targeting a light GROUP looped
forever. One blocking group turn-off fans out; a dead member whose integration
RAISES (e.g. "SwitchBot Cloud device is offline") propagates one exception out
of the group call even though the group AGGREGATE already read desired (HA
any-on: N-off + M-unavailable members read 'off'). Pre-fix that raise climbed
consecutive_errors -> sticky STATUS_ERROR + red banner, and the verify
corrective loop never settled. The fix (rule_engine _record_failure): when the
failing target is a group whose aggregate reached desired AND a member is
unavailable, fire a `member_unavailable` SKIP instead of service_call_failed —
no consecutive_errors bump, no STATUS_ERROR.

WHAT IS LIVE-FORCEABLE vs UNIT-LOCKED (honest scope):

  The reclassify branch (_record_failure) only runs when the group service call
  ACTUALLY RAISES or times out. On serene_booth the two allowed lights are
  demo/template lights — light.turn_off on a group of them does NOT raise even
  with a member forced 'unavailable' (no real integration throws). So the
  `member_unavailable` outcome CANNOT be forced live here; that exact branch is
  unit-locked by test_group_dead_member_raise_is_skip_not_error. We still probe
  for it and report member_unavailable if it ever appears (SKIP otherwise, same
  honesty as smoke_unreachable_signal / smoke_verify_enforce).

  The FORCEABLE, user-visible symptom — and the HARD assertion here — is the
  NEGATIVE: a verify-ON group rule with a dead member and the aggregate at
  desired does NOT go sticky STATUS_ERROR and does NOT climb consecutive_errors.
  (No raise -> normal path -> verify sees the aggregate at desired -> confirms
  clean. That is exactly what the user wanted: no false error loop.)

SCENARIOS:
  (a) GROUP + verify-ON off-rule over a DISPOSABLE light group (built live via
      the native `group` light-helper config-entry flow over the two allowed
      lights — memory: smoke_test_entities; real groups are never touched).
      Force one member 'unavailable', aggregate reads 'off', trip the rule.
      HARD: status is NOT 'error' and consecutive_errors stays 0.
      BEST-EFFORT: if the group call happened to raise and reclassify, assert
      target_outcomes[group].reason == 'member_unavailable' with the dead
      member's friendly-name in `members`; else SKIP (unit-locked).
  (b) REGRESSION HARD: a single-entity (non-group) verify rule is unaffected by
      the group code path — created and armed clean, status live not error.

Group helper teardown uses DELETE /api/config/config_entries/entry/<id>
(verified: plain /api/config/config_entries/<id> 404s; the /entry/ path works
and returns require_restart False for a light-group helper).

Run INSIDE the container:
  /home/vscode/.local/ha-venv/bin/python -I scripts/smoke_group_dead_member.py
Exit 0 = HARD assertions green (best-effort may SKIP), 1 = a HARD failure.
"""

from __future__ import annotations

import sys
import time

sys.path.insert(0, "/workspaces/home-assistant-core/config")
import smoke_follow as sf  # token/api/waits/cleanup/check/CREATED
import smoke_verify_enforce as sv  # status_sensor_for, force_state, single-entity TARGET

# Two allowed smoke lights (memory: smoke_test_entities). The disposable group
# is built over these; one is forced unavailable as the "dead member".
M_DEAD = "light.living_room_under_cabinet_lights"  # forced unavailable
M_LIVE = "light.living_room_diffuser"  # stays off (live member)
DOMAIN = "entity_guard"
LIVE = {"armed", "conditional", "enforcing", "pending", "cooldown", "verifying"}
POLL = 0.2


def make_light_group(token: str, name: str, members: list[str]) -> tuple[str, str]:
    """Create a disposable light-group helper over `members`.

    Returns (config_entry_id, group_entity_id). The group integration has no
    `group.set` service on serene_booth (returns 400), but the native group
    light-helper CONFIG-ENTRY flow works. Caller must tear down the entry.
    """
    flow = sf.api(
        token, "POST", "/api/config/config_entries/flow", {"handler": "group"}
    )
    fid = flow["flow_id"]
    sf.api(
        token,
        "POST",
        f"/api/config/config_entries/flow/{fid}",
        {"next_step_id": "light"},
    )
    result = sf.api(
        token,
        "POST",
        f"/api/config/config_entries/flow/{fid}",
        {"name": name, "entities": members, "hide_members": False, "all": False},
    )
    if result.get("type") != "create_entry":
        raise RuntimeError(f"group helper flow did not finish: {result}")
    entry_id = result["result"]["entry_id"]
    # Resolve the group's light entity_id by its member-list attribute matching.
    gid = None
    deadline = time.time() + 15
    while time.time() < deadline and gid is None:
        for st in sf.api(token, "GET", "/api/states"):
            eid = st["entity_id"]
            if eid.startswith("light.") and isinstance(
                st.get("attributes", {}).get("entity_id"), list
            ):
                if sorted(st["attributes"]["entity_id"]) == sorted(members):
                    gid = eid
                    break
        if gid is None:
            time.sleep(POLL)
    if gid is None:
        # Clean the orphan entry before bailing.
        sf.api(token, "DELETE", f"/api/config/config_entries/entry/{entry_id}")
        raise RuntimeError("group light entity never appeared after helper create")
    return entry_id, gid


def create_verify_rule_on(
    token: str, name: str, targets: list[str], trigger_states, target_state
) -> str:
    """Create a verify-ON fixed_state rule on arbitrary targets.

    sv.create_verify_rule hardcodes its single light target; this parametrizes
    the target list (so we can point at the group) and still sets
    verify_enabled=True in the extras step.
    """
    flow = sf.api(
        token, "POST", "/api/config/config_entries/flow", {"handler": "entity_guard"}
    )
    fid = flow["flow_id"]
    result = sf.api(
        token,
        "POST",
        f"/api/config/config_entries/flow/{fid}",
        {"rule_name": name, "target_entities": targets, "mode": "fixed_state"},
    )
    while result.get("type") == "form":
        sf.guard_form(result)
        step = result.get("step_id")
        if step == "state":
            payload = {
                "trigger_states": trigger_states,
                "target_state": target_state,
                "delay_seconds": 0,
            }
        elif step == "extras":
            payload = {"verify_enabled": True}
        else:
            payload = {}  # preview — defaults
        result = sf.api(
            token, "POST", f"/api/config/config_entries/flow/{fid}", payload
        )
    if result.get("type") != "create_entry":
        raise RuntimeError(f"verify group rule flow did not finish: {result}")
    entry_id = result["result"]["entry_id"]
    sf.CREATED.append(entry_id)
    return entry_id


def main() -> int:
    token = sf.mint_token()
    sf.cleanup(token)  # clear orphan EG rules from prior runs

    group_entry = None
    try:
        # === (a) GROUP + verify + dead member ==============================
        # Build the disposable group and drive both members OFF so the aggregate
        # reads 'off' (desired for an off-rule). Then force one member
        # unavailable — the aggregate stays 'off' (HA any-on) with a dead member.
        group_entry, gid = make_light_group(token, "ea_smoke_grp", [M_DEAD, M_LIVE])
        sf.check("disposable light group created", bool(gid), f"gid={gid}")

        # Precondition: members available before we start (else force_state and
        # the aggregate behave unpredictably and we'd false-FAIL blaming the fix).
        # Mirrors smoke_fast_verify's availability guard.
        # Seed both members 'off' via raw /api/states, THEN assert the live
        # member holds at off. M_LIVE may be a real device reporting
        # 'unavailable' at rest (e.g. a Tuya light offline between polls); what
        # the test needs is a live-off member so the any-on group aggregate
        # reads 'off', and force_state provides exactly that. Checking the
        # device's pre-seed state here was a false FAIL — it gated on the device
        # being awake rather than on the forced seed sticking.
        sv.force_state(token, M_DEAD, "off")
        sv.force_state(token, M_LIVE, "off")
        time.sleep(1.0)
        pre_live = sf.get_state(token, M_LIVE)
        pre_ok = pre_live and pre_live.get("state") == "off"
        sf.check(
            "live member seeds to 'off' before trip (else false FAIL unrelated to fix)",
            pre_ok,
            f"{M_LIVE} state={pre_live.get('state') if pre_live else None}",
        )
        if not pre_ok:
            return 1  # precondition miss, not a fix regression
        # Rule: trigger when group reads 'on', enforce 'off'. (target_state must
        # differ from trigger_states — target_in_triggers guard.)
        entry = create_verify_rule_on(token, "ea_smoke_grp_rule", [gid], ["on"], "off")
        sf.check("verify-ON group rule created", bool(entry))
        slug = "ea_smoke_grp_rule"
        status_eid = sv.status_sensor_for(token, slug)
        armed = sf.wait_for_armed(token, "fixed_state", [gid])
        sf.check("group rule left startup grace (armed)", armed)

        # Kill one member; aggregate must still read 'off' (dead member dragged
        # to unavailable, live member off -> any-on aggregate 'off' = desired).
        # Inject WITH a friendly_name (raw /api/states, not sv.force_state which
        # posts no attributes): the best-effort member_unavailable reclassify
        # branch below matches the dead member by friendly-name, so an attribute
        # strip would make that match silently fall back to the entity_id. Branch
        # is unit-locked, but keep the live probe honest if it ever fires.
        sf.api(
            token,
            "POST",
            f"/api/states/{M_DEAD}",
            {"state": "unavailable", "attributes": {"friendly_name": "Dead Member"}},
        )
        time.sleep(1.0)
        agg = sf.get_state(token, gid)
        sf.check(
            "group aggregate still reads 'off' with a dead member",
            agg and agg.get("state") == "off",
            f"agg={agg.get('state') if agg else None}",
        )

        # Trip: paint the group 'on' so the rule enforces 'off' on a group that
        # has a dead member. Post-fix this must NOT loop into a sticky error.
        sv.force_state(token, gid, "on")

        saw_reclassify = False
        went_error = False
        max_consec = 0
        deadline = time.time() + 20
        while time.time() < deadline:
            st = sf.get_state(token, status_eid)
            state = st.get("state") if st else None
            attrs = (st.get("attributes") or {}) if st else {}
            max_consec = max(max_consec, int(attrs.get("consecutive_errors") or 0))
            if state == "error":
                went_error = True
            outcomes = attrs.get("target_outcomes") or {}
            tgt = outcomes.get(gid, {})
            if tgt.get("reason") == "member_unavailable":
                saw_reclassify = True
            time.sleep(POLL)

        # HARD: the user-visible symptom — no sticky error loop on a group whose
        # aggregate is at desired with a dead member.
        sf.check(
            "group with dead member does NOT go sticky STATUS_ERROR",
            not went_error,
            "status hit 'error' (false-error loop — fix regression)",
        )
        sf.check(
            "group with dead member does NOT climb consecutive_errors",
            max_consec == 0,
            f"consecutive_errors peaked at {max_consec}",
        )

        # BEST-EFFORT: member_unavailable reclassify (only if the group call
        # actually raised — not forceable with demo lights, unit-locked).
        if saw_reclassify:
            tgt = (
                (sf.get_state(token, status_eid).get("attributes") or {})
                .get("target_outcomes", {})
                .get(gid, {})
            )
            dead_name = (
                sf.get_state(token, M_DEAD)
                .get("attributes", {})
                .get("friendly_name", M_DEAD)
            )
            members = tgt.get("members") or []
            sf.check(
                "reclassified skip outcome == 'skipped'",
                tgt.get("outcome") == "skipped",
            )
            sf.check(
                "dead member named in target_outcomes.members",
                any(dead_name in m for m in members),
                f"members={members}",
            )
        else:
            sf.check(
                "SKIP: member_unavailable reclassify not forced "
                "(demo-light group turn_off doesn't raise; branch unit-locked by "
                "test_group_dead_member_raise_is_skip_not_error)",
                True,
            )

        # === (b) REGRESSION: single-entity verify unaffected ===============
        # A non-group verify rule must arm clean — the group code path (dead-
        # member list is empty for a plain entity) must not alter it.
        sv.force_state(token, sv.TARGET, "off")  # reset the single-entity target
        single = sv.create_verify_rule(token, "ea_smoke_single_regress", verify=True)
        sf.check("single-entity verify rule created (regression)", bool(single))
        s_status = sv.status_sensor_for(token, "ea_smoke_single_regress")
        s_armed = sf.wait_for_armed(token, "fixed_state", [sv.TARGET])
        sf.check("single-entity rule armed (regression)", s_armed)
        sst = sf.get_state(token, s_status)
        sstate = sst.get("state") if sst else None
        sf.check(
            "single-entity rule lands on a live status, not error (regression)",
            sstate in LIVE,
            f"state={sstate}",
        )
    finally:
        sf.cleanup(token)  # EG rules
        if group_entry:
            # Reset the forced member + remove the disposable group helper.
            sv.force_state(token, M_DEAD, "off")
            try:
                sf.api(
                    token, "DELETE", f"/api/config/config_entries/entry/{group_entry}"
                )
            except Exception as err:
                print(f"WARN: group teardown failed: {err}")

    if sf.FAILS:
        print(f"\nFAILED: {sf.FAILS}")
        return 1
    print("\nALL GREEN")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
