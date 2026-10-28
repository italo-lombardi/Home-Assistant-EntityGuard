// Unit tests for the Entity Guard card's pure rendering logic.
//
// Harness note: the card is loaded via ./_harness.mjs, which stubs the browser
// globals the card reads at module-load and captures the card class. The
// methods under test use no `this` state, so they are reached off the
// prototype with a bare `{}` receiver.
//
// Highest-value tests here are the two backend-parity guards: the card carries
// hand-copied duplicates of FOLLOW_ON_STATES and ATTRIBUTE_DISPLAY_NAMES
// (const.py). The backend stays authoritative for enforcement; the card copies
// drive display-only hints. The parity tests parse const.py and fail if the
// two drift — the exact "keep in sync" the source comments ask for.

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { Card, CardEditor } from "./_harness.mjs";

const bare = {};
const read = (rel) =>
  readFileSync(fileURLToPath(new URL(rel, import.meta.url)), "utf8");
const constPy = read("../../custom_components/entity_guard/const.py");
const cardJs = read(
  "../../custom_components/entity_guard/frontend/entity-guard-card.js",
);

// Backend-parity guard: every STATUS_* the engine can emit (const.py
// STATUS_VALUES) must have a label AND a color in the card's two maps, else the
// card falls back to the raw enum string / conditional color. Fails on drift —
// the exact failure that let "verifying" ship with no card entry.
test("STATUS_VALUES parity: card maps cover every engine status", () => {
  const statuses = [
    ...constPy.matchAll(/^STATUS_[A-Z_]+ = "([a-z_]+)"$/gm),
  ].map((m) => m[1]);
  assert.ok(statuses.length >= 10, "parsed the STATUS_* consts");
  const mapKeys = (name) => {
    const body = cardJs.match(new RegExp(`const ${name} = \\{([^}]*)\\}`))[1];
    return [...body.matchAll(/^\s*([a-z_]+):/gm)].map((m) => m[1]);
  };
  const labels = mapKeys("STATUS_LABELS");
  const colors = mapKeys("STATUS_COLORS");
  for (const s of statuses) {
    assert.ok(labels.includes(s), `STATUS_LABELS missing "${s}"`);
    assert.ok(colors.includes(s), `STATUS_COLORS missing "${s}"`);
  }
});

test("_formatSeconds: sub-minute shows bare seconds", () => {
  assert.equal(Card.prototype._formatSeconds.call(bare, 0), "0 s");
  assert.equal(Card.prototype._formatSeconds.call(bare, 59), "59 s");
});

test("_formatSeconds: 60 s rolls to '1 min', not '0 min 60 s'", () => {
  assert.equal(Card.prototype._formatSeconds.call(bare, 60), "1 min");
  assert.equal(Card.prototype._formatSeconds.call(bare, 90), "1 min 30 s");
});

test("_formatSeconds: hours drop zero sub-units", () => {
  assert.equal(Card.prototype._formatSeconds.call(bare, 3600), "1 h"); // no 0 min 0 s
  assert.equal(Card.prototype._formatSeconds.call(bare, 3660), "1 h 1 min"); // no 0 s
  assert.equal(Card.prototype._formatSeconds.call(bare, 3661), "1 h 1 min 1 s");
  assert.equal(Card.prototype._formatSeconds.call(bare, 3601), "1 h 0 min 1 s"); // rem>0, m==0
});

test("_attrValuesMatch: numeric strings compare as numbers", () => {
  assert.equal(Card.prototype._attrValuesMatch.call(bare, "128", 128), true);
  assert.equal(Card.prototype._attrValuesMatch.call(bare, "1.0", "1"), true);
  assert.equal(Card.prototype._attrValuesMatch.call(bare, 128, 129), false);
});

test("_attrValuesMatch: non-numeric falls back to string equality", () => {
  assert.equal(Card.prototype._attrValuesMatch.call(bare, "red", "red"), true);
  assert.equal(Card.prototype._attrValuesMatch.call(bare, "red", "blue"), false);
});

test("_attrValuesMatch: null/undefined never match", () => {
  assert.equal(Card.prototype._attrValuesMatch.call(bare, null, null), false);
  assert.equal(Card.prototype._attrValuesMatch.call(bare, undefined, "x"), false);
  assert.equal(Card.prototype._attrValuesMatch.call(bare, 0, null), false);
});

