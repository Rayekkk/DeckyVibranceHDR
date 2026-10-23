// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 Rayekkk
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { baseState } from "./panel-harness.mjs";

// The panel tests all start from baseState; this keeps it the shape get_state returns.
test("baseState has exactly the keys of the backend's public state", () => {
  const source = readFileSync(new URL("../vibrance_state.py", import.meta.url), "utf8");
  const block = source.split("self.public = {\n")[1].split(/^        \}/m)[0];
  const keys = [...block.matchAll(/^            "(\w+)":/gm)].map(match => match[1]);
  assert.ok(keys.length > 0, "no keys found in PluginState.reset");
  assert.deepEqual(Object.keys(baseState).sort(), keys.sort());
});
