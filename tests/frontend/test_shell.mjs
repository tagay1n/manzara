import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const PAGE_FILES = [
  "tasks.html",
  "task.html",
  "flow.html",
  "database.html",
  "library.html",
  "library-classifications.html",
  "library-classification.html",
  "library-personalities.html",
  "library-publishers.html",
  "library-collections.html",
  "library-normalization.html",
  "gemini.html",
];

const STYLES_SOURCE = readFileSync(
  new URL("../../static/styles.css", import.meta.url),
  "utf-8",
);

test("conveyor lays sequential steps left-to-right and parallel tasks vertically", () => {
  assert.match(
    STYLES_SOURCE,
    /\.conveyor-stages\s*\{[^}]*display:\s*flex;[^}]*overflow-x:\s*auto;/s,
  );
  assert.match(
    STYLES_SOURCE,
    /\.conveyor-stage-items\s*\{[^}]*grid-template-columns:\s*minmax\(0,\s*1fr\);/s,
  );
});

test("all application pages use the shared shell and omit permanent alert strips", () => {
  for (const file of PAGE_FILES) {
    const source = readFileSync(new URL(`../../static/${file}`, import.meta.url), "utf-8");
    assert.match(source, /data-manzara-page=/, `${file} declares its active page`);
    assert.match(source, /\/static\/shell\.js/, `${file} loads the shared shell`);
    assert.match(source, /\/static\/shell-state\.js/, `${file} restores shell state before paint`);
    assert.ok(
      source.indexOf("/static/shell-state.js") < source.indexOf("/static/styles.css"),
      `${file} restores shell state before loading styles`,
    );
    assert.doesNotMatch(source, /class="alert-strip"/, `${file} has no permanent alert strip`);
    assert.doesNotMatch(
      source,
      /Task index is grouped by flow/,
      `${file} has no oversized task-index guidance panel`,
    );
    assert.doesNotMatch(
      source,
      /class="(?:app-shell|side-rail|topbar|footer)"/,
      `${file} does not duplicate shared shell markup`,
    );
  }
});

test("obsolete dashboard asset redirects to the task index without legacy UI", () => {
  const source = readFileSync(
    new URL("../../static/dashboard.html", import.meta.url),
    "utf-8",
  );

  assert.match(source, /location\.replace\("\/tasks"\)/);
  assert.doesNotMatch(source, /class="(?:app-shell|alert-strip|dashboard)"/);
  assert.doesNotMatch(source, /\/static\/app\.js/);
});

test("frontend source does not use browser system dialogs", () => {
  const sources = [
    "app.js",
    "database.js",
    "flow.js",
    "gemini.js",
    "library-classification.js",
    "library-classifications.js",
    "library-collections.js",
    "library-normalization.js",
    "library-personalities.js",
    "library-publishers.js",
    "task.js",
    "tasks.js",
  ].map((file) => readFileSync(new URL(`../../static/${file}`, import.meta.url), "utf-8"));

  const combined = sources.join("\n");
  assert.doesNotMatch(combined, /\bwindow\.(?:alert|confirm|prompt)\s*\(/);
  assert.doesNotMatch(combined, /(?:^|[^\w.])(?:alert|confirm|prompt)\s*\(/m);
});

test("shared shell has no legacy schedules navigation", () => {
  const source = readFileSync(new URL("../../static/shell.js", import.meta.url), "utf-8");
  assert.doesNotMatch(source, /href:\s*["']\/schedules["']/);
  assert.doesNotMatch(source, /title:\s*["']Schedules["']/);
});

test("shared shell has no command palette", () => {
  const source = readFileSync(new URL("../../static/shell.js", import.meta.url), "utf-8");
  assert.doesNotMatch(source, /command-trigger|command-dialog|command-input/);
  assert.doesNotMatch(source, /ctrlKey|metaKey|Jump to page/);
});
