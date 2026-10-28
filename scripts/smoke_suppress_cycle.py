"""Live check (serene_booth, REST) for the reachable slice of this branch.

What a complying-device container CAN show over REST:
  - suppress -> status 'suppressed', suppression_reason is a known card key
    ('manual') that bug 2's SUPPRESSION_REASON_LABELS humanizes;
  - unsuppress -> the rule leaves 'suppressed' and lands on a live status
    (armed/pending/cooldown/...), never stuck.

What this container CANNOT show (no device that fails a real service call; the
only genuinely-looping rule targets a FORBIDDEN real device): a true `failed`/
`unreachable_after_retries` target_outcome, hence the suppressed-recovery CLEAR
(bug 1) and the verify self-heal cycle. Those are locked by the unit suite at
100% coverage (test_suppressed_target_recovery_clears_outcome_keeps_suppression,
the self-heal/loop-protection tests) — not re-proven here.

Deterministic, zero real-device side effect: a fixed_state rule on the allowed
`input_boolean.ea_ctrl_kitchen_1`.

Run INSIDE the container:
  /home/vscode/.local/ha-venv/bin/python -I scripts/smoke_suppress_cycle.py
"""

import sys
import time

sys.path.insert(0, "/workspaces/home-assistant-core/config")
import smoke_follow as sf

TARGET = "input_boolean.ea_ctrl_kitchen_1"
DOMAIN = "entity_guard"
LIVE = {"armed", "conditional", "enforcing", "pending", "cooldown"}


def create_rule(token: str, name: str) -> str:
    flow = sf.api(token, "POST", "/api/config/config_entries/flow", {"handler": DOMAIN})
    fid = flow["flow_id"]
    result = sf.api(
        token,
        "POST",
        f"/api/config/config_entries/flow/{fid}",
        {"rule_name": name, "target_entities": [TARGET], "mode": "fixed_state"},
    )
    while result.get("type") == "form":
        sf.guard_form(result)
        step = result.get("step_id")
        if step == "state":
            payload = {
                "trigger_states": ["on"],
                "target_state": "off",
                "delay_seconds": 0,
            }
        elif step == "extras":
            payload = {"verify_enabled": False}
        else:
            payload = {}
        result = sf.api(
            token, "POST", f"/api/config/config_entries/flow/{fid}", payload
        )
    if result.get("type") != "create_entry":
        raise RuntimeError(f"create flow did not finish: {result}")
    entry_id = result["result"]["entry_id"]
    sf.CREATED.append(entry_id)
    return entry_id


def status_of(token: str, slug: str, timeout: int = 90) -> dict:
    want = f"sensor.{slug}_status"
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = sf.get_state(token, want)
        if st and st.get("state") not in (None, "unavailable", "unknown", "starting"):
            return st
        time.sleep(2)
    raise RuntimeError(f"status {want} never left startup/unknown")


def wait_state(token: str, slug: str, pred, timeout: int = 30) -> dict:
    want = f"sensor.{slug}_status"
    deadline = time.time() + timeout
    st = sf.get_state(token, want)
    while time.time() < deadline:
        st = sf.get_state(token, want)
        if st and pred(st["state"]):
            return st
        time.sleep(2)
    return st


def main() -> int:
    token = sf.mint_token()
    sf.cleanup(token)
    name = "Suppress Cycle Live"
    slug = "suppress_cycle_live"
    entry_id = create_rule(token, name)
    try:
        status_of(token, slug)

        # Suppress -> suppressed + known reason key (bug 2 card input).
        sf.api(
            token,
            "POST",
            f"/api/services/{DOMAIN}/suppress",
            {"rule_id": entry_id, "duration_minutes": 10},
        )
        st = wait_state(token, slug, lambda s: s == "suppressed")
        sf.check(
            "suppress -> status suppressed",
            st["state"] == "suppressed",
            f"state={st['state']}",
        )
        reason = st["attributes"].get("suppression_reason")
        sf.check(
            "suppression_reason is a known card key ('manual')",
            reason == "manual",
            f"reason={reason}",
        )

        # Unsuppress (clear) -> leaves suppressed, lands on a live status
        # (Thread B: no stuck-PENDING / stuck-SUPPRESSED after clear).
        sf.api(
            token,
            "POST",
            f"/api/services/{DOMAIN}/unsuppress",
            {"rule_id": entry_id},
        )
        st = wait_state(token, slug, lambda s: s != "suppressed")
        sf.check(
            "unsuppress -> leaves suppressed",
            st["state"] != "suppressed",
            f"state={st['state']}",
        )
        sf.check(
            "unsuppress -> lands on a live status (not stuck)",
            st["state"] in LIVE,
            f"state={st['state']}",
        )
    finally:
        sf.cleanup(token)

    ok = not sf.FAILS
    if sf.FAILS:
        print(f"FAILED: {', '.join(sf.FAILS)}")
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
