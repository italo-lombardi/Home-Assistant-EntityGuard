#!/usr/bin/env python3
"""Smoke test: unsupported-domain repair issue.

When a rule's enforce path resolves NO service mapping for a target (a custom
non-on/off target_state on a domain Entity Guard can't drive), the engine both
records the per-target no_service_mapping outcome AND raises a Home Assistant
repair issue (translation_key "unsupported_domain", id
"unsupported_domain_<domain>") with a "Learn more" link to a GitHub issue-form
asking for that domain's service mapping. One card per domain, keyed by the
target's domain — not per rule.

REST/WS-observable surface asserted here (allowed/disposable entities only):
  1. A fixed_state rule whose target_state resolves to no service mapping raises
     the unsupported_domain_<domain> repair issue, with translation_key
     "unsupported_domain" and translation_placeholders {"domain": <domain>}.
  2. The issue carries a learn_more_url pointing at the issue-form template.

WHY input_boolean (a MAPPED domain) with target_state "gamma":
  This is the deterministic, ZERO-side-effect trigger for no_service_mapping.
  input_boolean is an ALLOWED smoke target; its service map is {on, off} only,
  so a target_state of "gamma" (not on/off, no mode mapping) resolves to NO
  service — the service is never called, no device is touched. The engine raises
  the card keyed by the target's domain, so the issue_id is
  unsupported_domain_input_boolean. The card copy is deliberately domain-level
  ("Entity Guard can't control the input_boolean domain yet") — honest for the
  user-facing path, since from the enforce path the only signal is "this domain
  produced no service for the requested state." Driving the genuinely
  neither-mapped domains live (humidifier/remote/siren/script/automation) would
  need a disposable helper the REST collection API can't create reliably in this
  container (memory: smoke_test_entities); the domain the card names differs, the
  repair-raise path asserted here is identical.

HONEST SCOPE — what this does NOT cover live:
  - The learn_more_url None branch (integration-not-loaded / no issue_tracker):
    unforceable from outside the process; unit-locked by
    test_raise_unsupported_domain_no_issue_tracker +
    test_raise_unsupported_domain_integration_not_loaded.
  - No self-clear is asserted: the card intentionally has no runtime clear path
    (an unmapped domain only becomes mapped via code update + restart, which
    clears repair issues anyway) — nothing to observe.

The issue registry has no REST endpoint, so this reads it via a minimal
WebSocket call (repairs/list_issues), the same pattern as smoke_target_missing.

Reuses smoke_follow.py helpers (token mint, REST api, create flow, cleanup).
Requires the integration branch (unsupported_domain repair) deployed to
serene_booth.
Usage: docker exec serene_booth python3 \
  /workspaces/home-assistant-core/config/smoke_unsupported_domain.py
Exit 0 = all green, 1 = a failure.
"""

from __future__ import annotations

import asyncio
import sys
import time

sys.path.insert(0, "/workspaces/home-assistant-core/config")
import smoke_follow as sf  # shared helpers, resolved inside the container

WS_URL = "ws://localhost:8123/api/websocket"

# input_boolean is an ALLOWED smoke target (memory: smoke_test_entities). Its
# service map is {on, off} only — a target_state of "gamma" resolves to NO
# service, the deterministic no_service_mapping path, zero device side effect.
UNDRIVABLE_TARGET = "input_boolean.ea_ctrl_kitchen_1"
UNDRIVABLE_STATE = "gamma"  # not on/off, not in any service map
EXPECTED_DOMAIN = UNDRIVABLE_TARGET.split(".", 1)[0]  # input_boolean
EXPECTED_ISSUE_ID = f"unsupported_domain_{EXPECTED_DOMAIN}"


async def _list_issues(token: str) -> list[dict]:
    """Read the repair issue registry via WS repairs/list_issues."""
    import aiohttp

    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(WS_URL) as ws:
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


def _unsupported_domain_issue(token: str) -> dict | None:
    """Return the entity_guard unsupported_domain_<domain> issue, or None."""
    for issue in asyncio.run(_list_issues(token)):
        if (
            issue.get("domain") == "entity_guard"
            and issue.get("issue_id") == EXPECTED_ISSUE_ID
        ):
            return issue
    return None


def _wait_for_issue(token: str, timeout: int = 30) -> dict | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        issue = _unsupported_domain_issue(token)
        if issue:
            return issue
        time.sleep(2)
    return None


def main() -> int:
    token = sf.mint_token()
    run_token = str(int(time.time()))
    rule_name = f"EG Unsupported Domain Smoke {run_token}"

    print(f"Creating fixed_state rule '{rule_name}' (undrivable target_state)…")
    # trigger_states covers the target's real states (on/off) so whatever state
    # input_boolean is in, the rule sees a trigger and attempts to enforce the
    # undrivable "gamma" → no_service_mapping → repair issue raised.
    entry_id = sf.create_fixed_state_rule(
        token,
        rule_name,
        [UNDRIVABLE_TARGET],
        target_state=UNDRIVABLE_STATE,
        trigger_states=["on", "off"],
    )
    print(f"  created entry {entry_id}")

    try:
        # Nudge the target so the rule evaluates + attempts enforcement. Toggling
        # input_boolean is a no-op side-effect on an allowed entity; enforcing
        # "gamma" resolves to no service and raises the repair.
        cur = sf.get_state(token, UNDRIVABLE_TARGET).get("state", "off")
        flip = "off" if cur == "on" else "on"
        sf.api(
            token,
            "POST",
            f"/api/services/input_boolean/turn_{flip}",
            {"entity_id": UNDRIVABLE_TARGET},
        )

        issue = _wait_for_issue(token)
        sf.check(
            f"unsupported_domain repair issue raised ({EXPECTED_ISSUE_ID})",
            issue is not None,
            f"issue={issue}",
        )
        if issue:
            sf.check(
                "issue translation_key == unsupported_domain",
                issue.get("translation_key") == "unsupported_domain",
                f"translation_key={issue.get('translation_key')}",
            )
            placeholders = issue.get("translation_placeholders") or {}
            sf.check(
                "issue placeholder names the target domain",
                placeholders.get("domain") == EXPECTED_DOMAIN,
                f"placeholders={placeholders}",
            )
            # The Learn-more link is the whole point of the card (report the
            # domain). Assert it points at the issue-form template.
            learn_more = issue.get("learn_more_url") or ""
            sf.check(
                "learn_more_url points at the unsupported_domain issue form",
                "template=unsupported_domain.yml" in learn_more,
                f"learn_more_url={learn_more}",
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
