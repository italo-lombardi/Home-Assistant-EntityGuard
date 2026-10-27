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
test("_renderTargetOutcomes renders only non-ok targets", () => {
  const ctx = {
    _handleEntityClick() {},
    hass: {
      states: {
        "light.bedroom": { state: "off", attributes: { friendly_name: "Bedroom" } },
        "light.kitchen": { state: "on", attributes: { friendly_name: "Kitchen" } },
      },
    },
  };
  const refs = {
    targetOutcomes: {
      "light.kitchen": { outcome: "ok", reason: null, error: null, at: "t" },
      "light.bedroom": {
        outcome: "failed",
        reason: "service_call_failed",
        error: "device offline",
        at: "t",
      },
    },
  };
  const out = Card.prototype._renderTargetOutcomes.call(ctx, refs);
  // The failed target renders; the ok target does not.
  assert.match(out.__html, /Bedroom/);
  assert.match(out.__html, /device offline/);
  assert.doesNotMatch(out.__html, /Kitchen/);
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
      "light.bedroom": { outcome: "skipped", reason: "light_off", error: null, at: "t" },
    },
  };
  const out = Card.prototype._renderTargetOutcomes.call(ctx, refs);
  // Does not throw; the valid non-ok row renders, bad entries are dropped.
  assert.match(out.__html, /light\.bedroom|skipped/);
});