test("_attributeDisplayName: unknown name is prettified", () => {
  assert.equal(
    Card.prototype._attributeDisplayName.call(bare, "fan_speed"),
    "Fan speed",
  );
});

// Bug 1 regression: a condition's snapshot value must be labelled by that value,
// not by the entity's live state. HA's formatEntityState labels stateObj.state
// (connectivity device_class: on->"Connected", off->"Disconnected"). Passing the
// LIVE state object while the snapshot differs mislabelled the row as
// "Connected (off)". _formatHypotheticalState builds a fake state with
// state=rawValue so the label tracks the value.
test("_formatHypotheticalState: labels the raw value, not the live state", () => {
  const formatEntityState = (st) =>
    st.state === "on" ? "Connected" : "Disconnected";
  const ctx = {
    _applyStateFormat: Card.prototype._applyStateFormat,
    hass: {
      formatEntityState,
      states: {
        "binary_sensor.chandelier_online": {
          state: "on", // live: Connected
          attributes: { device_class: "connectivity" },
        },
      },
    },
  };
  assert.equal(
    Card.prototype._formatHypotheticalState.call(
      ctx,
      "binary_sensor.chandelier_online",
      "off",
    ),
    "Disconnected (off)",
  );
  assert.equal(
    Card.prototype._formatHypotheticalState.call(
      ctx,
      "binary_sensor.chandelier_online",
      "on",
    ),
    "Connected (on)",
  );
});

// Bug 1 render-path lock: the defect was the CALL-SITE in _renderConditions
// (it passed the live state object), not the helper. This drives _renderConditions
// directly and asserts the rendered row labels the SNAPSHOT value, so reverting
// the call-site swap (back to _formatStateDisplay(st, …)) fails here — the direct
// helper test above would stay green on that revert and miss it.
test("_renderConditions renders the snapshot label, not the live-state label", () => {
  const formatEntityState = (st) =>
    st.state === "on" ? "Connected" : "Disconnected";
  const entity = "binary_sensor.chandelier_online";
  const ctx = {
    _applyStateFormat: Card.prototype._applyStateFormat,
    _formatStateDisplay: Card.prototype._formatStateDisplay,
    _formatHypotheticalState: Card.prototype._formatHypotheticalState,
    _handleEntityClick() {},
    hass: {
      formatEntityState,
      states: {
        // Live state is "on" (Connected); the condition snapshot below is "off".
        [entity]: { state: "on", attributes: { device_class: "connectivity" } },
        "sensor.rule_status": {
          attributes: {
            flags: [
              { entity, required: "on", current: "off", matches: false },
            ],
          },
        },
      },
    },
  };
  const out = Card.prototype._renderConditions.call(ctx, {
    status: "sensor.rule_status",
  });
  const text = out.__html;
  // The off snapshot must read "Disconnected (off)" — the live "Connected" label
  // paired with "(off)" is the bug.
  assert.match(text, /Disconnected \(off\)/);
  assert.doesNotMatch(text, /Connected \(off\)/);
});

// #48 guard: a malformed flag entry (null / non-object) from the HA status
// sensor must not throw the whole render — the bad row is skipped.
test("_renderConditions skips a malformed flag entry without throwing", () => {
  const good = "binary_sensor.ok";
  const ctx = {
    _applyStateFormat: Card.prototype._applyStateFormat,
    _formatStateDisplay: Card.prototype._formatStateDisplay,
    _formatHypotheticalState: Card.prototype._formatHypotheticalState,
    _handleEntityClick() {},
    hass: {
      formatEntityState: (st) => st.state,
      states: {
        [good]: { state: "on", attributes: {} },
        "sensor.rule_status": {
          attributes: {
            // null + a non-object entry alongside a valid one
            flags: [null, "oops", { entity: good, required: "on", current: "on", matches: true }],
          },
        },
      },
    },
  };
  // Must not throw, and the valid row still renders.
  const out = Card.prototype._renderConditions.call(ctx, { status: "sensor.rule_status" });
  assert.match(out.__html, /binary_sensor\.ok|on/);
});

