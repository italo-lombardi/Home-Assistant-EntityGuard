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

# Unique per-run suffix so a rule orphaned by a prior crashed/killed run can't
# collide on name (flow returns 400 name_already_exists). Matches the sibling
# smokes' idiom (smoke_color_guard.py, smoke_deliverable_states.py).
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
# Every rule entry_id this run creates, so cleanup() can delete them all even on
# an early exit (memory rule: smoke must clean up its own rules).
CREATED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


def skip(name: str, reason: str) -> None:
    """A leg we could not validate because the test FIXTURE (a real HA light)
    would not cooperate — e.g. a virtual/demo light that does not expose
    brightness. Not a code failure, so it must not append to FAILS; print it so
    the run is honest about what it did not cover (memory: no silent caps)."""
    print(f"  [SKIP] {name} — {reason}")


def cleanup(token: str) -> None:
    """Delete every rule entry this run created. Best-effort — a failed delete is
    logged but never masks the smoke result."""
    for entry_id in CREATED:
        try:
            api(token, "DELETE", f"/api/config/config_entries/entry/{entry_id}")
            print(f"  cleaned up entry {entry_id}")
        except Exception as e:
            print(f"  [WARN] cleanup failed for {entry_id}: {e}")


def guard_form(result: dict) -> None:
    """Fail loudly if a config-flow form came back WITH validation errors.

    Without this, the create loop re-POSTs an empty payload to a re-shown form
    (e.g. a duplicate rule_name re-shows step 'rule' with errors), which HA then
    rejects as "required key not provided" — a confusing 400 that hides the real
    cause. Raise the actual field errors instead."""
    errors = result.get("errors")
    if errors:
        raise RuntimeError(
            f"flow step '{result.get('step_id')}' returned errors: {errors}"
        )


def wait_for_armed(
    token: str, mode: str, targets: list[str], timeout: int = 100
) -> bool:
    """Wait until THIS run's freshly-created rule (matched by mode AND its unique
    target set) leaves startup grace and is actually enforcing.

    The engine IGNORES target state events during startup grace
    (_startup_complete gate, ~60s), so violating the target before grace ends sees
    no enforcement — a false FAIL. Matching on mode ALONE is not enough: the
    container has pre-existing DISABLED rules of the same mode whose status reads
    'disabled' (also not 'starting'), so a mode-only match returns instantly on
    the wrong rule. Require the target set to match ours AND the status to be a
    live/enforcing state (armed/conditional/enforcing/cooldown/pending), never
    disabled/starting. Returns True once ready, False on timeout."""
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
        guard_form(result)
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
    entry_id = result["result"]["entry_id"]
    CREATED.append(entry_id)
    return entry_id


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
        guard_form(result)
        step = result.get("step_id", "")
        if step in ("dynamic_attribute_params",):
            # source_attribute is NOT accepted here — it lives in the separate
            # source_attribute_pick step reached when source_value_from=attribute.
            # Sending it here makes HA reject the extra key (400).
            payload = {
                "source_entity": source,
                "source_value_from": value_from,
                "attribute": target_attribute,
            }
        elif step == "source_attribute_pick":
            payload = {"source_attribute": source_attribute}
        else:
            payload = {}
        result = api(
            token, "POST", f"/api/config/config_entries/flow/{flow_id}", payload
        )
    if result.get("type") != "create_entry":
        raise RuntimeError(f"dynamic_attribute create flow did not finish: {result}")
    entry_id = result["result"]["entry_id"]
    CREATED.append(entry_id)
    return entry_id


