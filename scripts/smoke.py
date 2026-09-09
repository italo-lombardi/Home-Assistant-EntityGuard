#!/usr/bin/env python3
"""Entity Guard v0.3.0 smoke tests — run INSIDE the serene_booth container.

Exercises the three v0.3.0 fixes against a live HA, including a real HA restart
so we prove persistence (not just in-process setattr):

  1. Number sliders (delay/debounce/max-enforcements) survive a restart.
  2. Max-enforcements slider accepts 0 (rate limiter disabled) and it persists.
  3. Enable switch state is owned by the Store: disable → restart → still off;
     re-enable → restart → still on (the old options round-trip bug).

Usage: python3 smoke.py <rule_entry_id>
Exit 0 = all green, 1 = a failure.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE = "http://localhost:8123"
AUTH_STORE = "/workspaces/home-assistant-core/config/.storage/auth"
CORE = "/workspaces/home-assistant-core"
HA_PY = "/home/vscode/.local/ha-venv/bin/python3"
# Store save is debounced 10s (STORE_SAVE_DELAY_SECONDS); wait past it so the
# blob is on disk regardless of shutdown-flush timing.
SAVE_FLUSH = 13
# Mirror number.py's _NUMBER_WRITE_DEBOUNCE_SECONDS.
_NUMBER_WRITE_DEBOUNCE = 2


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


def state(token: str, entity_id: str) -> dict:
    return api(token, "GET", f"/api/states/{entity_id}")


def set_number(token: str, entity_id: str, value: float) -> None:
    api(
        token,
        "POST",
        "/api/services/number/set_value",
        {"entity_id": entity_id, "value": value},
    )


def switch(token: str, entity_id: str, on: bool) -> None:
    api(
        token,
        "POST",
        f"/api/services/switch/turn_{'on' if on else 'off'}",
        {"entity_id": entity_id},
    )


def _hass_pids() -> list[int]:
    """PIDs of the running HA process(es). This dev container launches HA as
    `python -m homeassistant -c ./config` (NOT the `hass` console script), so
    match the module invocation."""
    out = subprocess.run(
        ["pgrep", "-f", "homeassistant -c"], capture_output=True, text=True
    ).stdout
    return [int(p) for p in out.split() if p.strip()]


def restart_ha(token: str) -> None:
    """Restart HA. The dev container has no supervisor, so we do it ourselves:
    stop the running instance and relaunch it detached (survives this script)."""
    for pid in _hass_pids():
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    # Wait for the process to fully exit (frees port 8123) before relaunching —
    # HA refuses to start with "Another Home Assistant instance is already running".
    deadline = time.time() + 60
    while time.time() < deadline and _hass_pids():
        time.sleep(1)
    subprocess.Popen(
        [HA_PY, "-m", "homeassistant", "-c", "./config"],
        cwd=CORE,
        stdout=open("/tmp/ha.log", "ab"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )


def wait_ready(timeout: int = 240) -> str:
    """Wait until the API answers authed. Returns a fresh token."""
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            token = mint_token()
            r = api(token, "GET", "/api/")
            if r.get("message") == "API running.":
                return token
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
            last = str(e)
        time.sleep(3)
    raise TimeoutError(f"HA API not ready in {timeout}s (last: {last})")


def wait_down(timeout: int = 60) -> None:
    """Wait until the API stops answering (restart has begun)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            api(mint_token(), "GET", "/api/")
        except (urllib.error.URLError, urllib.error.HTTPError, OSError):
            return
        time.sleep(1)
    # Didn't observe a down window; the restart may have been very fast. Proceed.