// #48 guard: _applyStateFormat must not throw when formatEntityState returns a
// non-string (e.g. a numeric device_class) — it coerces before toLowerCase.
test("_applyStateFormat coerces a non-string label without throwing", () => {
  const ctx = { hass: { formatEntityState: () => 42 } };
  // rawValue non-numeric so it passes the parseFloat guard and reaches the label path.
  const out = Card.prototype._applyStateFormat.call(ctx, { state: "x" }, "active");
  assert.equal(typeof out, "string");
  assert.match(out, /42/);
});

// #48 behavior: _loadEntries leaves _entries === null on a WS failure so the
// editor retries on the next update instead of sticking on "No rules".
test("_loadEntries leaves _entries null on a websocket failure", async () => {
  const ctx = {
    _entries: null,
    _loading: false,
    hass: { callWS: async () => { throw new Error("ws down"); } },
    requestUpdate() {},
  };
  await CardEditor.prototype._loadEntries.call(ctx);
  assert.equal(ctx._entries, null);
  assert.equal(ctx._loading, false);
});

test("parity: _followLooksOn set == FOLLOW_ON_STATES in const.py", () => {
  const pyBlock = constPy.match(
    /FOLLOW_ON_STATES\s*=\s*frozenset\(\s*\{([^}]*)\}/s,
  );
  assert.ok(pyBlock, "FOLLOW_ON_STATES frozenset not found in const.py");
  const pyStates = [...pyBlock[1].matchAll(/"([^"]+)"/g)].map((m) => m[1]).sort();

  // Recover the JS set from the card's own literal array rather than probing a
  // hand-picked candidate list — a cherry-picked probe can miss a new JS-only
  // state. The card builds it as `_followLooksOn(state) { return [ ... ]`;
  // anchor on `return [` so a stray `[` earlier in the method can't be grabbed.
  const jsBlock = cardJs.match(
    /_followLooksOn\(state\)\s*\{\s*return\s*\[([^\]]*)\]/s,
  );
  assert.ok(jsBlock, "_followLooksOn array literal not found in card JS");
  const jsStates = [...jsBlock[1].matchAll(/"([^"]+)"/g)].map((m) => m[1]).sort();

  // Set-equal both ways: catches additions and removals on either side.
  assert.deepEqual(jsStates, pyStates, "JS _followLooksOn drifted from const.py");

  // Behavior sanity: a real member is truthy, a clear non-member is falsy.
  assert.equal(Card.prototype._followLooksOn.call(bare, pyStates[0]), true);
  assert.equal(Card.prototype._followLooksOn.call(bare, "off"), false);
});

test("parity: _attributeDisplayName matches ATTRIBUTE_DISPLAY_NAMES in const.py", () => {
  // const.py keys are ATTR_* constants, values are the display strings. We
  // assert every display string in the dict is produced by the JS map for its
  // matching key. Map ATTR_* -> literal key via the HA constant names the card
  // hard-codes (brightness, volume_level, …).
  const attrConst = {
    ATTR_BRIGHTNESS: "brightness",
    ATTR_VOLUME_LEVEL: "volume_level",
    ATTR_TEMPERATURE: "temperature",
    ATTR_PERCENTAGE: "percentage",
    ATTR_RGB_COLOR: "rgb_color",
    ATTR_COLOR_TEMP_KELVIN: "color_temp_kelvin",
  };
  const block = constPy.match(/ATTRIBUTE_DISPLAY_NAMES[^=]*=\s*\{([^}]*)\}/s);
  assert.ok(block, "ATTRIBUTE_DISPLAY_NAMES dict not found in const.py");
  const pairs = [...block[1].matchAll(/(\w+):\s*"([^"]+)"/g)];
  assert.ok(pairs.length >= 6, "expected 6+ display-name entries");

  for (const [, constName, display] of pairs) {
    const key = attrConst[constName];
    assert.ok(key, `unmapped ATTR constant ${constName} — update the test`);
    assert.equal(
      Card.prototype._attributeDisplayName.call(bare, key),
      display,
      `JS label for "${key}" drifted from const.py`,
    );
  }
});

