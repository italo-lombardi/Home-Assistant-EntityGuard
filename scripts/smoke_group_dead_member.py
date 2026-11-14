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

WHAT IS LIVE-FORCEABLE (deterministic via fixtures):

  Both legs are HARD and deterministic. Two fixtures (scripts/eg_smoke_fixtures
  .yaml): switch.eg_smoke_unavailable (availability template false) is
  PERMANENTLY unavailable — no /api/states paint race — and switch.eg_smoke_raiser
  raises on turn_off. The reclassify is forced WITHOUT any state-paint timing race
  by pressing the rule's test_enforce button: a manual force issues turn_off on a
  group that ALREADY reads the desired 'off' (so _verify_reached is True) while
  the raiser makes the fan-out turn_off RAISE — exactly the condition
  _record_failure reclassifies to member_unavailable. The reclassify is also
  unit-locked by test_group_dead_member_raise_is_skip_not_error.

SCENARIOS:
  (a) GROUP + verify-ON off-rule over a DISPOSABLE switch group (built live via
      the native `group` switch-helper config-entry flow over the eg_smoke_*
      fixtures; real groups/devices are never touched). The group rests at 'off'
      (== desired); force-enforce via the test_enforce button.
      HARD: status is NOT 'error', consecutive_errors stays 0, AND
      target_outcomes[group].reason == 'member_unavailable' (reclassify, skip
      outcome, dead member named).
  (b) REGRESSION HARD: a single-entity (non-group) verify rule is unaffected by
      the group code path — created and armed clean, status live not error.

Group helper teardown uses DELETE /api/config/config_entries/entry/<id>
(verified: plain /api/config/config_entries/<id> 404s; the /entry/ path works
and returns require_restart False for a group helper).

Run INSIDE the container:
  /home/vscode/.local/ha-venv/bin/python -I scripts/smoke_group_dead_member.py
Exit 0 = all HARD assertions green, 1 = a failure.
"""

from __future__ import annotations

import sys
import time

sys.path.insert(0, "/workspaces/home-assistant-core/config")
import smoke_follow as sf  # token/api/waits/cleanup/check/CREATED
import smoke_verify_enforce as sv  # status_sensor_for, force_state, single-entity TARGET

# Switch group over deterministic fixtures (scripts/eg_smoke_fixtures.yaml):
#  - eg_smoke_unavailable: availability=false -> genuinely 'unavailable' (dead
#    member; no /api/states paint race).
#  - eg_smoke_raiser: turn_off calls a nonexistent service -> the group turn_off
#    RAISES, driving the member_unavailable reclassify path.
# EG group handling is domain-agnostic (_dead_group_members keys only on a
# list-valued `entity_id` attr), so a switch group behaves like a light group.
M_DEAD = "switch.eg_smoke_unavailable"  # genuinely unavailable
M_RAISER = "switch.eg_smoke_raiser"  # raises on turn_off
LIVE = {"armed", "conditional", "enforcing", "pending", "cooldown", "verifying"}
POLL = 0.2


def make_switch_group(token: str, name: str, members: list[str]) -> tuple[str, str]:
    """Create a disposable switch-group helper over `members`.

    Returns (config_entry_id, group_entity_id). Uses the native group
    config-entry flow (switch sub-flow). Caller must tear down the entry.
    """
    flow = sf.api(
        token, "POST", "/api/config/config_entries/flow", {"handler": "group"}
    )
    fid = flow["flow_id"]
    sf.api(
        token,
        "POST",
        f"/api/config/config_entries/flow/{fid}",
        {"next_step_id": "switch"},
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
    # Resolve the group's switch entity_id by its member-list attribute matching.
    gid = None
    deadline = time.time() + 15
    while time.time() < deadline and gid is None:
        for st in sf.api(token, "GET", "/api/states"):
            eid = st["entity_id"]
            if eid.startswith("switch.") and isinstance(
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
        raise RuntimeError("group switch entity never appeared after helper create")
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
        # Build the disposable switch group over fixtures. eg_smoke_unavailable
        # is PERMANENTLY unavailable (availability=false); eg_smoke_raiser raises
        # on turn_off. Both rest at 'off', so the any-on aggregate reads 'off'
        # (== desired for an off-rule) with a genuinely dead member present.
        group_entry, gid = make_switch_group(token, "ea_smoke_grp", [M_DEAD, M_RAISER])
        sf.check("disposable switch group created", bool(gid), f"gid={gid}")

        time.sleep(1.0)
        pre_dead = sf.get_state(token, M_DEAD)
        sf.check(
            "dead-member fixture is genuinely unavailable",
            pre_dead and pre_dead.get("state") == "unavailable",
            f"{M_DEAD} state={pre_dead.get('state') if pre_dead else None}",
        )

        # Rule: trigger when group reads 'on', enforce 'off'. The group rests at
        # 'off' (== the DESIRED state), so a manual force-enforce (test_enforce
        # button) issues turn_off on an ALREADY-at-desired group — exactly the
        # real scenario: _verify_reached(group)=True while a dead member makes
        # the fan-out turn_off RAISE. That drives _record_failure's reclassify to
        # member_unavailable deterministically (no state-paint timing race —
        # force-enforce bypasses the trigger check).
        entry = create_verify_rule_on(token, "ea_smoke_grp_rule", [gid], ["on"], "off")
        sf.check("verify-ON group rule created", bool(entry))
        slug = "ea_smoke_grp_rule"
        status_eid = sv.status_sensor_for(token, slug)
        armed = sf.wait_for_armed(token, "fixed_state", [gid])
        sf.check("group rule left startup grace (armed)", armed)

        agg = sf.get_state(token, gid)
        sf.check(
            "group aggregate reads 'off' (desired) with a dead member",
            agg and agg.get("state") == "off",
            f"agg={agg.get('state') if agg else None}",
        )

        # Force-enforce via the test_enforce button: issues turn_off on the
        # at-desired group → the raiser member makes it RAISE → reclassify.
        sf.api(
            token,
            "POST",
            "/api/services/button/press",
            {"entity_id": f"button.{slug}_test_enforce"},
        )

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

        # HARD: member_unavailable reclassify. Forced deterministically by the
        # test_enforce button on an at-desired group with a raise-on-turn_off
        # member — _verify_reached(group)=True + the fan-out raises → reclassify.
        sf.check("member_unavailable reclassify observed", saw_reclassify)
        tgt = (
            (sf.get_state(token, status_eid).get("attributes") or {})
            .get("target_outcomes", {})
            .get(gid, {})
        )
        members = tgt.get("members") or []
        sf.check(
            "reclassified skip outcome == 'skipped'",
            tgt.get("outcome") == "skipped",
            f"outcome={tgt.get('outcome')}",
        )
        sf.check(
            "dead member named in target_outcomes.members",
            bool(members),
            f"members={members}",
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
            # Remove the disposable group helper. M_DEAD is a permanent template
            # fixture (availability=false) — never paint it; a raw /api/states
            # write would override its template state until the next HA re-eval.
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
