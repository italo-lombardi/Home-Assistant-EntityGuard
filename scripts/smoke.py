#!/usr/bin/env python3
"""Entity Guard v0.3.0 smoke tests — run INSIDE the serene_booth container.

Exercises the three v0.3.0 fixes against a live HA, including a real HA restart
so we prove persistence (not just in-process setattr):

  1. Number sliders (delay/debounce/max-enforcements) survive a restart.
  2. Max-enforcements slider accepts 0 (rate limiter disabled) and it persists.
  3. Enable switch state is owned by the Store: disable → restart → still off;
     re-enable → restart → still on (the old options round-trip bug).
  4. A slider write does not reload the entry (no platform teardown).
  5. An options-flow save (rename, and edit_advanced) does NOT revert a slider
     value the number entities persisted to entry.options — live and across a
     restart. This is the v0.3.0 options-flow revert bug.

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


def options_flow_rename(token: str, entry_id: str, new_name: str) -> dict:
    """Drive the options flow (REST): init → pick edit_basics → submit a new name —
    an edit of a field unrelated to the sliders. Proves the options-flow save
    preserves the slider values the number entities persisted to entry.options
    (the revert bug): a pre-fix save wrote entry.data and blanked entry.options,
    reverting the slider. The options flow is a REST-only API in HA (there is no
    websocket command for it), so we drive it over REST like the real frontend."""
    flow = api(
        token, "POST", "/api/config/config_entries/options/flow", {"handler": entry_id}
    )
    flow_id = flow["flow_id"]
    # init is a menu; choosing a menu option is a configure with next_step_id.
    form = api(
        token,
        "POST",
        f"/api/config/config_entries/options/flow/{flow_id}",
        {"next_step_id": "edit_basics"},
    )
    # Seed from the form defaults so we change ONLY the name (one-field edit).
    submit = {s["name"]: s.get("default") for s in form["data_schema"]}
    submit["rule_name"] = new_name
    return api(
        token,
        "POST",
        f"/api/config/config_entries/options/flow/{flow_id}",
        submit,
    )


def options_flow_edit_advanced(
    token: str, entry_id: str, debounce_seconds: int
) -> dict:
    """Drive the options flow: init → pick edit_advanced → submit, editing only
    debounce_seconds. Returns the final flow result. This is the exact path the
    reported bug used ('edit configuration → edit mode-specific settings')."""
    flow = api(
        token, "POST", "/api/config/config_entries/options/flow", {"handler": entry_id}
    )
    flow_id = flow["flow_id"]
    # init is a menu; choosing a menu option is a configure with next_step_id.
    form = api(
        token,
        "POST",
        f"/api/config/config_entries/options/flow/{flow_id}",
        {"next_step_id": "edit_advanced"},
    )
    # Seed the submission from the form's current defaults so we change ONLY
    # debounce_seconds — mirroring a user who edits one field and saves.
    submit = {s["name"]: s.get("default") for s in form["data_schema"]}
    # Guard against a form-generation regression that drops sliders from the
    # schema: without them here the seeded submit would silently omit the
    # values, and a survival check downstream would pass for the wrong reason.
    for key in ("debounce_seconds", "max_enforcements_per_minute"):
        if key not in submit:
            raise RuntimeError(f"edit_advanced form missing slider field: {key}")
    submit["debounce_seconds"] = debounce_seconds
    return api(
        token,
        "POST",
        f"/api/config/config_entries/options/flow/{flow_id}",
        submit,
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
    # A stuck instance would make the relaunch race the old one for the port (the
    # old one wins, answers briefly, then dies) — SIGKILL any survivor first.
    for pid in _hass_pids():
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    time.sleep(2)
    subprocess.Popen(
        [HA_PY, "-m", "homeassistant", "-c", "./config"],
        cwd=CORE,
        stdout=open("/tmp/ha.log", "ab"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )


def wait_ready(timeout: int = 240) -> str:
    """Wait until HA has fully started and stays up. Returns a fresh token.

    A bare "/api/ answers" check is not enough here: this dev container has no
    supervisor, so a relaunch can race a not-yet-dead old instance — the old one
    answers "API running." for a moment, then loses the port and dies. We guard
    against that by requiring the core state to be RUNNING (start-up finished, not
    just the HTTP server bound) and by confirming it twice a few seconds apart, so
    a doomed transient instance can't satisfy the wait."""
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            token = mint_token()
            if api(token, "GET", "/api/").get("message") != "API running.":
                raise OSError("api not running")
            if api(token, "GET", "/api/config").get("state") != "RUNNING":
                raise OSError("core not RUNNING yet")
            time.sleep(3)
            if api(token, "GET", "/api/config").get("state") == "RUNNING":
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

    # --- v0.3.0: options-flow save must NOT revert persisted slider values ----
    # The reported bug: a slider value (delay=123) is persisted to entry.options,
    # then the user opens the options flow and edits an UNRELATED field. Pre-fix,
    # the save wrote entry.data and blanked entry.options, reverting the slider to
    # its creation-time value. Post-fix the flow reads/writes the merged view, so
    # the unedited slider value survives — both live and across a restart.
    slider_before = float(state(token, delay_e)["state"])
    check("delay is 123 before options edit", slider_before == 123.0)

    # --- rename triggers exactly ONE reload (double-reload fix) ------------
    # Pre-fix, async_setup_entry called async_update_entry to sync the device
    # name, re-entering its own update listener -> a second reload per rename.
    # Post-fix the setup-time device-sync block is gone; DeviceInfo(name=title)
    # renames the device on the single rename-triggered reload. Count teardowns
    # scoped to THIS rule around a real rename.
    def rule_unload_count() -> int:
        out = subprocess.run(
            ["grep", "-ac", f"Unloading entry {rule}", "/tmp/ha.log"],
            capture_output=True,
            text=True,
        ).stdout.strip()
        return int(out or 0)

    rename_to = f"Smoke Renamed {int(time.time())}"
    before_rl = rule_unload_count()
    options_flow_rename(token, rule, rename_to)
    time.sleep(3)
    token = wait_entity(token, delay_e)
    after_rl = rule_unload_count()
    check(
        "rename reloads entry exactly once (no double-reload)",
        after_rl - before_rl == 1,
        f"unload lines for rule {before_rl}->{after_rl} (delta {after_rl - before_rl})",
    )
    d_after_edit = float(state(token, delay_e)["state"])
    check(
        "delay survives options-flow save of unrelated field (live)",
        d_after_edit == 123.0,
        f"got {d_after_edit}",
    )
    # Also drive an edit_advanced save (changes debounce only) and confirm the
    # unedited delay slider is untouched.
    options_flow_edit_advanced(token, rule, debounce_seconds=88)
    time.sleep(3)
    token = wait_entity(token, delay_e)
    d_after_adv = float(state(token, delay_e)["state"])
    db_after_adv = float(state(token, debounce_e)["state"])
    check(
        "delay untouched by edit_advanced save",
        d_after_adv == 123.0,
        f"got {d_after_adv}",
    )
    check("edit_advanced applied debounce", db_after_adv == 88.0, f"got {db_after_adv}")
    # And it persists across a restart (options is now the single owner).
    print("Restarting HA (options-flow persistence)…")
    restart_ha(token)
    wait_down()
    token = wait_ready()
    token = wait_entity(token, delay_e)
    check(
        "delay persisted after options edit + restart",
        float(state(token, delay_e)["state"]) == 123.0,
    )
    check(
        "debounce=88 persisted after options edit + restart",
        float(state(token, debounce_e)["state"]) == 88.0,
    )

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