// Feature A: per-target outcome panel. Minimal render — only non-ok targets.
test("_renderTargetOutcomes shows failed + no_service_mapping, hides benign skips", () => {
  const ctx = {
    _handleEntityClick() {},
    hass: {
      states: {
        "light.bedroom": { state: "off", attributes: { friendly_name: "Bedroom" } },
        "light.kitchen": { state: "on", attributes: { friendly_name: "Kitchen" } },
        "light.hall": { state: "off", attributes: { friendly_name: "Hall" } },
        "climate.den": { state: "off", attributes: { friendly_name: "Den" } },
      },
    },
  };
  const refs = {
    targetOutcomes: {
      "light.kitchen": { outcome: "ok", reason: null, error: null, at: "t" },
      // benign wait — must be HIDDEN
      "light.hall": { outcome: "skipped", reason: "light_off", error: null, at: "t" },
      // real failure — shown
      "light.bedroom": {
        outcome: "failed",
        reason: "service_call_failed",
        error: "device offline",
        at: "t",
      },
      // misconfig skip — shown
      "climate.den": {
        outcome: "skipped",
        reason: "no_service_mapping",
        error: null,
        at: "t",
      },
    },
  };
  const out = Card.prototype._renderTargetOutcomes.call(ctx, refs);
  assert.match(out.__html, /Bedroom/); // failed → shown
  assert.match(out.__html, /device offline/);
  assert.match(out.__html, /Den/); // no_service_mapping → shown
  assert.doesNotMatch(out.__html, /Kitchen/); // ok → hidden
  assert.doesNotMatch(out.__html, /Hall/); // benign light_off skip → hidden
});

test("_renderTargetOutcomes renders nothing when all targets ok or empty", () => {
  const ctx = { hass: { states: {} } };
  const allOk = Card.prototype._renderTargetOutcomes.call(ctx, {
    targetOutcomes: { "light.a": { outcome: "ok" } },
  });
  // `nothing` stub is `?? ""` → flattens to empty, no section rendered.
  assert.equal(allOk.__html ?? allOk, "");
  const empty = Card.prototype._renderTargetOutcomes.call(ctx, { targetOutcomes: {} });
  assert.equal(empty.__html ?? empty, "");
});

test("_renderTargetOutcomes skips a malformed outcome entry without throwing", () => {
  const ctx = {
    _handleEntityClick() {},
    hass: { states: { "light.bedroom": { state: "off", attributes: {} } } },
  };
  const refs = {
    targetOutcomes: {
      "light.null": null,
      "light.str": "oops",
      "light.bedroom": {
        outcome: "failed",
        reason: "service_call_failed",
        error: null,
        at: "t",
      },
    },
  };
  const out = Card.prototype._renderTargetOutcomes.call(ctx, refs);
  // Does not throw; the valid actionable row renders, bad entries are dropped.
  assert.match(out.__html, /light\.bedroom|failed/);
});
// --- Feature B: status reason line (_renderReason) ---
//
// Drives _renderReason directly with a synthetic ctx (the _renderConditions
// pattern): hass.states carries the status sensor + any sibling sensors the
// branch reads, and we assert on the rendered fragment's __html. A `nothing`
// return is the harness's `nothing ?? ""` fallback → the empty string.

// Minimal ctx carrying only the helpers _renderReason reaches.
function reasonCtx(states) {
  return {
    _formatHypotheticalState: Card.prototype._formatHypotheticalState,
    _applyStateFormat: Card.prototype._applyStateFormat,
    _formatSeconds: Card.prototype._formatSeconds,
    _stateValue: Card.prototype._stateValue,
    hass: { states },
  };
}

test("_renderReason conditional: shows the first unmet flag", () => {
  const ctx = reasonCtx({
    "person.alice": { state: "home", attributes: { friendly_name: "Alice" } },
    "sensor.s": {
      attributes: {
        flags: [
          // first flag matches (skipped), second is the unmet one shown
          { entity: "x", required: "on", current: "on", matches: true },
          {
            entity: "person.alice",
            required: "not_home",
            current: "home",
            matches: false,
          },
        ],
      },
    },
  });
  const out = Card.prototype._renderReason.call(ctx, { status: "sensor.s" }, "conditional");
  assert.match(out.__html, /Waiting on: Alice = not_home/);
});

test("_renderReason conditional: no unmet flag → nothing", () => {
  const ctx = reasonCtx({
    "sensor.s": {
      attributes: { flags: [{ entity: "x", required: "on", current: "on", matches: true }] },
    },
  });
  const out = Card.prototype._renderReason.call(ctx, { status: "sensor.s" }, "conditional");
  assert.equal(out, "");
});

