#!/usr/bin/env python3
"""Smoke test: the `forced` origin marker on the live event payload.

Every Entity Guard event carries a boolean `forced` field: `true` when the
enforce came from a manual test-enforce (the rule card's button /
`entity_guard.test_enforce`), `false` for an automatic state-trigger. The
deterministic threading of `forced` to all seven async_fire sites is locked by
the unit tests (tests/test_rule_engine.py::test_forced_*). This adds the live
confirmation that the bit actually lands on the wire, which the pulse-sensor
smokes CANNOT prove — a binary_sensor pulse has no payload fields, so reading
`forced` needs a real event subscription over the WebSocket API.

Both legs use ONE fixed_state rule over ONE allowed light target and match the
received events by `rule_id` (the rule's entry_id) so unrelated rules can't
pollute the assertion:

  AUTOMATIC leg — force the target to the trigger state; the engine auto-enforces
    on its own eval. Assert the `entity_guard_enforced` event for this rule has
    `forced == false`.
  MANUAL leg — press `button.<slug>_test_enforce`. Assert the
    `entity_guard_enforced` event for this rule has `forced == true`.

Reuses smoke_follow.py + smoke_verify_enforce.py helpers (token mint, REST api,
fixed_state flow, force_state). WS subscribe modelled on
smoke_deliverable_states._ws_cmd_session but keeps listening for pushed `event`
frames, not just the subscribe `result` ack.

Requires the `forced`-marker code deployed to serene_booth.
Usage: docker exec serene_booth python3 \
  /workspaces/home-assistant-core/config/smoke_forced_marker.py
Exit 0 = all hard assertions green, 1 = a failure.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time

sys.path.insert(0, "/workspaces/home-assistant-core/config")
import smoke_follow as sf  # shared helpers, resolved inside the container
import smoke_verify_enforce as sve  # force_state / TARGET

WS = "ws://localhost:8123/api/websocket"
TARGET = sve.TARGET  # light.living_room_under_cabinet_lights (allowed)
EVENT = "entity_guard_enforced"


async def _capture_forced(token: str, rule_id: str, action, timeout: float = 25.0):
    """Subscribe to EVENT over WS, run `action()` (a plain callable that drives
    the enforce), and return the `forced` value of the first enforced event whose
    `rule_id` matches ours. Returns None on timeout / no matching event.

    `action` is called AFTER the subscription is confirmed so the triggering
    enforce can't fire before we're listening."""
    import websockets

    async with websockets.connect(WS, max_size=None) as ws:
        await ws.recv()  # auth_required
        await ws.send(json.dumps({"type": "auth", "access_token": token}))
        if json.loads(await ws.recv()).get("type") != "auth_ok":
            raise RuntimeError("WS auth failed")
        await ws.send(
            json.dumps({"id": 1, "type": "subscribe_events", "event_type": EVENT})
        )
        if not json.loads(await ws.recv()).get("success"):
            raise RuntimeError("subscribe_events failed")

        # Subscription live — now drive the enforce (off the event loop thread).
        await asyncio.get_event_loop().run_in_executor(None, action)

        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                msg = json.loads(
                    await asyncio.wait_for(ws.recv(), timeout=deadline - time.time())
                )
            except asyncio.TimeoutError:
                return None
            if msg.get("type") != "event":
                continue
            data = msg["event"]["data"]
            if data.get("rule_id") == rule_id:
                return data.get("forced")
        return None


def press(token: str, entity_id: str) -> None:
    sf.api(token, "POST", "/api/services/button/press", {"entity_id": entity_id})


def resolve_unique_id(token: str, name: str) -> str | None:
    """Return the rule's unique_id (the id carried as `rule_id` on every event)
    by matching the rule NAME via the list_rules service. The create flow returns
    the config entry_id (a ULID), which is a DIFFERENT id space from the event's
    rule_id (the unique_id / UUID) — matching on entry_id never hits."""
    resp = sf.api(
        token,
        "POST",
        "/api/services/entity_guard/list_rules?return_response",
        {},
    )
    rules = (resp.get("service_response") or {}).get("rules", [])
    for r in rules:
        if r.get("name") == name:
            return r.get("rule_id")
    return None


def main() -> int:
    token = sf.mint_token()
    sf.cleanup(token)  # clear orphan EG rules from prior runs

    ok = True
    name = "ea_smoke_forced_marker"
    slug = name

    entry = sf.create_fixed_state_rule(
        token,
        name,
        [TARGET],
        target_state="on",
        trigger_states=["off"],  # desired excluded from triggers (target_in_triggers)
    )
    sf.check("forced-marker rule created", bool(entry))
    ok = ok and bool(entry)

    armed = sf.wait_for_armed(token, "fixed_state", [TARGET])
    sf.check("rule armed past startup grace", armed)
    ok = ok and armed

    # Events carry rule_id = the rule's unique_id (UUID), NOT the config entry_id
    # (ULID) that the create flow returned. Resolve the unique_id by name so the
    # event match below is in the right id space.
    rule_id = resolve_unique_id(token, name)
    sf.check(
        "resolved rule unique_id via list_rules", bool(rule_id), f"rule_id={rule_id!r}"
    )
    ok = ok and bool(rule_id)

    # --- AUTOMATIC leg: forcing the target to the trigger state makes the engine
    # enforce on its own; the event's `forced` must be False. -------------------
    sve.force_state(
        token, TARGET, "on"
    )  # seed off-trigger so the drop is a real change
    time.sleep(1)
    auto_forced = asyncio.run(
        _capture_forced(token, rule_id, lambda: sve.force_state(token, TARGET, "off"))
    )
    sf.check(
        "automatic enforce → event forced == False",
        auto_forced is False,
        f"forced={auto_forced!r}",
    )
    ok = ok and auto_forced is False

    # --- MANUAL leg: pressing the test_enforce button is a manual force; the
    # event's `forced` must be True. --------------------------------------------
    sve.force_state(
        token, TARGET, "on"
    )  # settle to desired so the forced pass has a clean start
    time.sleep(1)
    button = f"button.{slug}_test_enforce"
    man_forced = asyncio.run(
        _capture_forced(token, rule_id, lambda: press(token, button))
    )
    sf.check(
        "manual test_enforce → event forced == True",
        man_forced is True,
        f"forced={man_forced!r}",
    )
    ok = ok and man_forced is True

    sf.cleanup(token)
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
