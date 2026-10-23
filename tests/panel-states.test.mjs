// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 Rayekkk
import assert from "node:assert/strict";
import test from "node:test";
import { baseState, loadPlugin, nodes, settle } from "./panel-harness.mjs";

test("a slow write after a settings load failure waits, then a reset notice shows", async t => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  let backend = { ...baseState, hdr_enabled: false };
  let reads = 0, writes = 0, release;
  const hang = new Promise(resolve => { release = resolve; });
  const { render, close } = await loadPlugin(route => async (...args) => {
    if (route === "get_state") { reads++; return { ...backend }; }
    if (route === "set_sdr_saturation" && ++writes === 1) {
      // settings failed to load: the first reply says not ready, the next write hangs
      const failure = "could not load settings: [Errno 13] Permission denied";
      backend = { ...backend, ready: false, reason: failure, settings_error: failure,
        settings_loaded: false, sdr_saturation: args[0] };
      return { ...backend };
    }
    await hang;
    return { ...backend };
  });
  const fields = view => view.filter(node => node.type === "Field").map(node => node.props.label);
  const updates = view => view.filter(node => node.type?.name === "UpdateSection").map(node => node.key);
  try {
    render(); await settle();
    const slider = nodes(render()).find(node => node.type === "SliderField");
    slider.props.onChange(1.2); slider.props.onChange(1.4);
    await settle();
    t.mock.timers.tick(10300);  // past the write notice delay
    await settle();
    const tree = nodes(render());
    // The connection notice takes the top row without hiding the settings error.
    assert.deepEqual(fields(tree), ["Waiting for backend", "Could not load settings"]);
    assert.equal(reads, 1);                    // the write still blocks every read
    // One key in both layouts, so switching between them keeps the updater's focus.
    assert.deepEqual(updates(tree), ["updates"]);
    backend = { ...backend, ready: true, reason: "ready", settings_error: "", settings_loaded: true,
      settings_notice: "The old file is kept as settings.json.corrupt-1." };
    release(); await settle();
    t.mock.timers.tick(1000); await settle();  // the next poll reads
    const ready = nodes(render());
    assert.deepEqual(fields(ready).slice(0, 1), ["Settings reset"]);
    assert.deepEqual(updates(ready), ["updates"]);
  } finally {
    release(); await settle();
    close();
  }
});