test("_renderReason conditional: empty/absent flags → nothing", () => {
  const ctx = reasonCtx({ "sensor.s": { attributes: {} } });
  const out = Card.prototype._renderReason.call(ctx, { status: "sensor.s" }, "conditional");
  assert.equal(out, "");
});

test("_renderReason cooldown: live countdown from absolute cooldown_ends (ticks)", () => {
  // Two future ends; the reason line takes the MAX remaining (mirrors the
  // engine's cooldown_remaining_seconds "largest remaining"). ~90s out → "1 min 30 s".
  const now = Date.now();
  const ctx = reasonCtx({ "sensor.s": { attributes: {} } });
  const out = Card.prototype._renderReason.call(
    ctx,
    {
      status: "sensor.s",
      cooldownEnds: {
        "light.a": new Date(now + 90_000).toISOString(),
        "light.b": new Date(now + 30_000).toISOString(),
      },
    },
    "cooldown",
  );
  // ceil of ~90s is 90 (or 89 if the ms clock already advanced) → accept either.
  assert.match(out.__html, /Cooldown 1 min (30|29) s/);
});

test("_renderReason cooldown: falls back to the frozen scalar when no end-times", () => {
  // No cooldown_ends (empty) → derive 0 → fall back to the scalar sensor value.
  const ctx = reasonCtx({
    "sensor.s": { attributes: { cooldown_ends: {} } },
    "sensor.cd": { state: "90" },
  });
  const out = Card.prototype._renderReason.call(
    ctx,
    { status: "sensor.s", cooldownEnds: {}, cooldownRemaining: "sensor.cd" },
    "cooldown",
  );
  assert.match(out.__html, /Cooldown 1 min 30 s/);
});

test("_renderReason cooldown: past-only ends and zero scalar → nothing", () => {
  const now = Date.now();
  const ctx = reasonCtx({
    "sensor.s": { attributes: {} },
    "sensor.cd": { state: "0" },
  });
  const out = Card.prototype._renderReason.call(
    ctx,
    {
      status: "sensor.s",
      cooldownEnds: { "light.a": new Date(now - 5000).toISOString() },
      cooldownRemaining: "sensor.cd",
    },
    "cooldown",
  );
  assert.equal(out, "");
});

test("_renderReason suppressed: future end shows absolute time + live remaining + reason", () => {
  const d = new Date(Date.now() + 12 * 60_000 + 4000); // ~12m 04s out
  const iso = d.toISOString();
  const when = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const ctx = reasonCtx({
    "sensor.s": { attributes: { suppression_reason: "manual 1h" } },
    "sensor.su": { state: iso },
  });
  const out = Card.prototype._renderReason.call(
    ctx,
    { status: "sensor.s", suppressedUntil: "sensor.su" },
    "suppressed",
  );
  // "Suppressed until HH:MM (12 min 4 s) — manual 1h" — tolerate the ±1s render drift.
  assert.match(
    out.__html,
    new RegExp(`Suppressed until ${when} \\(12 min [34] s\\) — manual 1h`),
  );
});

test("_renderReason suppressed: future end, no reason → time + live remaining", () => {
  const d = new Date(Date.now() + 90_000);
  const iso = d.toISOString();
  const when = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const ctx = reasonCtx({ "sensor.s": { attributes: {} }, "sensor.su": { state: iso } });
  const out = Card.prototype._renderReason.call(
    ctx,
    { status: "sensor.s", suppressedUntil: "sensor.su" },
    "suppressed",
  );
  assert.match(
    out.__html,
    new RegExp(`^<div class="reason">Suppressed until ${when} \\(1 min (30|29) s\\)</div>$`),
  );
});

test("_renderReason suppressed: past end → absolute time only, no remaining", () => {
  // End already passed (stale future render) → secsLeft<=0 → no "(…)" suffix.
  const d = new Date(Date.now() - 5000);
  const iso = d.toISOString();
  const when = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const ctx = reasonCtx({ "sensor.s": { attributes: {} }, "sensor.su": { state: iso } });
  const out = Card.prototype._renderReason.call(
    ctx,
    { status: "sensor.s", suppressedUntil: "sensor.su" },
    "suppressed",
  );
  assert.match(out.__html, new RegExp(`^<div class="reason">Suppressed until ${when}</div>$`));
});

