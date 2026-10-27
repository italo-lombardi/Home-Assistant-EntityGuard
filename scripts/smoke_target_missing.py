#!/usr/bin/env python3
"""Smoke test: target-entity-missing repair issue (Feature D).

Feature D raises a Home Assistant repair issue (translation_key
"target_entity_missing", id "{entry_id}_missing_targets") when a rule's target
entity is renamed or removed, so a rule silently watching a dead entity_id
surfaces instead of failing invisibly — the kill-switch correctness hole.

REST/WS-observable surface asserted here (allowed/disposable entities only):
  1. A rule targeting a NON-EXISTENT entity (missing at setup — not in the
     entity registry AND not in hass.states) raises the target_entity_missing
     repair issue for that config entry, naming the missing entity.
  2. Removing the rule clears the issue (async_remove_entry deletes it) — no
     orphaned repair left behind.

Why a non-existent target (not a removed real device): it is the clean,
deterministic "registry-missing target" case with ZERO side effect on any real
entity — no device touched, no helper to create/tear-down over the flaky
collection API. The engine never calls a service on a target it cannot resolve.

HONEST SCOPE — what this does NOT cover live:
  - The state-only-present case (target in hass.states but NOT the entity
    registry — e.g. a YAML group.* or a template/MQTT entity without a
    unique_id — must NOT be flagged missing). Staging that live requires a
    states.async_set entity with no registry entry, which cannot be created
    cleanly from outside the process over REST/WS. It is locked by the unit
    test test_state_only_target_not_flagged_missing (present-if-registry-OR-
    states). Not faked here.
  - The rename path (vs plain missing): the listener-fires + check-detects
    rename behavior is unit-locked (test_target_rename_triggers_check + the
    rename-e2e test). This smoke asserts the issue-raised end state over the
    real issue registry, which is the user-visible outcome.

The issue registry has no REST endpoint, so this reads it via a minimal
WebSocket call (repairs/list_issues) using aiohttp (present in the HA venv).

Reuses smoke_follow.py helpers (token mint, REST api, create flow, cleanup).
Requires the integration branch (Feature D) deployed to serene_booth.
Usage: docker exec serene_booth python3 \
  /workspaces/home-assistant-core/config/smoke_target_missing.py
Exit 0 = all green, 1 = a failure.
"""

from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, "/workspaces/home-assistant-core/config")
import smoke_follow as sf  # shared HTTP helpers, resolved inside the container

WS_URL = "ws://localhost:8123/api/websocket"


async def _list_issues(token: str) -> list[dict]:
    """Read the repair issue registry via WS repairs/list_issues. Returns the
    issues list (each a dict with domain, issue_id, translation_key, …)."""
    import aiohttp

    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(WS_URL) as ws:
            # auth handshake
            await ws.receive_json()  # auth_required
            await ws.send_json({"type": "auth", "access_token": token})
            auth = await ws.receive_json()
            if auth.get("type") != "auth_ok":
                raise RuntimeError(f"WS auth failed: {auth}")
            await ws.send_json({"id": 1, "type": "repairs/list_issues"})
            while True:
                msg = await ws.receive_json()
                if msg.get("id") == 1 and msg.get("type") == "result":
                    if not msg.get("success"):
                        raise RuntimeError(f"list_issues failed: {msg}")
                    return msg["result"]["issues"]


def _target_missing_issue(token: str, entry_id: str) -> dict | None:
    """Return the entity_guard target_entity_missing issue for entry_id, or None."""
    issues = asyncio.run(_list_issues(token))
    want_id = f"{entry_id}_missing_targets"
    for issue in issues:
        if issue.get("domain") == "entity_guard" and issue.get("issue_id") == want_id:
            return issue
    return None


def _wait_for_issue(token: str, entry_id: str, timeout: int = 30) -> dict | None:
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        issue = _target_missing_issue(token, entry_id)
        if issue:
            return issue
        time.sleep(2)
    return None


def main() -> int:
    import time

    token = sf.mint_token()
    run_token = str(int(time.time()))
    rule_name = f"EG Target Missing Smoke {run_token}"
    # A target that exists in NEITHER the registry NOR states → missing.
    missing_target = f"light.eg_smoke_absent_{run_token}"

    print(f"Creating fixed_state rule '{rule_name}' targeting absent entity…")
    entry_id = sf.create_fixed_state_rule(
        token,
        rule_name,
        [missing_target],
        target_state="off",
        trigger_states=["on"],
    )
    print(f"  created entry {entry_id}")

    try:
        # The deferred target check runs on setup (HA already running in-container),
        # so the issue should appear shortly after the entry is set up.
        issue = _wait_for_issue(token, entry_id)
        sf.check(
            "target_entity_missing repair issue raised for the absent target",
            issue is not None,
            f"issue={issue}",
        )
        if issue:
            sf.check(
                "issue translation_key == target_entity_missing",
                issue.get("translation_key") == "target_entity_missing",
                f"translation_key={issue.get('translation_key')}",
            )
            placeholders = issue.get("translation_placeholders") or {}
            sf.check(
                "issue names the missing target entity",
                missing_target in (placeholders.get("missing_entities") or ""),
                f"placeholders={placeholders}",
            )

        # Removing the rule must clear the issue (no orphaned repair).
        print("Deleting rule; issue must self-clear…")
        sf.api(token, "DELETE", f"/api/config/config_entries/entry/{entry_id}")
        sf.CREATED.remove(entry_id) if entry_id in sf.CREATED else None
        time.sleep(3)
        cleared = _target_missing_issue(token, entry_id)
        sf.check(
            "repair issue cleared after rule removal",
            cleared is None,
            f"still present: {cleared}",
        )

    finally:
        sf.cleanup(token)

    if sf.FAILS:
        print(f"\nFAILED: {len(sf.FAILS)} check(s): {sf.FAILS}")
        return 1
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"ERROR: {e}")
        try:
            sf.cleanup(sf.mint_token())
        except Exception as ce:
            print(f"  [WARN] cleanup after error failed: {ce}")
        sys.exit(1)
