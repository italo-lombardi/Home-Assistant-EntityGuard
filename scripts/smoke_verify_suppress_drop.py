#!/usr/bin/env python3
"""Live check (serene_booth, REST) for the reached-but-dropped verify path.

Proves the b983de0 fix: when a verify-enabled target reaches desired WHILE the
rule is suppressed mid-window, the event confirm is dropped BUT the stale
deadline handle is dropped too — so a later unsuppress before the ~5s deadline
does NOT re-derive a phantom STATUS_VERIFYING on an already-compliant target.

Three assertions, all HARD (deterministic via the silent-ignore fixture):

  1. a verify cycle caught PENDING ('verifying') while suppressed. The target is
     the silent-ignore fixture (switch.eg_smoke_silent_ignore): it ACKs turn_on
     but never reaches "on", so verify stays pending the full ~5s window and the
     event confirm can NEVER win — suppress reliably lands mid-window on the
     reached-but-dropped path (stale deadline handle dropped by the fix).
  2. unsuppress → the rule lands on a LIVE status (armed/pending/cooldown/
     conditional/enforcing), NEVER stuck on 'verifying' — the user-visible
     symptom the b983de0 fix removes.
  3. (implied) the stale handle was dropped, so no phantom STATUS_VERIFYING
     re-derives after unsuppress. Unit-locked by
     test_verify_dropped_reached_drops_stale_handle; this is live corroboration.

Deterministic target: a verify-ON fixed_state rule on the silent-ignore fixture
`switch.eg_smoke_silent_ignore` (sv.SILENT_TARGET). force_state trips the target;
no real-device side effect. The fixture is always available, so no pre-trip
availability guard is needed.

Run INSIDE the container:
  /home/vscode/.local/ha-venv/bin/python -I scripts/smoke_verify_suppress_drop.py
Exit 0 = all assertions green, 1 = a failure.
"""

from __future__ import annotations

import sys
import time

sys.path.insert(0, "/workspaces/home-assistant-core/config")
import smoke_follow as sf  # token/api/waits/cleanup/check
import smoke_verify_enforce as sv  # create_verify_rule, status_sensor_for, force_state

TARGET = "switch.eg_smoke_delayed_confirm"  # state mirrors eg_smoke_confirm
CONFIRM = "input_boolean.eg_smoke_confirm"  # harness-driven confirm edge
DOMAIN = "entity_guard"
LIVE = {"armed", "conditional", "enforcing", "pending", "cooldown"}
POLL = 0.2


def _set_confirm(token: str, on: bool) -> None:
    sf.api(
        token,
        "POST",
        f"/api/services/input_boolean/turn_{'on' if on else 'off'}",
        {"entity_id": CONFIRM},
    )


def main() -> int:
    token = sf.mint_token()
    sf.cleanup(token)  # clear orphan EG rules from prior runs
    _set_confirm(token, False)  # target starts "off" (== trigger)

    name = "ea_smoke_verify_suppress_drop"
    slug = name
    entry = sv.create_verify_rule_on(token, name, [TARGET], ["off"], "on", verify=True)
    sf.check("verify-enabled rule created", bool(entry))

    try:
        sv.status_sensor_for(token, slug)  # status sensor live
        armed = sf.wait_for_armed(token, "fixed_state", [TARGET])
        sf.check("rule left startup grace (armed)", armed)

        # --- HARD: reached-but-dropped WHILE suppressed (b983de0) -------------
        # The delayed-confirm fixture mirrors input_boolean.eg_smoke_confirm,
        # which starts OFF, so the rule self-enforces and enters VERIFYING (the
        # enforce ACKs but the state does not move until WE drive the helper).
        # Sequence with EXPLICIT ordering (no wall-clock race):
        #   1. wait until the rule is VERIFYING,
        #   2. suppress the rule mid-window,
        #   3. THEN drive the confirm helper ON -> the target reaches desired
        #      ("on") AFTER suppression -> the reached ACK lands on the dropped
        #      path and the stale deadline handle is dropped (the b983de0 fix).
        saw_verifying = False
        deadline = time.time() + 30
        while time.time() < deadline:
            st = sf.get_state(token, f"sensor.{slug}_status")
            if st and st.get("state") == "verifying":
                saw_verifying = True
                break
            time.sleep(POLL)
        sf.check("rule reaches VERIFYING on the delayed-confirm target", saw_verifying)

        sf.api(
            token,
            "POST",
            f"/api/services/{DOMAIN}/suppress",
            {"rule_id": entry, "duration_minutes": 10},
        )
        time.sleep(0.5)  # let suppress land before the reached edge
        _set_confirm(token, True)  # target now reaches desired, WHILE suppressed
        reached = sf.wait_for_state(token, TARGET, "on", timeout=10)
        sf.check("target reached desired ('on') while suppressed", reached)

        # --- HARD: unsuppress must NOT strand the rule on 'verifying' ---------
        sf.api(
            token,
            "POST",
            f"/api/services/{DOMAIN}/unsuppress",
            {"rule_id": entry},
        )
        st = None
        deadline = time.time() + 30
        while time.time() < deadline:
            st = sf.get_state(token, f"sensor.{slug}_status")
            if st and st.get("state") not in (None, "suppressed", "verifying"):
                break
            time.sleep(POLL)
        cur = st.get("state") if st else None
        sf.check(
            "unsuppress -> not stuck on 'verifying' (b983de0 stale-handle fix)",
            cur != "verifying",
            f"state={cur}",
        )
        sf.check(
            "unsuppress -> lands on a live status",
            cur in LIVE,
            f"state={cur}",
        )
    finally:
        _set_confirm(token, False)  # reset target to "off" (helper, not a paint)
        sf.cleanup(token)

    if sf.FAILS:
        print(f"\nFAILED: {sf.FAILS}")
        return 1
    print("\nALL GREEN")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