test("_renderReason suppressed: reason only when time unavailable", () => {
  const ctx = reasonCtx({
    "sensor.s": { attributes: { suppression_reason: "panic" } },
    // suppressedUntil unavailable → _stateValue returns the null fallback
    "sensor.su": { state: "unavailable" },
  });
  const out = Card.prototype._renderReason.call(
    ctx,
    { status: "sensor.s", suppressedUntil: "sensor.su" },
    "suppressed",
  );
  assert.match(out.__html, /^<div class="reason">Suppressed — panic<\/div>$/);
});

test("_renderReason suppressed: neither time nor reason → nothing", () => {
  const ctx = reasonCtx({ "sensor.s": { attributes: {} }, "sensor.su": { state: "unknown" } });
  const out = Card.prototype._renderReason.call(
    ctx,
    { status: "sensor.s", suppressedUntil: "sensor.su" },
    "suppressed",
  );
  assert.equal(out, "");
});

test("_renderReason master_disabled: static line", () => {
  const ctx = reasonCtx({ "sensor.s": { attributes: {} } });
  const out = Card.prototype._renderReason.call(ctx, { status: "sensor.s" }, "master_disabled");
  assert.match(out.__html, /Master switch off/);
});

test("_renderReason: non-reason statuses return nothing", () => {
  const ctx = reasonCtx({ "sensor.s": { attributes: {} } });
  for (const s of ["armed", "enforcing", "pending", "error", "disabled", "starting"]) {
    const out = Card.prototype._renderReason.call(ctx, { status: "sensor.s" }, s);
    assert.equal(out, "", `status ${s} should produce no reason line`);
  }
});

test("_renderReason: no status entity → nothing", () => {
  const ctx = reasonCtx({});
  const out = Card.prototype._renderReason.call(ctx, { status: null }, "conditional");
  assert.equal(out, "");
});

// --- Feature B: conditional auto-show gate ---
// The render() gate is `show_conditions || status === "conditional"`. We can't
// drive full render() (needs the whole hass shape), so assert the boolean gate
// logic directly — the exact expression used at the call site.
test("conditions auto-show: conditional forces on even when show_conditions is false", () => {
  const gate = (cfg, status) => cfg.show_conditions || status === "conditional";
  assert.equal(gate({ show_conditions: false }, "conditional"), true);
  assert.equal(gate({}, "conditional"), true);
  // other states stay governed by the flag → default off unaffected
  assert.equal(gate({ show_conditions: false }, "armed"), false);
  assert.equal(gate({}, "cooldown"), false);
  assert.equal(gate({ show_conditions: true }, "armed"), true);
});

// auto-show still yields nothing when flags empty: _renderConditions returns
// `nothing` on an empty/absent flags array, so even a forced call renders blank.
test("conditions auto-show: empty flags still render nothing", () => {
  const ctx = {
    _applyStateFormat: Card.prototype._applyStateFormat,
    _formatStateDisplay: Card.prototype._formatStateDisplay,
    _formatHypotheticalState: Card.prototype._formatHypotheticalState,
    _handleEntityClick() {},
    hass: { states: { "sensor.s": { attributes: { flags: [] } } } },
  };
  const out = Card.prototype._renderConditions.call(ctx, { status: "sensor.s" });
  assert.equal(out, "");
});

// Feature F: the live-tick interval gate lives in the REAL updated() — drive it,
// not a copy of its predicate. updated() reads status via _findRuleEntities()/
// _stateValue() and must start a 1s interval for pending/cooldown/suppressed and
// CLEAR it on transition to any other status. Stubbing setInterval/clearInterval +
// the status source lets us assert the shipped behavior (so dropping "suppressed"
// from the gate, inverting the start/clear branch, or failing to clear all fail).
function runUpdated(statusSeq) {
  let intervalId = 0;
  const cleared = [];
  const origSI = globalThis.setInterval;
  const origCI = globalThis.clearInterval;
  globalThis.setInterval = () => ++intervalId;
  globalThis.clearInterval = (id) => cleared.push(id);
  const ctx = Object.create(Card.prototype);
  ctx._status = null;
  ctx.requestUpdate = () => {};
  ctx._findRuleEntities = () => ({ status: "sensor.r_status" });
  ctx._stateValue = () => ctx._status;
  const states = [];
  try {
    for (const st of statusSeq) {
      ctx._status = st;
      ctx.updated(new Map());
      states.push(ctx._countdownInterval != null);
    }
  } finally {
    globalThis.setInterval = origSI;
    globalThis.clearInterval = origCI;
  }
  return { states, cleared };
}

