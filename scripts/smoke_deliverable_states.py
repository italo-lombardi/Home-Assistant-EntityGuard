#!/usr/bin/env python3
"""Entity Guard FEATURE C — DELIVERABLE NON-ON/OFF STATES smoke test.

Run INSIDE the serene_booth container. Proves end-to-end that the engine now
DELIVERS target states beyond on/off (the headline Feature-C correctness win),
against a live HA via the REST + WebSocket APIs.

Primary proof (deterministic, device-safe — a software helper, no physical device):
  1. Create a DISPOSABLE input_select helper (eg_smoke_<token>) over the WS
     collection API, options ["idle","active","paused"]. input_select has no
     turn_on/off service, so a rule driving it to "active" can ONLY succeed via
     input_select.select_option(option="active") — exactly Feature C's new
     DOMAIN_MODE_SERVICE_MAP path. On OLD code this state is undeliverable
     (no_service_mapping, silent skip) and the assertion fails — so a pass proves
     the new wiring is live.
  2. fixed_state rule: target_state="active", trigger_states=["idle"]. Drive the
     helper to "idle" → assert it is driven to "active" (select_option fired).
  3. Second value: retarget to "paused" via a second rule → assert driven to
     "paused" (proves the kwarg value is the runtime target_state, not a constant).

Secondary proof (live resolver, no actuatable device needed): import the deployed
rule_engine and assert _resolve_state_service returns the right (service, data)
for every new (domain, state) — climate/vacuum/water_heater/valve/cover-stop/
lawn_mower — plus the on/off-fallback + input_select regression-fix (on/off on a
mode domain must NOT become set_hvac_mode(hvac_mode="on")). This exercises the
map wiring live even where no safe actuatable entity exists.

Honest scope / skips:
  - climate live-actuate SKIPPED: the only climate entity in the container is the
    user's real thermostat — must not actuate. Covered by the resolver assertion
    instead.
  - RESTRICT (pruned picker states) NOT smoke-tested: the state picker is a
    config-flow schema, not cleanly REST-observable. Locked by unit tests
    (tests/test_config_flow_helpers.py::test_states_for_entities_*).

Usage: python3 smoke_deliverable_states.py
Exit 0 = all green, 1 = a failure. Cleans up its helper + rules on any exit.

Mirrors scripts/smoke_follow.py helpers (token mint, REST api, fixed_state flow).
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.request

BASE = "http://localhost:8123"
WS = "ws://localhost:8123/api/websocket"
AUTH_STORE = "/workspaces/home-assistant-core/config/.storage/auth"

# Unique per-run suffix so repeated runs never collide on a leftover helper/rule.
RUN = str(int(time.time()))
HELPER_SLUG = f"eg_smoke_{RUN}"
HELPER_ENTITY = f"input_select.{HELPER_SLUG}"


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


def select_option(token: str, entity_id: str, option: str) -> None:
    """Drive the input_select to an option via its real service (a genuine state
    change the engine observes)."""
    api(
        token,
        "POST",
        "/api/services/input_select/select_option",
        {"entity_id": entity_id, "option": option},
    )


def wait_for_state(token: str, entity_id: str, want: str, timeout: int = 20) -> bool:
    """Poll until entity_id reaches `want` (or timeout). Engine eval is async, so a
    fixed sleep races it — poll."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if get_state(token, entity_id).get("state") == want:
            return True
        time.sleep(1)
    return False


FAILS: list[str] = []
CREATED: list[str] = []  # rule entry_ids created this run
HELPER_CREATED = False  # whether the WS input_select helper exists (cleanup gate)


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


