#!/usr/bin/env python3
"""Smoke test: enforce bookkeeping survives to disk (fix/enforce-cancel-race).

THE LIVE BUG (v0.4.0): a rule's target switches flipped correctly, but the
success tail never ran — `enforcements_today/total` stuck, `last_enforced`
days old, no EVENT_ENFORCED, `recently_enforced` never pulsed. Root cause was
self-inflicted task cancellation: a battery/RF switch ACKs seconds after the
blocking service call starts, so the target's OWN post-enforce state_changed
re-entered the per-entity cancel-and-replace (_schedule_eval_task) and killed
the still-awaiting enforce task mid-await. CancelledError (a BaseException)
slipped past `except Exception`, so the whole bookkeeping tail was lost while
the physical switch stayed flipped. Fix = shield the service call, swallow the
frame cancel, commit the tail, then re-raise.

SCOPE / HONESTY GUARD (do not remove):
  The self-cancel RACE itself is not forceable on a fast smoke target — the
  race window is the battery-switch ACK latency, and an allowed smoke target
  ACKs near-instant, so the cancel almost never lands inside the await here.
  The race regression is therefore UNIT-LOCKED by
  `test_enforce_tail_survives_cancel_during_service_call` (tests/test_rule_engine.py),
  which deterministically cancels the enforce task during the shielded call.

  What THIS smoke guards is the user-visible INVARIANT the bug broke: after a
  real enforce of a real target, the bookkeeping actually advances AND persists
  across a restart — counters up, `last_enforced` moved, EVENT_ENFORCED fired.
  A regression that drops the tail (by any route, not just the cancel race)
  fails here because the counters would not move / not survive the restart.

Reuses smoke_follow.py (token/api/state/create_fixed_state_rule/cleanup) and
smoke.py (restart_ha, status attribute helpers) — does not modify either.
Run inside the serene_booth container:
  docker cp scripts/smoke_enforce_cancel_bookkeeping.py \
    serene_booth:/workspaces/home-assistant-core/config/smoke_enforce_cancel_bookkeeping.py
  docker exec serene_booth python3 \
    /workspaces/home-assistant-core/config/smoke_enforce_cancel_bookkeeping.py
Exit 0 = all hard assertions green, 1 = a failure.
"""

from __future__ import annotations

import importlib.util
import sys
import time
from datetime import datetime

_CFG = "/workspaces/home-assistant-core/config"
sys.path.insert(0, _CFG)


def _load(name: str):
    # Load by explicit file path: a sibling `smoke/` test package on the HA
    # config dir shadows smoke.py under a plain `import smoke`.
    spec = importlib.util.spec_from_file_location(f"eg_{name}", f"{_CFG}/{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sf = _load("smoke_follow")  # token/api/state/create_fixed_state_rule/cleanup
sm = _load("smoke")  # reuse restart_ha + state-attr helpers

TARGET = sf.TARGET  # allowed smoke target (memory: smoke_test_entities)
SAVE_FLUSH = 13  # STORE_SAVE_DELAY_SECONDS(10) + margin; mirror smoke.py


def _sensor(slug: str, suffix: str) -> str:
    return f"sensor.{slug}_{suffix}"


def _int_state(token: str, eid: str) -> int | None:
    st = sf.get_state(token, eid)
    try:
        return int(float(st.get("state")))
    except (TypeError, ValueError):
        return None


def main() -> int:
    token = sf.mint_token()
    sf.cleanup(token)

    name = "ea_smoke_enforce_bookkeeping"
    slug = name
    total_eid = _sensor(slug, "enforcements_total")
    today_eid = _sensor(slug, "enforcements_today")
    last_eid = _sensor(slug, "last_enforced")
    recent_eid = f"binary_sensor.{slug}_recently_enforced"

    try:
        # off->on fixed_state rule on the allowed target.
        entry = sf.create_fixed_state_rule(
            token, name, [TARGET], target_state="on", trigger_states=["off"]
        )
        sf.check("fixed_state rule created", bool(entry))

        armed = sf.wait_for_armed(token, "fixed_state", [TARGET])
        sf.check("rule left startup grace (armed)", armed)

        # Precondition: target reachable, else its enforce never ACKs and the
        # counters legitimately stay put — a false FAIL unrelated to the tail.
        pre = sf.get_state(token, TARGET)
        pre_ok = pre and pre.get("state") not in (None, "unavailable", "unknown")
        sf.check(
            "target available before trip",
            pre_ok,
            f"target state={pre.get('state') if pre else None}",
        )
        if not pre_ok:
            return 1

        before_total = _int_state(token, total_eid) or 0
        before_today = _int_state(token, today_eid) or 0
        before_last = sf.get_state(token, last_eid).get("state")

        # Trip the rule: fake the target reading "off" so the engine enforces
        # "on" through the REAL service against the REAL device -> ACK ->
        # success tail must run.
        sf.force_state(token, TARGET, "off") if hasattr(
            sf, "force_state"
        ) else sf.set_state(token, TARGET, "off")

        # Wait for the counters to advance (tail ran in-process).
        deadline = time.time() + 25
        moved = False
        while time.time() < deadline:
            now_total = _int_state(token, total_eid)
            if now_total is not None and now_total > before_total:
                moved = True
                break
            time.sleep(0.3)
        sf.check(
            "enforcements_total advanced after enforce (tail ran)",
            moved,
            f"before={before_total} now={_int_state(token, total_eid)}",
        )

        after_today = _int_state(token, today_eid)
        sf.check(
            "enforcements_today advanced",
            after_today is not None and after_today > before_today,
            f"before={before_today} now={after_today}",
        )

        # last_enforced is the stamp the live bug froze (stuck days old). Two
        # enforces inside one clock-second share an ISO string, so != is too
        # tight — the real invariant is "fresh", not "different". Assert it
        # parses to a time within the last couple minutes.
        after_last = sf.get_state(token, last_eid).get("state")
        fresh = False
        try:
            dt = datetime.fromisoformat(after_last)
            age = (datetime.now(dt.tzinfo) - dt).total_seconds()
            fresh = 0 <= age <= 120
        except (TypeError, ValueError):
            fresh = False
        sf.check(
            "last_enforced is fresh (not stuck days old)",
            fresh,
            f"before={before_last} now={after_last}",
        )

        # recently_enforced is the pulse the live bug never fired. It is a
        # short one-shot; poll a brief window for its ON edge.
        pulsed = sf.wait_for_state(token, recent_eid, "on", timeout=10)
        sf.check("recently_enforced pulsed ON", pulsed)

        # Persistence: the bug's reload fingerprint was 'Restored total=4
        # today=0' — tail never hit disk. Snapshot, restart, assert it survives.
        committed_total = _int_state(token, total_eid)
        time.sleep(SAVE_FLUSH)  # let the debounced Store flush
        sm.restart_ha(token)
        # Re-mint: tokens are short-lived across a restart.
        token = sf.mint_token()
        restored_total = None
        rdl = time.time() + 60
        while time.time() < rdl:
            restored_total = _int_state(token, total_eid)
            if restored_total is not None:
                break
            time.sleep(0.5)
        sf.check(
            "enforcements_total persisted across restart",
            restored_total == committed_total,
            f"committed={committed_total} restored={restored_total}",
        )
    finally:
        sf.cleanup(token)

    if sf.FAILS:
        print(f"\nFAILED: {sf.FAILS}")
        return 1
    print("\nALL GREEN")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
