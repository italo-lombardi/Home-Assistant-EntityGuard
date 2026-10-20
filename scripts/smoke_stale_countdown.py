#!/usr/bin/env python3
"""Smoke test: stale countdown clears when entity returns to compliant state.

Verifies that when one entity in a multi-target rule turns compliant while
another is still pending, its entry is removed from pending_enforcement_due
AND the sensor broadcasts the updated dict so the card stops showing its
countdown.

Strategy: enable Multi-Target Fan Guard, set delay=10s, inject both fans as
'on' (non-compliant), confirm both appear in pending_enforcement_due, then
set one fan to 'off' (compliant) and confirm it is removed from the dict
while the other remains.

Usage: docker exec serene_booth python3 /tmp/smoke_stale_countdown.py
Exit 0 = pass, 1 = fail.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request

BASE = "http://localhost:8123"
AUTH_STORE = "/workspaces/home-assistant-core/config/.storage/auth"

RULE_SWITCH = "switch.multi_target_fan_guard_enabled"
RULE_DELAY = "number.multi_target_fan_guard_delay_seconds"
STATUS_SENSOR = "sensor.multi_target_fan_guard_status"
FAN_A = "fan.bedroom_fan"
FAN_B = "fan.living_room_fan"


def mint_token() -> str:
    import jwt

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
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} {path}: {e.read().decode()}") from e


def get_state(token: str, entity_id: str) -> dict:
    return api(token, "GET", f"/api/states/{entity_id}")


def set_state(
    token: str, entity_id: str, new_state: str, attributes: dict | None = None
) -> None:
    body: dict = {"state": new_state}
    if attributes:
        body["attributes"] = attributes
    api(token, "POST", f"/api/states/{entity_id}", body)


def set_number(token: str, entity_id: str, value: float) -> None:
    api(
        token,
        "POST",
        "/api/services/number/set_value",
        {"entity_id": entity_id, "value": value},
    )


def call_service(token: str, domain: str, service: str, data: dict) -> None:
    api(token, "POST", f"/api/services/{domain}/{service}", data)


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}" + (f": {detail}" if detail else ""))
        sys.exit(1)


def main() -> None:
    print("=== Smoke test: stale countdown clears on compliant entity ===\n")

    token = mint_token()
    print("Authenticated OK")

    orig_switch = get_state(token, RULE_SWITCH)["state"]
    orig_delay = float(get_state(token, RULE_DELAY)["state"])
    print(f"  Rule initially: enabled={orig_switch}, delay={orig_delay}s\n")

    try:
        # Step 1: compliant baseline
        print("Step 1: set fans to off (compliant baseline)")
        set_state(token, FAN_A, "off")
        set_state(token, FAN_B, "off")
        time.sleep(1)

        # Step 2: enable rule + set delay=10s
        print("Step 2: enable rule and set delay=10s")
        call_service(token, "switch", "turn_on", {"entity_id": RULE_SWITCH})
        time.sleep(2)
        set_number(token, RULE_DELAY, 10)
        time.sleep(5)

        status_before = get_state(token, STATUS_SENSOR)["state"]
        print(f"  Rule status after enable: {status_before}")
        check(
            "Rule status is armed", status_before == "armed", f"got '{status_before}'"
        )

        # Step 3: both fans non-compliant
        print("\nStep 3: set both fans to on (non-compliant)")
        set_state(token, FAN_A, "on")
        time.sleep(0.3)
        set_state(token, FAN_B, "on")
        time.sleep(2)

        sensor = get_state(token, STATUS_SENSOR)
        due = sensor.get("attributes", {}).get("pending_enforcement_due", {})
        print(f"  Rule status: {sensor['state']}")
        print(f"  pending_enforcement_due keys: {list(due.keys())}")
        check("Rule status is pending", sensor["state"] == "pending", sensor["state"])
        check(
            f"{FAN_A} in pending_enforcement_due",
            FAN_A in due,
            f"keys: {list(due.keys())}",
        )
        check(
            f"{FAN_B} in pending_enforcement_due",
            FAN_B in due,
            f"keys: {list(due.keys())}",
        )

        # Step 4: FAN_A turns compliant
        print(f"\nStep 4: set {FAN_A} to off (compliant)")
        set_state(token, FAN_A, "off")
        time.sleep(2)

        sensor2 = get_state(token, STATUS_SENSOR)
        due2 = sensor2.get("attributes", {}).get("pending_enforcement_due", {})
        print(f"  Rule status: {sensor2['state']}")
        print(f"  pending_enforcement_due keys: {list(due2.keys())}")

        check(
            "Rule stays pending (FAN_B still non-compliant)",
            sensor2["state"] == "pending",
            f"got '{sensor2['state']}'",
        )
        check(
            f"{FAN_A} removed from pending_enforcement_due",
            FAN_A not in due2,
            f"still present — stale countdown bug NOT fixed. keys: {list(due2.keys())}",
        )
        check(
            f"{FAN_B} still in pending_enforcement_due",
            FAN_B in due2,
            f"keys: {list(due2.keys())}",
        )

        print("\n=== All checks PASSED — stale countdown fix confirmed ===")
        print(f"{FAN_A} countdown clears as soon as it turns compliant.")
        print(f"{FAN_B} countdown remains independently.")

    finally:
        print("\nCleanup: restore original state")
        set_state(token, FAN_A, "off")
        set_state(token, FAN_B, "off")
        set_number(token, RULE_DELAY, orig_delay)
        time.sleep(1)
        if orig_switch == "off":
            call_service(token, "switch", "turn_off", {"entity_id": RULE_SWITCH})
        time.sleep(1)
        print("Cleanup done.")


if __name__ == "__main__":
    main()
