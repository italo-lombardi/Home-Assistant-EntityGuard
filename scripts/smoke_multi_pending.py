#!/usr/bin/env python3
"""Smoke test: multi-entity pending_enforcement_due broadcast fix.

Verifies that when a multi-target rule with delay_seconds > 0 has multiple
non-compliant entities, ALL of them appear in the pending_enforcement_due
sensor attribute — not just the first one triggered.

Strategy: enable the Multi-Target Fan Guard rule (targets fan.bedroom_fan and
fan.living_room_fan), set delay=8s, then inject both fan states via the HA
states API (POST /api/states/{id} works for any entity), trigger non-compliance,
and verify both appear in pending_enforcement_due.

Usage: docker exec serene_booth python3 /tmp/smoke_multi_pending.py
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
# Rule: trigger_states=["on"], target_state="off" — so fans being "on" = non-compliant


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
        body_txt = e.read().decode()
        raise RuntimeError(f"HTTP {e.code} {path}: {body_txt}") from e


def state(token: str, entity_id: str) -> dict:
    return api(token, "GET", f"/api/states/{entity_id}")


def set_state(
    token: str, entity_id: str, new_state: str, attributes: dict | None = None
) -> None:
    """Inject a state via POST /api/states/{entity_id} (works for any entity)."""
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
    print("=== Smoke test: multi-entity pending_enforcement_due fix ===\n")

    token = mint_token()
    print("Authenticated OK")

    # Record initial state
    orig_switch = state(token, RULE_SWITCH)["state"]
    orig_delay = float(state(token, RULE_DELAY)["state"])
    print(f"  Rule initially: enabled={orig_switch}, delay={orig_delay}s\n")

    try:
        # Step 1: set fans to "off" (compliant baseline)
        print("Step 1: set fans to off (compliant baseline)")
        set_state(token, FAN_A, "off", {"friendly_name": "Bedroom Fan (test)"})
        set_state(token, FAN_B, "off", {"friendly_name": "Living Room Fan (test)"})
        time.sleep(1)

        # Step 2: enable rule + set delay=8s
        print("Step 2: enable rule and set delay=8s")
        call_service(token, "switch", "turn_on", {"entity_id": RULE_SWITCH})
        time.sleep(1)
        set_number(token, RULE_DELAY, 8)
        time.sleep(3)  # debounce + apply

        status_before = state(token, STATUS_SENSOR)["state"]
        print(f"  Rule status after enable: {status_before}")
        check(
            "Rule status is armed",
            status_before == "armed",
            f"got '{status_before}'",
        )

        # Step 3: trigger both fans to "on" (non-compliant)
        print("\nStep 3: set both fans to on (non-compliant)")
        set_state(token, FAN_A, "on", {"friendly_name": "Bedroom Fan (test)"})
        time.sleep(0.5)
        set_state(token, FAN_B, "on", {"friendly_name": "Living Room Fan (test)"})
        time.sleep(2)  # let engine react and broadcast

        # Step 4: inspect status sensor
        print("\nStep 4: inspect pending_enforcement_due")
        sensor_st = state(token, STATUS_SENSOR)
        status = sensor_st["state"]
        attrs = sensor_st.get("attributes", {})
        pending_due = attrs.get("pending_enforcement_due", {})

        print(f"  Rule status: {status}")
        print(f"  pending_enforcement_due keys: {list(pending_due.keys())}")

        check("Rule status is pending", status == "pending", f"got '{status}'")
        check(
            f"{FAN_A} in pending_enforcement_due",
            FAN_A in pending_due,
            f"keys present: {list(pending_due.keys())}",
        )
        check(
            f"{FAN_B} in pending_enforcement_due",
            FAN_B in pending_due,
            f"keys present: {list(pending_due.keys())}",
        )
        check(
            "Both due timestamps are non-null",
            pending_due.get(FAN_A) is not None and pending_due.get(FAN_B) is not None,
        )

        print("\n=== All checks PASSED — fix confirmed ===")
        print("Both fans appear independently in pending_enforcement_due.")

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
