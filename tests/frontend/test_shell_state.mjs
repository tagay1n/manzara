import test from "node:test";
import assert from "node:assert/strict";
import vm from "node:vm";
import { readFileSync } from "node:fs";

const SOURCE = readFileSync(
  new URL("../../static/shell-state.js", import.meta.url),
  "utf-8",
);

function restoreRailState({ stored = null, desktop = true } = {}) {
  let expanded = null;
  const context = {
    document: {
      documentElement: {
        classList: {
          toggle(_name, value) {
            expanded = value;
          },
        },
      },
    },
    localStorage: {
      getItem() {
        return stored;
      },
    },
    matchMedia() {
      return { matches: desktop };
    },
    window: {},
  };

  vm.runInNewContext(SOURCE, context);
  return {
    expanded,
    state: context.window.ManzaraShellState,
  };
}

test("rail defaults to expanded on desktop and collapsed on compact screens", () => {
  assert.equal(restoreRailState({ desktop: true }).expanded, true);
  assert.equal(restoreRailState({ desktop: false }).expanded, false);
});

test("explicit rail preference wins over viewport default", () => {
  assert.equal(restoreRailState({ stored: "0", desktop: true }).expanded, false);
  assert.equal(restoreRailState({ stored: "1", desktop: false }).expanded, true);
});

test("restored rail state is exposed to the shared shell", () => {
  const restored = restoreRailState({ stored: "1", desktop: true });
  assert.equal(restored.state.initialExpanded, true);
  assert.equal(restored.state.storageKey, "manzara.ui.rail.expanded");
});
