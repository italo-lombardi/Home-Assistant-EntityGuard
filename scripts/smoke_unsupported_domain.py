#!/usr/bin/env python3
"""Smoke test: unsupported-DOMAIN repair issue (Card A).

Card A fires when a rule's enforce path targets a domain Entity Guard has NO
service mapping for AT ALL — the domain is in neither DOMAIN_SERVICE_MAP nor
DOMAIN_MODE_SERVICE_MAP. The engine records the per-target no_service_mapping
outcome AND raises a Home Assistant repair issue (translation_key
"unsupported_domain", id "unsupported_domain_<domain>") with a "Learn more"
link to a GitHub issue-form asking for that domain's support. One card per
domain, keyed by the target's domain — not per rule.

(The sibling case — a MAPPED domain with an off-map STATE — now raises Card B,
"unsupported_state"; that path is covered by smoke_unsupported_state.py.)

REST/WS-observable surface asserted here (allowed/disposable entities only):
  1. A fixed_state rule whose target domain is in neither service map raises the
     unsupported_domain_<domain> repair issue, with translation_key
     "unsupported_domain" and translation_placeholders {"domain": <domain>}.
  2. The issue carries a learn_more_url pointing at the issue-form template and
     prefilling the domain.
  3. NEGATIVE leg: a mapped domain+state enforce raises NO Card A.

WHY automation as the Card A target:
  automation is a domain Entity Guard cannot drive — it is in NEITHER service
  map — yet the EG config-flow entity selector ACCEPTS it as a target (sun is
  rejected at flow validation, so sun.sun can't be used here). The engine
  resolves no service for an unmapped domain, so nothing is ever called on the
  automation (zero side effect: it is never triggered or toggled). A fixed_state
  rule whose target_state is any non-automation string (e.g. "gamma") can never
  match the live on/off state, so the engine attempts enforcement, hits the
  domain gap, and raises Card A keyed unsupported_domain_automation.
  Deterministic, no device touched. (humidifier/remote/siren are the other
  flow-allowed unmapped domains, but humidifier maps to real diffuser devices —
  memory smoke_test_entities — so automation is the disposable-safe pick.)

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

# automation is in NEITHER service map — the Card A (unsupported_domain) path.
# The EG config-flow entity selector accepts automation as a target domain (sun
# is rejected at flow validation), yet the engine has no service mapping for it,
# so this is a genuinely-unmapped domain the flow will actually let us target.
# Zero side effect: because the domain is unmapped the engine resolves NO service
# and never calls anything on the automation — nothing is triggered or toggled.
# The target_state "gamma" can never match an automation's on/off state, so the
# rule always attempts enforcement and hits the domain gap → Card A.
# (ea_demo_* is a disposable EG demo automation; even so, no service is called.)
UNDRIVABLE_TARGET = "automation.ea_demo_entity_went_offline"
UNDRIVABLE_STATE = "gamma"  # never a real automation state → enforce always attempted
EXPECTED_DOMAIN = UNDRIVABLE_TARGET.split(".", 1)[0]  # automation
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


def _issue_for(token: str, domain: str) -> dict | None:
    """Return the entity_guard unsupported_domain_<domain> issue, or None."""
    want = f"unsupported_domain_{domain}"
    for issue in asyncio.run(_list_issues(token)):
        if issue.get("domain") == "entity_guard" and issue.get("issue_id") == want:
            return issue
    return None


def _wait_for_issue(token: str, timeout: int = 30) -> dict | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        issue = _issue_for(token, EXPECTED_DOMAIN)
        if issue:
            return issue
        time.sleep(2)
    return None


# A fully-mapped target for the NEGATIVE leg: light is in DOMAIN_SERVICE_MAP and
# "on" is a mapped state, so enforcing it resolves a real service → NO card. An
# allowed smoke target (memory: smoke_test_entities).
MAPPED_TARGET = "light.living_room_under_cabinet_lights"
MAPPED_STATE = "on"
MAPPED_DOMAIN = MAPPED_TARGET.split(".", 1)[0]  # light


def main() -> int:
    token = sf.mint_token()
    run_token = str(int(time.time()))
    rule_name = f"EG Unsupported Domain Smoke {run_token}"

    # NEGATIVE leg first (clean registry): a mapped domain+state must NOT raise a
    # card. Guards the regression where the card fires on every enforce. Assert
    # the light card is absent both before and after a mapped rule enforces.
    neg_pre = _issue_for(token, MAPPED_DOMAIN)
    sf.check(
        f"no pre-existing card for mapped domain ({MAPPED_DOMAIN})",
        neg_pre is None,
        f"issue={neg_pre}",
    )
    mapped_name = f"EG Mapped Domain Smoke {run_token}"
    mapped_entry = sf.create_fixed_state_rule(
        token,
        mapped_name,
        [MAPPED_TARGET],
        target_state=MAPPED_STATE,
        trigger_states=["off"],  # must exclude target_state (target_in_triggers)
    )
    try:
        # Drive the target OFF so the rule triggers and enforces "on" (a mapped
        # service call) — the path that would wrongly raise a card if mapped
        # domains leaked into the no_service_mapping branch.
        sf.api(
            token,
            "POST",
            "/api/services/light/turn_off",
            {"entity_id": MAPPED_TARGET},
        )
        time.sleep(6)  # let the enforce + any (wrong) raise settle
        sf.check(
            f"mapped domain+state raises NO unsupported_domain card ({MAPPED_DOMAIN})",
            _issue_for(token, MAPPED_DOMAIN) is None,
            f"issue={_issue_for(token, MAPPED_DOMAIN)}",
        )
    finally:
        if mapped_entry:
            sf.api(token, "DELETE", f"/api/config/config_entries/entry/{mapped_entry}")

    print(f"Creating fixed_state rule '{rule_name}' (unmapped-domain target)…")
    # automation's live state is on/off; trigger on both so the rule evaluates
    # immediately. target_state "gamma" never matches → the engine attempts to
    # enforce an unmapped domain → no_service_mapping → Card A.
    entry_id = sf.create_fixed_state_rule(
        token,
        rule_name,
        [UNDRIVABLE_TARGET],
        target_state=UNDRIVABLE_STATE,
        trigger_states=["on", "off"],
    )
    print(f"  created entry {entry_id}")

    try:
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
            # domain). Assert it points at the issue-form template AND prefills
            # the domain (so the card's "the domain is pre-filled" copy is true).
            learn_more = issue.get("learn_more_url") or ""
            sf.check(
                "learn_more_url points at the unsupported_domain issue form",
                "template=unsupported_domain.yml" in learn_more,
                f"learn_more_url={learn_more}",
            )
            sf.check(
                "learn_more_url prefills the domain field",
                f"&domain={EXPECTED_DOMAIN}" in learn_more,
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