def wait_entity(token: str, entity_id: str, timeout: int = 60) -> str:
    """Wait for an entity to leave 'unavailable'. Returns (maybe fresh) token."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            token = mint_token()
            s = state(token, entity_id)
            if s.get("state") not in (None, "unavailable", "unknown"):
                return token
        except (urllib.error.URLError, urllib.error.HTTPError, OSError):
            pass
        time.sleep(2)
    return token


FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: smoke.py <rule_entry_id>")
        return 2
    rule = sys.argv[1]
    # Restart first so the freshly-deployed integration build is live before we
    # touch anything (the previously-running HA may be an older version).
    print("Restarting HA to load the deployed build…")
    token = mint_token()
    restart_ha(token)
    wait_down()
    token = wait_ready()

    # Resolve this rule's entity_ids from the registry by config_entry_id —
    # unambiguous even with many rules sharing entity-name suffixes.
    reg = json.load(
        open("/workspaces/home-assistant-core/config/.storage/core.entity_registry")
    )["data"]["entities"]

    def by_unique_suffix(suffix: str) -> str:
        want = f"{rule}{suffix}"
        for e in reg:
            if e.get("config_entry_id") == rule and e["unique_id"] == want:
                return e["entity_id"]
        return ""

    delay_e = by_unique_suffix("_delay_seconds")
    debounce_e = by_unique_suffix("_debounce_seconds")
    rate_e = by_unique_suffix("_max_enforcements_per_minute")
    enabled_e = by_unique_suffix("_enabled")

    print(
        f"Resolved entities:\n  delay={delay_e}\n  debounce={debounce_e}\n"
        f"  rate={rate_e}\n  enabled={enabled_e}"
    )
    if not all([delay_e, debounce_e, rate_e, enabled_e]):
        print("Could not resolve all rule entities; is the rule loaded?")
        return 2

    # --- Set distinctive values pre-restart -------------------------------
    set_number(token, delay_e, 123)
    set_number(token, debounce_e, 77)
    set_number(token, rate_e, 0)  # 0 = disable rate limiting (v0.3.0)
    switch(token, enabled_e, False)  # disable via Store-backed switch
    time.sleep(SAVE_FLUSH)  # past the number 2s + Store 10s debounces

    check("rate slider accepts 0 (live)", float(state(token, rate_e)["state"]) == 0.0)
    check("enabled switch off (live)", state(token, enabled_e)["state"] == "off")

    # --- v0.3.1: a slider write must NOT reload the entry -------------------
    # The value applies live; persisting to options is debounced but the update
    # listener skips the reload. Prove it: count "Unloading entry" log lines
    # around a slider nudge — a reload would emit one.
    def unload_count() -> int:
        out = subprocess.run(
            ["grep", "-ac", "Unloading entry", "/tmp/ha.log"],
            capture_output=True,
            text=True,
        ).stdout.strip()
        return int(out or 0)

    before = unload_count()
    set_number(token, delay_e, 111)
    time.sleep(_NUMBER_WRITE_DEBOUNCE + 3)  # let the debounced flush fire
    after = unload_count()
    check(
        "slider write does not reload entry (no teardown)",
        after == before,
        f"unload lines {before}->{after}",
    )
    # Restore the value we assert survives the restart (111 -> 123 is a real
    # change, so this write is not skipped as a no-op).
    set_number(token, delay_e, 123)
    time.sleep(SAVE_FLUSH)

    # --- Restart HA -------------------------------------------------------
    print("Restarting HA…")
    restart_ha(token)
    wait_down()
    token = wait_ready()
    token = wait_entity(token, delay_e)

    # --- Assert persistence ----------------------------------------------
    d = float(state(token, delay_e)["state"])
    db = float(state(token, debounce_e)["state"])
    rt = float(state(token, rate_e)["state"])
    en = state(token, enabled_e)["state"]
    check("delay persisted", d == 123.0, f"got {d}")
    check("debounce persisted", db == 77.0, f"got {db}")
    check("rate=0 persisted (not clamped to 1)", rt == 0.0, f"got {rt}")
    check("enabled=off persisted via Store", en == "off", f"got {en}")

    # --- Re-enable, restart, assert it STAYS on (old round-trip bug) ------
    switch(token, enabled_e, True)
    time.sleep(SAVE_FLUSH)  # enabled=True is Store-backed with a 10s debounce
    print("Restarting HA again (re-enable round-trip)…")
    restart_ha(token)
    wait_down()
    token = wait_ready()
    token = wait_entity(token, enabled_e)
    en2 = state(token, enabled_e)["state"]
    check("re-enabled stays on after restart", en2 == "on", f"got {en2}")

    # --- Restore friendly defaults so the rule is usable ------------------
    set_number(token, delay_e, 5)
    set_number(token, debounce_e, 30)
    set_number(token, rate_e, 10)
    time.sleep(3)

    print()
    if FAILS:
        print(f"SMOKE RESULT: {len(FAILS)} FAILED — {', '.join(FAILS)}")
        return 1
    print("SMOKE RESULT: ALL GREEN")
    return 0


if __name__ == "__main__":
    sys.exit(main())
