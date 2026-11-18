#!/usr/bin/env python3
"""Entity Guard FOLLOW-MODE MULTI-STATE TARGET smoke — run INSIDE serene_booth.

Regression guard for the Finding-1 fix (DOMAIN_ON_STATES / target_state_is_on):
a dynamic_state (follow) rule whose TARGET is a multi-state domain (climate) that
reports a running MODE (`heat`), not literal `on`, must read as REACHED once driven
on — NOT loop the verify self-heal into a spurious `unreachable_loop` suppression.

Pre-fix: the reached-check used a flat FOLLOW_ON_STATES set with no `heat`, so a
correctly-heating climate read not-reached forever → 5 exhausted cycles →
auto-suppressed with a false "target offline" notice.
Post-fix: `heat` is a recognized on-state for climate → reached → rule stays live.

Fixtures (seeded in this container):
  SOURCE = input_boolean.eg_smoke_heater   (driven via input_boolean.turn_on/off)
  TARGET = climate.eg_smoke_thermostat     (hvac_modes [heat, off]; actuatable)

The rule mirrors source→target with VERIFY ENABLED (default on for state modes),
so the self-heal/kill-switch path is actually armed — if the reached-check still
mis-read `heat`, this smoke would observe the rule go `suppressed` and FAIL.

Usage: python3 smoke_followon_multistate.py
Exit 0 = all green, 1 = a failure.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

BASE = "http://localhost:8123"
AUTH_STORE = "/workspaces/home-assistant-core/config/.storage/auth"

SOURCE = "input_boolean.eg_smoke_heater"
TARGET = "climate.eg_smoke_thermostat"
# Unique per-run suffix so an orphan from a prior killed run can't collide on name.
RUN = str(int(time.time()))


def mint_token() -> str:
    import jwt  # PyJWT, present in the HA venv

    data = json.load(open(AUTH_STORE))["data"]
    rt = next(t for t in data["refresh_tokens"] if t.get("token_type") == "normal")
    now = int(time.time())
    return jwt.encode(
        {"iss": rt["id"], "iat": now, "exp": now + 1800},
        rt["jwt_key"],
        algorithm="HS256",
    )


def api(token: str, method: str, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(
        f"{BASE}{path}",
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read().decode()
        return json.loads(raw) if raw else {}


def get_state(token: str, entity_id: str) -> dict:
    try:
        return api(token, "GET", f"/api/states/{entity_id}")
    except urllib.error.HTTPError:
        return {}


def drive_source(token: str, state: str) -> None:
    api(
        token,
        "POST",
        f"/api/services/input_boolean/turn_{state}",
        {"entity_id": SOURCE},
    )


def set_climate(token: str, mode: str) -> None:
    """Put the climate target into a known hvac_mode via its real service."""
    api(
        token,
        "POST",
        "/api/services/climate/set_hvac_mode",
        {"entity_id": TARGET, "hvac_mode": mode},
    )


def wait_for_climate(token: str, want: str, timeout: int = 20) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if get_state(token, TARGET).get("state") == want:
            return True
        time.sleep(1)
    return False


FAILS: list[str] = []
CREATED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


def guard_form(result: dict) -> None:
    errors = result.get("errors")
    if errors:
        raise RuntimeError(
            f"flow step '{result.get('step_id')}' returned errors: {errors}"
        )


def cleanup(token: str) -> None:
    for entry_id in CREATED:
        try:
            api(token, "DELETE", f"/api/config/config_entries/entry/{entry_id}")
            print(f"  cleaned up entry {entry_id}")
        except Exception as e:
            print(f"  [WARN] cleanup failed for {entry_id}: {e}")


def rule_status(token: str, targets: list[str]) -> str | None:
    """Return the status-sensor state for THIS run's dynamic_state rule (matched by
    mode + unique target set), or None if not found yet."""
    want = sorted(targets)
    for s in api(token, "GET", "/api/states"):
        eid = s.get("entity_id", "")
        a = s.get("attributes", {})
        if (
            eid.startswith("sensor.")
            and eid.endswith("_status")
            and a.get("mode") == "dynamic_state"
            and sorted(a.get("target_entities") or []) == want
        ):
            return s.get("state")
    return None


def wait_for_status(
    token: str, targets: list[str], live: set[str], timeout: int = 120
) -> str | None:
    """Wait until the rule leaves startup grace and reaches a LIVE status; returns
    that status, or the last-seen status on timeout. The engine ignores target
    events during ~60s startup grace, so poll past it."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = rule_status(token, targets)
        if last in live:
            return last
        time.sleep(3)
    return last


