#!/usr/bin/env python3
"""Smoke test: unsupported-STATE repair issue (Card B).

Card B fires when a rule's enforce path targets a domain Entity Guard DOES
control, but a STATE it has no service mapping for — the domain is in a service
map, yet the requested target_state resolves to no service (or, for a mode-map
domain like climate, is not in the device's valid-states list). The engine
records the per-target no_service_mapping outcome AND raises a Home Assistant
repair issue (translation_key "unsupported_state", id
"unsupported_state_<domain>_<state>") whose copy is honest: Entity Guard can
drive this domain, just not this state. One card per (domain, state).

(The sibling case — a domain in NEITHER map — raises Card A,
"unsupported_domain"; that path is covered by smoke_unsupported_domain.py.)

REST/WS-observable surface asserted here (allowed/disposable entities only):
  1. A fixed_state rule on a MAPPED domain with an off-map target_state raises
     the unsupported_state_<domain>_<state> repair issue, translation_key
     "unsupported_state", placeholders {"domain": <domain>, "state": <state>}.
  2. The issue's learn_more_url points at the unsupported_state issue form AND
     prefills BOTH the domain and the state.
  3. NEGATIVE leg: Card A (unsupported_domain_<domain>) is NOT raised for the
     same mapped domain — the split correctly routes a state gap to Card B.

WHY input_boolean + "gamma":
  input_boolean is an ALLOWED smoke target (memory: smoke_test_entities) and IS
  in DOMAIN_SERVICE_MAP, but its service map is {on, off} only. A target_state
  of "gamma_<run_token>" (not on/off, no mode mapping) resolves to NO service —
  the service is never called, no device is touched — so the engine raises
  Card B keyed unsupported_state_input_boolean_gamma_<run_token>. The per-run
  token makes the card id unique: HA never deletes a non-fixable card (no
  repairs/delete WS) and re-raising a dismissed one keeps it dismissed, so a
  static id would collide with a prior run's leftover. Deterministic, zero side
  effect.

HONEST SCOPE — what this does NOT cover live:
  - The mode-map validity leg (climate target_state not in hvac_modes → Card B):
    needs a disposable climate helper the REST collection API can't create
    reliably here (memory smoke_test_entities); unit-locked by the mode-map
    reject test in tests/test_rule_engine.py. The raise path asserted here is
    identical — the only difference is which map produced the None.
  - The learn_more_url None branch and the no-self-clear behaviour: same as the
    Card A smoke (unit-locked; nothing to observe live).

The issue registry has no REST endpoint, so this reads it via a minimal
WebSocket call (repairs/list_issues), the same pattern as smoke_target_missing.

Reuses smoke_follow.py helpers (token mint, REST api, create flow, cleanup).
Requires the integration branch (unsupported_state repair) deployed to
serene_booth.
Usage: docker exec serene_booth python3 \
  /workspaces/home-assistant-core/config/smoke_unsupported_state.py
Exit 0 = all green, 1 = a failure.
"""

from __future__ import annotations

import asyncio
import sys
import time

sys.path.insert(0, "/workspaces/home-assistant-core/config")
import smoke_follow as sf  # shared helpers, resolved inside the container

WS_URL = "ws://localhost:8123/api/websocket"

# input_boolean IS in DOMAIN_SERVICE_MAP ({on, off}) but an off-map state is the
# Card B (unsupported_state) path. An allowed smoke target; zero side effect.
TARGET = "input_boolean.ea_ctrl_kitchen_1"
DOMAIN = TARGET.split(".", 1)[0]  # input_boolean
# The off-map state carries a per-run token (see main()). HA's issue registry
# keys the repair as unsupported_state_<domain>_<state> and NEVER removes a
# non-fixable card (no repairs/delete WS; re-raising a dismissed one keeps it
# dismissed — dataclasses.replace omits dismissed_version). A per-run-unique
# state gives each run a DISTINCT card id, so the precheck is genuinely clean
# and the post-raise assert proves THIS run's rule raised it — not a leftover.
OFF_MAP_BASE = "gamma"  # mapped domain, unmapped state


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


def _issue_by_id(token: str, issue_id: str) -> dict | None:
    """Return the entity_guard issue with the given id, or None.

    No ignored-flag handling: the per-run-unique state (see main()) gives each
    run a fresh card id, so a hit is always this run's own card — never a
    dismissed leftover from a prior run."""
    for issue in asyncio.run(_list_issues(token)):
        if issue.get("domain") == "entity_guard" and issue.get("issue_id") == issue_id:
            return issue
    return None


