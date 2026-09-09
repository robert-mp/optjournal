import assert from "node:assert/strict";
import test from "node:test";

import {
  chooseMarketDay,
  eventPasses,
  facetsAtDefault,
  seedFacets,
} from "../../src/optjournal/static/market.js";

const FACETS = [
  { value: "USD", default: true },
  { value: "EUR", default: false },
  { value: "All", default: true },
];

test("the payload owns filter defaults", () => {
  assert.deepEqual([...seedFacets(null, FACETS)], ["USD", "All"]);
  const explicit = new Set();
  assert.equal(seedFacets(explicit, FACETS), explicit);
});

test("an empty axis means all values rather than no rows", () => {
  const event = { country: "EUR", impact: "Low" };
  assert.equal(eventPasses(event, new Set(), new Set()), true);
  assert.equal(eventPasses(event, new Set(["USD"]), new Set()), false);
});

test("default comparison is by membership", () => {
  assert.equal(facetsAtDefault(new Set(["All", "USD"]), FACETS), true);
  assert.equal(facetsAtDefault(new Set(["USD"]), FACETS), false);
});

test("a hidden selection heals forward, then to the latest past day", () => {
  const days = ["2026-09-07", "2026-09-10"];
  assert.equal(chooseMarketDay(days, "2026-09-09", "2026-09-08"), "2026-09-10");
  assert.equal(chooseMarketDay(["2026-09-07"], "2026-09-09", null), "2026-09-07");
  assert.equal(chooseMarketDay([], "2026-09-09", null), null);
});
