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
import { Card } from "./_harness.mjs";

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
