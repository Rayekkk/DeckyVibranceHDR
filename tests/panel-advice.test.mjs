// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 Rayekkk
import assert from "node:assert/strict";
import test from "node:test";
import { baseState, loadPlugin, nodes, settle } from "./panel-harness.mjs";

test("advice under an apply error names only controls that are shown", async t => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const backend = { ...baseState, reason: "could not restore the previous gamescope SDR value",
    hdr_enabled: false, sdr_saturation: 1.5,
    sdr_error: "could not restore the previous gamescope SDR value", external_display: true };
  const { render, close } = await loadPlugin(route => async () => {
    if (route === "get_version") return { version: "1.0.0" };
    return { ...backend };
  });
  try {
    render(); await settle();
    const tree = nodes(render());
    assert.equal(tree.filter(n => n.type === "ToggleField").length, 0, "no switch is shown while docked");
    const advice = tree.find(n => n.type === "Field" && n.props.label === "What happens now");
    // While docked the switches are hidden, so the advice must not ask for them.
    assert.ok(!/switch the effect off and on/.test(advice?.props.description ?? ""), advice?.props.description);
  } finally { close(); }
});