def _wait_for_issue(token: str, issue_id: str, timeout: int = 75) -> dict | None:
    # 75s > the 60s default startup grace: a runtime-created rule whose target
    # state never changes only evaluates at the grace-done sweep
    # (_handle_startup_grace_done). If HA runs with EG_STARTUP_GRACE_SECONDS=5
    # the card lands in seconds; without it, the sweep is at 60s — wait past it.
    deadline = time.time() + timeout
    while time.time() < deadline:
        issue = _issue_by_id(token, issue_id)
        if issue:
            return issue
        time.sleep(2)
    return None


def main() -> int:
    token = sf.mint_token()
    run_token = str(int(time.time()))
    rule_name = f"EG Unsupported State Smoke {run_token}"

    # Per-run-unique off-map state → a DISTINCT card id each run. HA never
    # removes a non-fixable repair card (no repairs/delete WS) and re-raising a
    # dismissed one keeps it dismissed, so a static id would collide with a prior
    # run's leftover. A tokenized state sidesteps that: the precheck is honestly
    # clean and the post-raise assert proves THIS run's rule raised the card.
    off_map_state = f"{OFF_MAP_BASE}_{run_token}"
    expected_issue_id = f"unsupported_state_{DOMAIN}_{off_map_state}"

    # Clean registry: this run's card must not pre-exist (it can't — the id is
    # unique to this run — but assert it so a logic slip surfaces loudly).
    pre = _issue_by_id(token, expected_issue_id)
    sf.check(
        f"no pre-existing unsupported_state card ({expected_issue_id})",
        pre is None,
        f"issue={pre}",
    )

    print(f"Creating fixed_state rule '{rule_name}' (off-map state on mapped domain)…")
    # trigger_states covers input_boolean's real states so whatever state it is
    # in, the rule sees a trigger and attempts to enforce the off-map state →
    # mapped domain, unmapped state → no_service_mapping → Card B.
    entry_id = sf.create_fixed_state_rule(
        token,
        rule_name,
        [TARGET],
        target_state=off_map_state,
        trigger_states=["on", "off"],
    )
    print(f"  created entry {entry_id}")

    try:
        # Nudge the target so the rule evaluates + attempts enforcement. Toggling
        # input_boolean is a no-op side-effect on an allowed entity; enforcing
        # the off-map state resolves to no service and raises the repair.
        cur = sf.get_state(token, TARGET).get("state", "off")
        flip = "off" if cur == "on" else "on"
        sf.api(
            token,
            "POST",
            f"/api/services/input_boolean/turn_{flip}",
            {"entity_id": TARGET},
        )

        issue = _wait_for_issue(token, expected_issue_id)
        sf.check(
            f"unsupported_state repair issue raised ({expected_issue_id})",
            issue is not None,
            f"issue={issue}",
        )
        if issue:
            sf.check(
                "issue translation_key == unsupported_state",
                issue.get("translation_key") == "unsupported_state",
                f"translation_key={issue.get('translation_key')}",
            )
            placeholders = issue.get("translation_placeholders") or {}
            sf.check(
                "issue placeholder names the target domain",
                placeholders.get("domain") == DOMAIN,
                f"placeholders={placeholders}",
            )
            sf.check(
                "issue placeholder names the off-map state",
                placeholders.get("state") == off_map_state,
                f"placeholders={placeholders}",
            )
            # Learn-more link: the unsupported_state form, prefilling BOTH fields.
            learn_more = issue.get("learn_more_url") or ""
            sf.check(
                "learn_more_url points at the unsupported_state issue form",
                "template=unsupported_state.yml" in learn_more,
                f"learn_more_url={learn_more}",
            )
            sf.check(
                "learn_more_url prefills the domain field",
                f"&domain={DOMAIN}" in learn_more,
                f"learn_more_url={learn_more}",
            )
            sf.check(
                "learn_more_url prefills the state field",
                f"&state={off_map_state}" in learn_more,
                f"learn_more_url={learn_more}",
            )

        # NEGATIVE: the split must route a mapped-domain state gap to Card B, NOT
        # the old Card A. Assert unsupported_domain_<domain> is absent.
        sf.check(
            f"NO unsupported_domain (Card A) raised for mapped domain ({DOMAIN})",
            _issue_by_id(token, f"unsupported_domain_{DOMAIN}") is None,
            f"issue={_issue_by_id(token, f'unsupported_domain_{DOMAIN}')}",
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