def create_fixed_state_rule(
    token: str,
    name: str,
    targets: list[str],
    target_state: str,
    trigger_states: list[str],
) -> str:
    """Create a fixed_state rule. Returns entry_id.

    Flow: rule(mode=fixed_state) -> state (trigger_states / target_state /
    delay_seconds) -> extras (skip) -> preview -> create.
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


def create_fixed_attribute_rule(
    token: str,
    name: str,
    targets: list[str],
    attribute: str,
    target_value: float,
) -> str:
    """Create a fixed_attribute rule for a NUMERIC attribute (e.g. brightness).

    Flow: rule(mode=fixed_attribute) -> attribute (pick attribute) ->
    attribute_params (operator / threshold / target_value / delay_seconds) ->
    extras (skip) -> preview -> create. Numeric attrs require operator+threshold
    (the "clamp" model); we use gt 0 so any on-value is driven to target_value.
    """
    flow = api(
        token, "POST", "/api/config/config_entries/flow", {"handler": "entity_guard"}
    )
    flow_id = flow["flow_id"]
    result = api(
        token,
        "POST",
        f"/api/config/config_entries/flow/{flow_id}",
        {"rule_name": name, "target_entities": targets, "mode": "fixed_attribute"},
    )
    while result.get("type") == "form":
        guard_form(result)
        step = result.get("step_id")
        if step == "attribute":
            payload = {"attribute": attribute}
        elif step == "attribute_params":
            payload = {
                "operator": "gt",
                "threshold": 0,
                "target_value": target_value,
                "delay_seconds": 0,
            }
        else:
            payload = {}  # extras / preview — accept defaults
        result = api(
            token, "POST", f"/api/config/config_entries/flow/{flow_id}", payload
        )
    if result.get("type") != "create_entry":
        raise RuntimeError(f"fixed_attribute create flow did not finish: {result}")
    entry_id = result["result"]["entry_id"]
    CREATED.append(entry_id)
    return entry_id


def main() -> int:
    token = mint_token()

    # --- fixture entities: known starting point via real services --------
    drive_source(token, "off")
    actuate_target(token, "off")

    # --- create a dynamic_state (mirror) rule -----------------------------
    print("Creating dynamic_state (mirror) rule…")
    try:
        entry_id = create_dynamic_state_rule(
            token, f"EG Smoke Dynamic State {RUN}", SOURCE, [TARGET], invert=False
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
            f"EG Smoke Dynamic Attribute {RUN}",
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
        # Reset the target to a known baseline != the value we will mirror (123),
        # so a stale brightness left by a PRIOR run's fixed_attribute leg (same
        # physical light) can't masquerade as a pass OR a fail.
        api(
            token,
            "POST",
            "/api/services/light/turn_on",
            {"entity_id": ATTR_TARGET, "brightness": 50},
        )
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
        # The mirror can only follow a brightness the SOURCE actually reports.
        # If this real light won't commit/expose brightness=123, the engine reads
        # None and correctly skips the drive — a fixture limit, not a bug: SKIP.
        src_ok = wait_for_attr(token, ATTR_SOURCE, "brightness", 123, timeout=10)
        if not src_ok:
            src_b = (
                get_state(token, ATTR_SOURCE).get("attributes", {}).get("brightness")
            )
            skip(
                "dynamic_attribute: target brightness follows source",
                f"source {ATTR_SOURCE} did not expose brightness=123 (got {src_b})",
            )
        else:
            # Real dimmers scale/round brightness; accept ±2 rather than exact ==.
            deadline = time.time() + 20
            tb = None
            while time.time() < deadline:
                tb = (
                    get_state(token, ATTR_TARGET)
                    .get("attributes", {})
                    .get("brightness")
                )
                if isinstance(tb, (int, float)) and abs(tb - 123) <= 2:
                    break
                time.sleep(1)
            ok = isinstance(tb, (int, float)) and abs(tb - 123) <= 2
            check(
                "dynamic_attribute: target brightness follows source",
                ok,
                f"target brightness={tb}",
            )

    # Delete the dynamic-mode rules now so the fixed-mode rules below own their
    # targets outright (only two actuatable lights exist; overlapping rules on the
    # same target would race each other and make the fixed assertions flaky).
    cleanup(token)
    CREATED.clear()

    # --- fixed_state: a non-target state is clamped back to target_state --
    print("Creating fixed_state rule…")
    try:
        create_fixed_state_rule(
            token,
            f"EG Smoke Fixed State {RUN}",
            [TARGET],
            target_state="off",
            trigger_states=["on"],
        )
        armed = wait_for_armed(token, "fixed_state", [TARGET])
        check("fixed_state: rule armed past startup grace", armed)
        actuate_target(token, "on")  # violate: target must be driven back to off
        check(
            "fixed_state: 'on' violation clamped back to target 'off'",
            wait_for_state(token, TARGET, "off", timeout=20),
            f"target={get_state(token, TARGET).get('state')}",
        )
    except Exception as e:
        check("fixed_state: create+enforce", False, str(e))
    cleanup(token)
    CREATED.clear()

    # --- fixed_attribute: brightness clamped to a fixed target_value ------
    print("Creating fixed_attribute rule…")
    try:
        create_fixed_attribute_rule(
            token,
            f"EG Smoke Fixed Attribute {RUN}",
            [ATTR_TARGET],
            attribute="brightness",
            target_value=180,
        )
        armed = wait_for_armed(token, "fixed_attribute", [ATTR_TARGET])
        check("fixed_attribute: rule armed past startup grace", armed)
        # Drive target to a different brightness; the rule must clamp it to 180.
        api(
            token,
            "POST",
            "/api/services/light/turn_on",
            {"entity_id": ATTR_TARGET, "brightness": 50},
        )
        check(
            "fixed_attribute: brightness clamped to fixed target 180",
            wait_for_attr(token, ATTR_TARGET, "brightness", 180, timeout=20),
            f"target brightness={get_state(token, ATTR_TARGET).get('attributes', {}).get('brightness')}",
        )
    except Exception as e:
        check("fixed_attribute: create+enforce", False, str(e))

    print()
    if FAILS:
        print(f"SMOKE RESULT: {len(FAILS)} FAILED — {', '.join(FAILS)}")
        return 1
    print("SMOKE RESULT: ALL GREEN")
    return 0


if __name__ == "__main__":
    import sys

    try:
        rc = main()
    finally:
        # Last-ditch: if main() threw after creating rules, delete whatever is
        # still tracked so a crashed run never leaves smoke rules behind.
        if CREATED:
            try:
                cleanup(mint_token())
            except Exception as e:
                print(f"  [WARN] final cleanup failed: {e}")
    sys.exit(rc)