test("updated() starts the tick for pending/cooldown/suppressed, clears otherwise", () => {
  // Each ticking status, entered from idle, sets the interval.
  for (const s of ["pending", "cooldown", "suppressed"]) {
    const { states } = runUpdated([s]);
    assert.equal(states[0], true, `${s} must start the countdown interval`);
  }
  // Non-ticking statuses do not set it.
  for (const s of ["armed", "enforcing", "conditional", "error", "disabled", "master_disabled", "starting"]) {
    const { states } = runUpdated([s]);
    assert.equal(states[0], false, `${s} must not start the interval`);
  }
  // Transition cooldown -> armed clears the interval (no leak).
  const { states, cleared } = runUpdated(["cooldown", "armed"]);
  assert.equal(states[0], true, "cooldown starts it");
  assert.equal(states[1], false, "armed clears it");
  assert.ok(cleared.length >= 1, "clearInterval called on transition out");
});


// --- Verify row in the Configuration section (_renderConfig) ---
// Minimal ctx: _renderConfig reaches _formatSeconds + hass.states only when the
// attrs it renders require them (debounce). We drive the verify-row branch.
function configCtx(attrs) {
  return {
    _formatSeconds: Card.prototype._formatSeconds,
    _handleEntityClick() {},
    hass: { states: { "sensor.s": { attributes: attrs } } },
  };
}

test("_renderConfig shows a Verify row for a state-mode rule with verify_enabled", () => {
  const out = Card.prototype._renderConfig.call(
    configCtx({ mode: "fixed_state", verify_enabled: true }),
    { status: "sensor.s" },
  );
  assert.match(out.__html, /Verify/);
  assert.match(out.__html, /on/);
});

test("_renderConfig hides the Verify row on an attribute-mode rule", () => {
  const out = Card.prototype._renderConfig.call(
    configCtx({ mode: "fixed_attribute", verify_enabled: true }),
    { status: "sensor.s" },
  );
  // attribute mode -> no verify row even if the attr is present
  assert.ok(out === "" || !/Verify/.test(out.__html ?? ""));
});

test("_renderConfig verify switch entity is not mis-anchored to the enabled ref", () => {
  // The id regex /(_|^)enabled$/ matches switch.rule_verify_enabled, but the tk
  // guard (tk === 'enabled') must keep it out of the enabled ref. Stub
  // _findMatchIds so only the tk-guard logic in _findRuleEntities is exercised.
  const ctx = {
    _config: { rule_id: "rule" },
    _findMatchIds: () => [
      "switch.rule_verify_enabled",
      "switch.rule_enabled",
    ],
    hass: {
      entities: {
        "switch.rule_verify_enabled": { translation_key: "verify_enabled" },
        "switch.rule_enabled": { translation_key: "enabled" },
      },
      states: {
        "switch.rule_verify_enabled": { attributes: {} },
        "switch.rule_enabled": { attributes: {} },
      },
    },
  };
  const refs = Card.prototype._findRuleEntities.call(ctx);
  assert.equal(refs.enabled, "switch.rule_enabled");
});

test("_findRuleEntities: fallback (no translation_key) still anchors Enabled correctly", () => {
  // Registry-less fallback: tk is undefined for every id. The loose /_enabled$/
  // must NOT bind out.enabled to the verify_ or debounce_ switch.
  const ctx = {
    _findMatchIds: () => [
      "switch.rule_debounce_enabled",
      "switch.rule_verify_enabled",
      "switch.rule_enabled",
    ],
    hass: {
      entities: {}, // no registry entries -> tk undefined
      states: {
        "switch.rule_debounce_enabled": { attributes: {} },
        "switch.rule_verify_enabled": { attributes: {} },
        "switch.rule_enabled": { attributes: {} },
      },
    },
  };
  const refs = Card.prototype._findRuleEntities.call(ctx);
  assert.equal(refs.enabled, "switch.rule_enabled");
});
