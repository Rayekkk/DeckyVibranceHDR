// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 Rayekkk
import assert from "node:assert/strict";
import test from "node:test";
import { loadPlugin, nodes, settle } from "./panel-harness.mjs";

test("a failing first read is shown, and a closed panel stops polling", async t => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  let reads = 0, mode = "fail", release;
  const ready = { ready: false, reason: "starting", settings_loaded: true, settings_error: "" };
  const { render, unmount, close } = await loadPlugin(route => async () => {
    if (route === "get_version") return { version: "1.0.0" };
    if (route !== "get_state") throw new Error(`unexpected ${route}`);
    reads++;
    if (mode === "fail") throw new Error("socket closed");
    if (mode === "hang") { await new Promise(resolve => { release = resolve; }); }
    return { ...ready };
  });
  try {
    // A failing first read says why instead of spinning.
    render(); await settle();
    const field = nodes(render()).find(node => node.type === "Field");
    assert.equal(field?.props.label, "Backend unavailable");
    assert.match(field.props.description, /socket closed/);
    unmount();

    mode = "hang";
    render(); await settle();              // the poll is now waiting on this read
    unmount();                             // the panel closes mid-read
    mode = "ok"; release(); await settle();
    t.mock.timers.tick(500); await settle();  // so a leaked loop would not share the new one's reads
    render(); await settle();              // reopened: one poll loop again
    const before = reads;
    for (let i = 0; i < 10; ++i) { t.mock.timers.tick(500); await settle(); }
    assert.ok(reads - before <= 5, `${reads - before} reads in 5 s`);
  } finally {
    release?.(); await settle();
    close();
  }
});
