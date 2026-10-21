// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 Rayekkk
import assert from "node:assert/strict";
import test from "node:test";
import { loadPlugin, nodes, settle } from "./panel-harness.mjs";

test("advice under an apply error names only controls that are shown", async t => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const backend = { ready: true, reason: "could not restore the previous gamescope SDR value",
    sdr_enabled: true, hdr_enabled: false, sdr_saturation: 1.5, hdr_saturation: 1,
    sdr_applied: true, hdr_applied: false, hdr_applied_saturation: null,
    sdr_error: "could not restore the previous gamescope SDR value", sdr_look_error: "", hdr_error: "",
    settings_error: "", settings_notice: "", settings_loaded: true, generating: false, conflict: "",
    external_display: true, sdr_generating: false, hdr_supported: true,
    sdr_min: 1, sdr_max: 3, sdr_soft_limit: 2, hdr_min: 1, hdr_max: 1.5, hdr_soft_limit: 1.3 };
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