def create_dynamic_state_verify_rule(
    token: str, name: str, source: str, targets: list[str]
) -> str:
    """Create a dynamic_state (Follow) rule with verify ENABLED. Returns entry_id."""
    flow = api(
        token, "POST", "/api/config/config_entries/flow", {"handler": "entity_guard"}
    )
    flow_id = flow["flow_id"]
    result = api(
        token,
        "POST",
        f"/api/config/config_entries/flow/{flow_id}",
        {"rule_name": name, "target_entities": targets, "mode": "dynamic_state"},
    )
    while result.get("type") == "form":
        guard_form(result)
        step = result.get("step_id", "")
        if step == "dynamic_state_params":
            payload = {"source_entity": source, "follow_invert": False}
        elif step == "safety":
            # climate is a safety-relevant target domain → the flow requires an
            # explicit acknowledgement before it will create the rule.
            payload = {"safety_acknowledged": True}
        elif step == "extras":
            # Verify ON (default for state modes, set explicitly); no flags/debounce.
            # Keys are the CONF_* string values, not the UI labels.
            payload = {
                "add_flags": False,
                "custom_rate_limit": False,
                "add_debounce": False,
                "verify_enabled": True,
            }
        else:
            payload = {}  # preview / unknown — accept defaults
        result = api(
            token, "POST", f"/api/config/config_entries/flow/{flow_id}", payload
        )
    if result.get("type") != "create_entry":
        raise RuntimeError(f"dynamic_state create flow did not finish: {result}")
    entry_id = result["result"]["entry_id"]
    CREATED.append(entry_id)
    return entry_id


def main() -> int:
    token = mint_token()
    print(f"Multi-state follow-target smoke (run {RUN})")

    # Preconditions: fixtures present + actuatable.
    tgt = get_state(token, TARGET)
    src = get_state(token, SOURCE)
    if tgt.get("state") in (None, "unavailable", "unknown"):
        print(f"  [SKIP] target {TARGET} not available — fixture missing")
        return 0
    if "heat" not in (tgt.get("attributes", {}).get("hvac_modes") or []):
        print(f"  [SKIP] target {TARGET} has no 'heat' hvac_mode — fixture mismatch")
        return 0
    if not src:
        print(f"  [SKIP] source {SOURCE} not available — fixture missing")
        return 0

    try:
        # Known start: source off, climate off.
        drive_source(token, "off")
        set_climate(token, "off")
        time.sleep(1)

        name = f"EG Smoke MultiState {RUN}"
        entry_id = create_dynamic_state_verify_rule(token, name, SOURCE, [TARGET])
        print(f"  created rule {entry_id}")

        live = {"armed", "conditional", "enforcing", "cooldown", "pending", "verifying"}
        status = wait_for_status(token, [TARGET], live, timeout=120)
        check(
            "rule reaches a live status after startup grace",
            status in live,
            f"status={status}",
        )

        # Drive source ON → mirror should turn the climate on. It settles to 'heat'.
        drive_source(token, "on")
        reached = wait_for_climate(token, "heat", timeout=25)
        check(
            "climate target driven to 'heat' by the follow rule",
            reached,
            f"climate={get_state(token, TARGET).get('state')}",
        )

        # THE FIX: with the climate correctly at 'heat' and verify on, the reached-
        # check must read it as reached → the rule holds a LIVE status and does NOT
        # loop to 'suppressed' (unreachable_loop) nor 'error'. Give the verify/
        # self-heal path ample time to (wrongly, pre-fix) exhaust 5 cycles.
        time.sleep(35)
        final = rule_status(token, [TARGET])
        check(
            "rule NOT auto-suppressed on a correctly-running climate target",
            final not in ("suppressed", "error"),
            f"status={final}",
        )
        check(
            "rule still in a live status (no spurious unreachable_loop)",
            final in live,
            f"status={final}",
        )

        # And the climate is still 'heat' (not flapped off by a bogus corrective).
        check(
            "climate target stable at 'heat' (no corrective thrash)",
            get_state(token, TARGET).get("state") == "heat",
        )
    finally:
        # Leave fixtures as found; delete the rule this run created.
        try:
            drive_source(token, "off")
        except Exception:
            pass
        cleanup(token)

    if FAILS:
        print(f"\nFAILED: {len(FAILS)} check(s): {', '.join(FAILS)}")
        return 1
    print("\nALL GREEN")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
