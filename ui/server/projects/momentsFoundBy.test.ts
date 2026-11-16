import { describe, expect, it } from "vitest";
import { foundBy } from "./momentsSearch";

// IND403 Lab 2: results never showed which search (keyword or meaning) found each hit, so the lab's point —
// hybrid search combines both — could not be seen. Same rule as the agent's render/moments.found_by.
describe("foundBy", () => {
  it("names the searches a hit's match_sources came from", () => {
    expect(foundBy(["ann:vector", "fts:named_entities"])).toEqual({ found_by: ["meaning", "keyword"] });
    expect(foundBy(["fts:text"])).toEqual({ found_by: ["keyword"] });
    expect(foundBy(["rrf"])).toEqual({});
    expect(foundBy(undefined)).toEqual({});
  });

  // The shape M2C actually sends (captured from staging 2026-11-15): an object of source → score. The list-only
  // first version showed no "Found by" on any tab-search card.
  it("reads the source → score object M2C sends", () => {
    expect(foundBy({ "ann:vector": 0.01166667 })).toEqual({ found_by: ["meaning"] });
    expect(foundBy({ "ann:vector": 0.01, "fts:named_entities": 0.02 })).toEqual({ found_by: ["meaning", "keyword"] });
  });
});
