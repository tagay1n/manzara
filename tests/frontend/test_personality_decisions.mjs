import test from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {createHarness} from "./support/page-harness.mjs";

const source = readFileSync(new URL("../../static/library-personalities.js", import.meta.url), "utf8");
const html = readFileSync(new URL("../../static/library-personalities.html", import.meta.url), "utf8");
const ids = [...html.matchAll(/id="([^"]+)"/g)].map(match => match[1]);

function setup(fail = false) {
  return createHarness({source, ids, apiResolver(path, options) {
    if (path === "/api/library/personalities") return {items: [], personality_count: 0, decision_counts: {not_person: 1, unusable: 2, failed: 3, deferred: 4}};
    if (path === "/api/library/personalities/decisions/retry") {
      assert.deepEqual(JSON.parse(options.body), {raw_name: "<script>source</script>", updated_at: "stamp"});
      return {state: "retry_requested"};
    }
    if (path.startsWith("/api/library/personalities/decisions?")) {
      if (fail) throw new Error("review unavailable");
      return {page: 1, has_more: true, items: [
        {raw_name: "<script>source</script>", state: "not_person", reason: "<img>organization", document_count: 2, updated_at: "stamp"},
        {raw_name: "Unknown", state: "unusable", reason: "Ambiguous", document_count: 1, updated_at: "stamp"},
        {raw_name: "Failed", state: "failed", reason: "Invalid JSON", document_count: 1, updated_at: "stamp"},
        {raw_name: "Deferred", state: "deferred", reason: "503", document_count: 1, updated_at: "stamp"},
      ]};
    }
    throw new Error(`Unexpected ${path}`);
  }});
}

test("personality review separates outcomes, escapes source text and offers explicit retry", async () => {
  const harness = setup();
  await harness.flush();
  const body = harness.elements.get("personality-decisions-body").innerHTML;
  for (const label of ["Not a person", "Needs review", "Failed", "Deferred"]) assert.match(body, new RegExp(label));
  assert.match(body, /&lt;script&gt;source&lt;\/script&gt;/);
  assert.match(body, /&lt;img&gt;organization/);
  assert.equal((body.match(/Retry after correction/g) || []).length, 2);
  const button = {dataset: {name: encodeURIComponent("<script>source</script>"), updatedAt: encodeURIComponent("stamp")}};
  harness.elements.get("personality-decisions-body").dispatch("click", {target: {closest: selector => selector === ".personality-decision-retry" ? button : null}});
  await harness.flush();
  assert.ok(harness.apiCalls.some(call => call.path.endsWith("/decisions/retry")));
  harness.elements.get("personality-decisions-next").dispatch("click");
  await harness.flush();
  assert.ok(harness.apiCalls.some(call => call.path.includes("page=2")));
});

test("personality decision review surfaces load errors", async () => {
  const harness = setup(true);
  await harness.flush();
  assert.match(harness.elements.get("personality-decisions-body").innerHTML, /review unavailable/);
});
