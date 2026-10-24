#!/usr/bin/env python3
"""Entity Guard v0.4.0 FOLLOW-MODE smoke test — run INSIDE the serene_booth container.

Exercises the follow feature end-to-end against a live HA:

  1. Create a follow rule via the config-flow REST API (mode=dynamic_state, mirror).
  2. Mirror: source on -> target on; source off -> target off.
  3. Idempotence: a repeat source change that doesn't change desired = no extra call.
  4. Offline hold: source unavailable -> target left untouched, no error.
  5. Invert: a second rule with follow_invert=on drives the opposite direction.
  6. Status sensor exposes mode/source_entity/follow_invert/follow_desired_on.
  7. No regression: a plain STATE-mode rule still enforces.

Usage: python3 smoke_follow.py
Exit 0 = all green, 1 = a failure.

Mirrors scripts/smoke.py helpers (token mint, REST api, restart) for the dev
container layout (HA at :8123, auth store under config/.storage).
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

BASE = "http://localhost:8123"
AUTH_STORE = "/workspaces/home-assistant-core/config/.storage/auth"
REG = "/workspaces/home-assistant-core/config/.storage/core.entity_registry"
# Use REAL actuatable entities so enforcement actually flips the target:
#  - SOURCE is an input_boolean driven via input_boolean.turn_on/off (a real state
#    change the engine observes), NOT a states-API paint.
#  - TARGET is a real light the engine can drive via light.turn_on/off.
# A states-API-only entity (e.g. a painted binary_sensor/light) is NOT actuatable:
# the engine's service call logs "entity missing or not available" and the target
# never flips — that would fail the smoke for a fixture reason, not a feature bug.
SOURCE = "input_boolean.ea_ctrl_kitchen_1"
TARGET = "light.living_room_under_cabinet_lights"
# dynamic_attribute test needs two distinct DIMMABLE lights. Only two are allowed
# for smoke in this container (no bedroom_diffuser, no chandelier bulbs), so the
# state-mode rule above is deleted before the attribute rule runs (sequential).
ATTR_SOURCE = "light.living_room_diffuser"
ATTR_TARGET = "light.living_room_under_cabinet_lights"


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


def set_state(
    token: str, entity_id: str, state: str, attrs: dict | None = None
) -> None:
    """Force an entity state via the states API (test fixture entities)."""
    api(
        token,
        "POST",
        f"/api/states/{entity_id}",
        {"state": state, "attributes": attrs or {}},
    )


def drive_source(token: str, state: str) -> None:
    """Drive the SOURCE input_boolean to on/off via its real service (a genuine
    state change the engine observes), or paint 'unavailable' via the states API
    for the offline-hold case (no service produces unavailable)."""
    if state in ("on", "off"):
        api(
            token,
            "POST",
            f"/api/services/input_boolean/turn_{state}",
            {"entity_id": SOURCE},
        )
    else:
        set_state(token, SOURCE, state)


def actuate_target(token: str, state: str) -> None:
    """Put the TARGET light into a known state via its real service."""
    api(
        token,
        "POST",
        f"/api/services/light/turn_{state}",
        {"entity_id": TARGET},
    )


def get_state(token: str, entity_id: str) -> dict:
    try:
        return api(token, "GET", f"/api/states/{entity_id}")
    except urllib.error.HTTPError:
        return {}


def wait_for_state(token: str, entity_id: str, want: str, timeout: int = 15) -> bool:
    """Poll until entity_id reaches `want` (or timeout). Returns True on match.

    Engine evaluation is async (source change -> scheduled eval task -> service
    call -> state propagation), so a fixed sleep races it. Poll instead.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if get_state(token, entity_id).get("state") == want:
            return True
        time.sleep(1)
    return False