def create_fixed_state_rule(
    token: str,
    name: str,
    targets: list[str],
    target_state: str,
    trigger_states: list[str],
) -> str:
    """Create a fixed_state rule. Returns entry_id.

    Flow: rule(mode=fixed_state) -> state(trigger/target/delay) -> extras(skip)
    -> preview -> create. Mirrors smoke_follow.create_fixed_state_rule.
    """
    flow = api(
        token, "POST", "/api/config/config_entries/flow", {"handler": "entity_guard"}
    )
    flow_id = flow["flow_id"]
    result = api(
        token,
        "POST",
        f"/api/config/config_entries/flow/{flow_id}",
        {"rule_name": name, "target_entities": targets, "mode": "fixed_state"},
    )
    while result.get("type") == "form":
        guard_form(result)
        if result.get("step_id") == "state":
            payload = {
                "trigger_states": trigger_states,
                "target_state": target_state,
                "delay_seconds": 0,
            }
        else:
            payload = {}  # extras / preview — accept defaults
        result = api(
            token, "POST", f"/api/config/config_entries/flow/{flow_id}", payload
        )
    if result.get("type") != "create_entry":
        raise RuntimeError(f"fixed_state create flow did not finish: {result}")
    entry_id = result["result"]["entry_id"]
    CREATED.append(entry_id)
    return entry_id


def wait_for_armed(
    token: str, mode: str, targets: list[str], timeout: int = 100
) -> bool:
    """Wait until THIS run's rule (matched by mode AND its unique target set) leaves
    startup grace. Same rationale as smoke_follow.wait_for_armed: the engine ignores
    target events during the ~60s grace, so acting earlier is a false FAIL; a
    mode-only match can hit a pre-existing disabled rule."""
    live = {"armed", "conditional", "enforcing", "cooldown", "pending"}
    want = sorted(targets)
    deadline = time.time() + timeout
    while time.time() < deadline:
        for s in api(token, "GET", "/api/states"):
            eid = s.get("entity_id", "")
            a = s.get("attributes", {})
            if (
                eid.startswith("sensor.")
                and eid.endswith("_status")
                and a.get("mode") == mode
                and sorted(a.get("target_entities") or []) == want
                and s.get("state") in live
            ):
                return True
        time.sleep(3)
    return False


# --------------------------------------------------------------------------- WS
async def _ws_cmd_session(token: str, commands):
    """Open one authenticated WS session, run a list of command dicts in order,
    return the list of result messages. Keeps websockets usage in one place."""
    import websockets

    results = []
    async with websockets.connect(WS, max_size=None) as ws:
        await ws.recv()  # auth_required
        await ws.send(json.dumps({"type": "auth", "access_token": token}))
        authres = json.loads(await ws.recv())
        if authres.get("type") != "auth_ok":
            raise RuntimeError(f"WS auth failed: {authres}")
        mid = 0
        for payload in commands:
            mid += 1
            payload = {**payload, "id": mid}
            await ws.send(json.dumps(payload))
            while True:
                m = json.loads(await ws.recv())
                if m.get("id") == mid and m.get("type") == "result":
                    results.append(m)
                    break
    return results


def create_helper(token: str) -> None:
    """Create the disposable input_select helper over the WS collection API."""
    global HELPER_CREATED
    res = asyncio.run(
        _ws_cmd_session(
            token,
            [
                {
                    "type": "input_select/create",
                    "name": HELPER_SLUG,
                    "options": ["idle", "active", "paused"],
                }
            ],
        )
    )
    if not res or not res[0].get("success"):
        raise RuntimeError(f"input_select/create failed: {res}")
    HELPER_CREATED = True


def delete_helper(token: str) -> None:
    """Delete the disposable helper (best-effort — never masks the smoke result)."""
    if not HELPER_CREATED:
        return
    try:
        asyncio.run(
            _ws_cmd_session(
                token,
                [{"type": "input_select/delete", "input_select_id": HELPER_SLUG}],
            )
        )
        print(f"  cleaned up helper {HELPER_ENTITY}")
    except Exception as e:
        print(f"  [WARN] helper cleanup failed: {e}")


def cleanup(token: str) -> None:
    """Delete every rule entry this run created, then the helper. Best-effort."""
    for entry_id in CREATED:
        try:
            api(token, "DELETE", f"/api/config/config_entries/entry/{entry_id}")
            print(f"  cleaned up entry {entry_id}")
        except Exception as e:
            print(f"  [WARN] cleanup failed for {entry_id}: {e}")
    CREATED.clear()
    delete_helper(token)


