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
const constPy = readFileSync(
  fileURLToPath(
    new URL("../../custom_components/entity_guard/const.py", import.meta.url),
  ),
  "utf8",
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
  assert.equal(Card.prototype._attributeDisplayName.call(bare, ""), "");
});

test("parity: _followLooksOn matches FOLLOW_ON_STATES in const.py", () => {
  const block = constPy.match(/FOLLOW_ON_STATES\s*=\s*frozenset\(\s*\{([^}]*)\}/s);
  assert.ok(block, "FOLLOW_ON_STATES frozenset not found in const.py");
  const pyStates = [...block[1].matchAll(/"([^"]+)"/g)].map((m) => m[1]).sort();

  // Probe the JS set: every member returns true, a clear non-member false.
  for (const s of pyStates) {
    assert.equal(
      Card.prototype._followLooksOn.call(bare, s),
      true,
      `JS _followLooksOn missing "${s}" present in FOLLOW_ON_STATES`,
    );
  }
  assert.equal(Card.prototype._followLooksOn.call(bare, "off"), false);

  // And the reverse: no JS member absent from Python. Recover the JS list by
  // probing the known universe (= the Python set) plus guards against extras.
  const jsExtras = ["standby", "paused", "closed", "locked"].filter((s) =>
    Card.prototype._followLooksOn.call(bare, s),
  );
  assert.deepEqual(jsExtras, [], "JS _followLooksOn has states not in const.py");
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