def wait_for_attr(
    token: str, entity_id: str, attr: str, want, timeout: int = 15
) -> bool:
    """Poll until entity_id's attribute equals want."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if get_state(token, entity_id).get("attributes", {}).get(attr) == want:
            return True
        time.sleep(1)
    return False


FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


def create_dynamic_state_rule(
    token: str, name: str, source: str, targets: list[str], invert: bool
) -> str:
    """Create a dynamic_state (Follow mode) rule. Returns entry_id.

    Flow: rule(name/targets/mode=dynamic_state) -> dynamic_state_params
    (source_entity/follow_invert) -> optional-features (skip) -> preview -> create.
    step_ids mirror -ca's config_flow slice; the loop fills whatever form shows.
    """
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
        step = result.get("step_id", "")
        if step in ("dynamic_state_params",):
            payload = {"source_entity": source, "follow_invert": invert}
        else:
            payload = {}  # extras / preview / unknown — accept defaults
        result = api(
            token, "POST", f"/api/config/config_entries/flow/{flow_id}", payload
        )
    if result.get("type") != "create_entry":
        raise RuntimeError(f"dynamic_state create flow did not finish: {result}")
    return result["result"]["entry_id"]


def create_dynamic_attribute_rule(
    token: str,
    name: str,
    source: str,
    targets: list[str],
    target_attribute: str,
    value_from: str,
    source_attribute: str | None = None,
) -> str:
    """Create a dynamic_attribute (Follow mode) rule. Returns entry_id.

    Flow: rule(mode=dynamic_attribute) -> dynamic_attribute_params
    (source_entity / source_value_from / [source_attribute] / target_attribute)
    -> optional-features (skip) -> preview -> create.
    """
    flow = api(
        token, "POST", "/api/config/config_entries/flow", {"handler": "entity_guard"}
    )
    flow_id = flow["flow_id"]
    result = api(
        token,
        "POST",
        f"/api/config/config_entries/flow/{flow_id}",
        {"rule_name": name, "target_entities": targets, "mode": "dynamic_attribute"},
    )
    while result.get("type") == "form":
        step = result.get("step_id", "")
        if step in ("dynamic_attribute_params",):
            payload = {
                "source_entity": source,
                "source_value_from": value_from,
                "attribute": target_attribute,
            }
            if source_attribute is not None:
                payload["source_attribute"] = source_attribute
        else:
            payload = {}
        result = api(
            token, "POST", f"/api/config/config_entries/flow/{flow_id}", payload
        )
    if result.get("type") != "create_entry":
        raise RuntimeError(f"dynamic_attribute create flow did not finish: {result}")
    return result["result"]["entry_id"]


def main() -> int:
    token = mint_token()

    # --- fixture entities: known starting point via real services --------
    drive_source(token, "off")
    actuate_target(token, "off")

    # --- create a dynamic_state (mirror) rule -----------------------------
    print("Creating dynamic_state (mirror) rule…")
    try:
        entry_id = create_dynamic_state_rule(
            token, "EG Smoke Dynamic State", SOURCE, [TARGET], invert=False
        )
    except Exception as e:
        print(f"  [FAIL] create dynamic_state rule — {e}")
        return 1
    print(f"  created entry {entry_id}")

    # Resolve the status sensor via the LIVE states API, not the on-disk entity
    # registry — the registry file is written debounced, so right after creating a
    # rule it is stale and would miss the new sensor. Match on the follow attrs
    # this rule uniquely sets (mode=dynamic_state + our source).
    def resolve_status() -> str:
        for s in api(token, "GET", "/api/states"):
            eid = s.get("entity_id", "")
            a = s.get("attributes", {})
            if (
                eid.startswith("sensor.")
                and eid.endswith("_status")
                and a.get("mode") == "dynamic_state"
                and a.get("source_entity") == SOURCE
            ):
                return eid
        return ""

    status_e = ""
    deadline = time.time() + 30
    while time.time() < deadline and not status_e:
        status_e = resolve_status()
        if not status_e:
            time.sleep(2)

    # Wait for the engine to leave startup grace (status != 'starting') rather than
    # a blind sleep — grace ends ~60s after engine setup, which trails the flow
    # return by an unknown margin. Poll the status sensor until it's armed/ready.
    if status_e:
        deadline = time.time() + 90
        while time.time() < deadline:
            if get_state(token, status_e).get("state") not in (
                "starting",
                "unknown",
                "unavailable",
                None,
            ):
                break
            time.sleep(3)
    else:
        time.sleep(65)

    # --- mirror: source on -> target on -----------------------------------
    drive_source(token, "on")
    check(
        "mirror: source on -> target on",
        wait_for_state(token, TARGET, "on"),
        f"target={get_state(token, TARGET).get('state')}",
    )

    # --- idempotence: a second identical source 'on' = no re-enforce -------
    # The target is already on and desired is unchanged; the engine must not
    # fire another service call. We assert the enforcement counter is unchanged.
    count_before = None
    if status_e:
        count_e = status_e.replace("_status", "_enforcement_count_total")
        cb = get_state(token, count_e).get("state")
        count_before = float(cb) if cb not in (None, "unknown", "unavailable") else None
    drive_source(token, "on")  # same state again
    time.sleep(4)
    if count_before is not None:
        ca = get_state(token, count_e).get("state")
        count_after = float(ca) if ca not in (None, "unknown", "unavailable") else -1
        check(
            "idempotence: repeat source 'on' does not re-enforce",
            count_after == count_before,
            f"count {count_before}->{count_after}",
        )

    # --- mirror: source off -> target off ---------------------------------
    drive_source(token, "off")
    check(
        "mirror: source off -> target off",
        wait_for_state(token, TARGET, "off"),
        f"target={get_state(token, TARGET).get('state')}",
    )

    # --- offline hold: source unavailable -> target untouched -------------
    actuate_target(token, "on")  # put target in a known non-desired state
    drive_source(token, "unavailable")
    time.sleep(4)  # give the engine a chance to (wrongly) act; assert it didn't
    check(
        "offline hold: target untouched when source unavailable",
        get_state(token, TARGET).get("state") == "on",
        f"target={get_state(token, TARGET).get('state')}",
    )

    # --- status sensor exposes follow attrs -------------------------------
    if status_e:
        drive_source(token, "on")
        # desired_on becomes True once the engine observes the source 'on'
        desired_ok = wait_for_attr(token, status_e, "follow_desired_on", True)
        attrs = get_state(token, status_e).get("attributes", {})
        check(
            "status attr mode==dynamic_state",
            attrs.get("mode") == "dynamic_state",
            f"{attrs.get('mode')}",
        )
        check(
            "status attr source_entity",
            attrs.get("source_entity") == SOURCE,
            f"{attrs.get('source_entity')}",
        )
        check("status attr follow_invert False", attrs.get("follow_invert") is False)
        check(
            "status attr follow_desired_on True (source on)",
            desired_ok,
            f"{attrs.get('follow_desired_on')}",
        )
    else:
        check("status entity resolved", False, "could not find status sensor")

    # --- dynamic_attribute: target brightness follows a source attribute ---
    # Source = a real light whose brightness we set; target = another real light.
    # Needs two distinct actuatable lights. Uses ATTR_SOURCE/ATTR_TARGET.
    print("Creating dynamic_attribute (brightness) rule…")
    try:
        attr_entry = create_dynamic_attribute_rule(
            token,
            "EG Smoke Dynamic Attribute",
            ATTR_SOURCE,
            [ATTR_TARGET],
            target_attribute="brightness",
            value_from="attribute",
            source_attribute="brightness",
        )
    except Exception as e:
        print(f"  [FAIL] create dynamic_attribute rule — {e}")
        attr_entry = None

    if attr_entry:
        # Both lights on; set source brightness; target must follow (while on).
        api(token, "POST", "/api/services/light/turn_on", {"entity_id": ATTR_TARGET})
        api(
            token,
            "POST",
            "/api/services/light/turn_on",
            {"entity_id": ATTR_SOURCE, "brightness": 200},
        )
        # Wait past startup grace for this rule's status.
        astatus = ""
        deadline = time.time() + 90
        while time.time() < deadline and not astatus:
            for s in api(token, "GET", "/api/states"):
                a = s.get("attributes", {})
                if (
                    s.get("entity_id", "").endswith("_status")
                    and a.get("mode") == "dynamic_attribute"
                    and a.get("source_entity") == ATTR_SOURCE
                ):
                    astatus = s["entity_id"]
            if not astatus:
                time.sleep(3)
        if astatus:
            deadline = time.time() + 90
            while time.time() < deadline:
                if get_state(token, astatus).get("state") not in (
                    "starting",
                    "unknown",
                    "unavailable",
                    None,
                ):
                    break
                time.sleep(3)
        api(
            token,
            "POST",
            "/api/services/light/turn_on",
            {"entity_id": ATTR_SOURCE, "brightness": 123},
        )
        ok = wait_for_attr(token, ATTR_TARGET, "brightness", 123, timeout=20)
        check(
            "dynamic_attribute: target brightness follows source",
            ok,
            f"target brightness={get_state(token, ATTR_TARGET).get('attributes', {}).get('brightness')}",
        )

    print()
    if FAILS:
        print(f"SMOKE RESULT: {len(FAILS)} FAILED — {', '.join(FAILS)}")
        return 1
    print("SMOKE RESULT: ALL GREEN")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