# ------------------------------------------------------------------- resolver
def resolver_assertions() -> None:
    """Import the DEPLOYED engine and assert _resolve_state_service maps every new
    (domain, state) to the right (service, data). Device-safe, deterministic —
    proves the map wiring live where no safe actuatable entity exists (climate,
    vacuum, water_heater, valve, lawn_mower, cover-stop), plus the on/off-fallback
    + input_select regression fix."""
    import sys

    sys.path.insert(0, "/workspaces/home-assistant-core/config/custom_components")
    from entity_guard.rule_engine import RuleEngine

    # _resolve_state_service is a pure method (no hass/state reads) — call it on a
    # bare instance via __new__, no engine setup needed.
    eng = RuleEngine.__new__(RuleEngine)

    def resolve(entity_id: str, state: str):
        domain = entity_id.split(".", 1)[0]
        return eng._resolve_state_service(entity_id, domain, state)

    cases = [
        # (entity, state, expected (svc_domain, svc, data))
        (
            "climate.x",
            "heat",
            (
                "climate",
                "set_hvac_mode",
                {"entity_id": "climate.x", "hvac_mode": "heat"},
            ),
        ),
        (
            "climate.x",
            "off",  # parameterless wins over mode map
            ("climate", "turn_off", {"entity_id": "climate.x"}),
        ),
        (
            "climate.x",
            "on",  # on/off fallback, NOT set_hvac_mode(hvac_mode="on")
            ("homeassistant", "turn_on", {"entity_id": "climate.x"}),
        ),
        (
            "water_heater.x",
            "eco",
            (
                "water_heater",
                "set_operation_mode",
                {"entity_id": "water_heater.x", "operation_mode": "eco"},
            ),
        ),
        (
            "input_select.x",
            "Movie",
            (
                "input_select",
                "select_option",
                {"entity_id": "input_select.x", "option": "Movie"},
            ),
        ),
        (
            "input_select.x",
            "on",  # regression fix: literal "on" option -> select_option
            (
                "input_select",
                "select_option",
                {"entity_id": "input_select.x", "option": "on"},
            ),
        ),
        ("vacuum.x", "cleaning", ("vacuum", "start", {"entity_id": "vacuum.x"})),
        ("vacuum.x", "docked", ("vacuum", "return_to_base", {"entity_id": "vacuum.x"})),
        ("valve.x", "open", ("valve", "open_valve", {"entity_id": "valve.x"})),
        ("cover.x", "stopped", ("cover", "stop_cover", {"entity_id": "cover.x"})),
        (
            "lawn_mower.x",
            "mowing",
            ("lawn_mower", "start_mowing", {"entity_id": "lawn_mower.x"}),
        ),
        ("sensor.x", "cooking", None),  # genuinely unmapped -> None
    ]
    for entity_id, state, expected in cases:
        got = resolve(entity_id, state)
        check(
            f"resolver: {entity_id.split('.')[0]} '{state}' -> "
            f"{expected[1] if expected else 'None'}",
            got == expected,
            f"got={got}",
        )

    # The input_select regression case is the one C's reorder nearly broke — assert
    # it loudly and separately so a future reorder can't silently regress it again.
    check(
        "regression: input_select literal 'off' option routes to select_option",
        resolve("input_select.x", "off")
        == (
            "input_select",
            "select_option",
            {"entity_id": "input_select.x", "option": "off"},
        ),
    )


# ------------------------------------------------------------------------ main
def main() -> int:
    token = mint_token()

    # Guard: is Feature C actually deployed? If not, every new-state assertion
    # would fail for a deploy reason, not a feature bug — say so clearly.
    try:
        import sys

        sys.path.insert(0, "/workspaces/home-assistant-core/config/custom_components")
        from entity_guard import const as _const

        if not hasattr(_const, "DOMAIN_MODE_SERVICE_MAP"):
            print(
                "  [FAIL] deployed code has no DOMAIN_MODE_SERVICE_MAP — "
                "Feature C not deployed. Deploy the integration branch first."
            )
            return 1
    except Exception as e:
        print(f"  [FAIL] could not import deployed entity_guard.const: {e}")
        return 1

    # --- Secondary proof first: live resolver (no HA round-trip needed) --------
    print("Resolver assertions (deployed engine)…")
    resolver_assertions()

    # --- Primary proof: end-to-end input_select deliver via select_option ------
    print(f"Creating disposable helper {HELPER_ENTITY}…")
    try:
        create_helper(token)
    except Exception as e:
        print(f"  [FAIL] create input_select helper — {e}")
        return 1
    # Seed to a known trigger state.
    time.sleep(2)
    select_option(token, HELPER_ENTITY, "idle")

    print("Creating fixed_state rule target_state=active…")
    try:
        create_fixed_state_rule(
            token,
            f"EG Smoke Deliverable Active {RUN}",
            [HELPER_ENTITY],
            target_state="active",
            trigger_states=["idle"],
        )
        armed = wait_for_armed(token, "fixed_state", [HELPER_ENTITY])
        check("fixed_state(active): rule armed past startup grace", armed)
        # Violate: drive the helper to the trigger 'idle' — the rule must drive it
        # to 'active' via select_option (undeliverable on old code).
        select_option(token, HELPER_ENTITY, "idle")
        check(
            "deliver: input_select driven idle -> active (select_option fired)",
            wait_for_state(token, HELPER_ENTITY, "active"),
            f"state={get_state(token, HELPER_ENTITY).get('state')}",
        )
    except Exception as e:
        check("fixed_state(active): create+enforce", False, str(e))

    # Drop the first rule so it doesn't fight the second over the same target.
    cleanup_rules_only(token)

    # --- Second value: prove the kwarg value is the runtime state, not constant --
    print("Creating fixed_state rule target_state=paused…")
    try:
        # Seed to 'active' so 'paused' is a real change; trigger on 'active'.
        select_option(token, HELPER_ENTITY, "active")
        create_fixed_state_rule(
            token,
            f"EG Smoke Deliverable Paused {RUN}",
            [HELPER_ENTITY],
            target_state="paused",
            trigger_states=["active"],
        )
        armed = wait_for_armed(token, "fixed_state", [HELPER_ENTITY])
        check("fixed_state(paused): rule armed past startup grace", armed)
        select_option(token, HELPER_ENTITY, "active")
        check(
            "deliver: input_select driven active -> paused (distinct option value)",
            wait_for_state(token, HELPER_ENTITY, "paused"),
            f"state={get_state(token, HELPER_ENTITY).get('state')}",
        )
    except Exception as e:
        check("fixed_state(paused): create+enforce", False, str(e))

    # --- Honest skips (printed, not asserted) ---------------------------------
    print(
        "  [SKIP] climate live-actuate — only climate entity is the user's real "
        "thermostat; covered by the resolver assertion instead."
    )
    print(
        "  [SKIP] RESTRICT picker pruning — config-flow schema, not cleanly "
        "REST-observable; locked by unit tests (test_states_for_entities_*)."
    )

    print()
    if FAILS:
        print(f"SMOKE RESULT: {len(FAILS)} FAILED — {', '.join(FAILS)}")
        return 1
    print("SMOKE RESULT: ALL GREEN")
    return 0


def cleanup_rules_only(token: str) -> None:
    """Delete created rules but keep the helper (reused by the next sub-test)."""
    for entry_id in list(CREATED):
        try:
            api(token, "DELETE", f"/api/config/config_entries/entry/{entry_id}")
            print(f"  cleaned up entry {entry_id}")
        except Exception as e:
            print(f"  [WARN] cleanup failed for {entry_id}: {e}")
    CREATED.clear()


if __name__ == "__main__":
    import sys

    try:
        rc = main()
    finally:
        # Last-ditch: delete any tracked rule + the helper so a crashed run never
        # leaves smoke artifacts behind (memory rule: smoke cleans up its own).
        try:
            cleanup(mint_token())
        except Exception as e:
            print(f"  [WARN] final cleanup failed: {e}")
    sys.exit(rc)
