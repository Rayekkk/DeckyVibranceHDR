// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 Rayekkk
import assert from "node:assert/strict";
import test from "node:test";
import { baseState, loadPlugin, nodes, settle } from "./panel-harness.mjs";

test("an SDR error keeps its switch advice when a hidden HDR half also failed", async t => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  // As in panel-hidden-hdr-advice, but the SDR write failed too: the SDR
  // switch is on screen, so "switch the effect off and on" still applies to it.
  const backend = { ...baseState, reason: "x", sdr_saturation: 1.5, hdr_saturation: 1.3,
    sdr_applied: false, hdr_applied: true, hdr_applied_saturation: 1.3,
    sdr_error: "could not set the SDR atom", hdr_error: "could not restore the previous HDR look",
    hdr_supported: false };
  const { render, close } = await loadPlugin(route => async () =>
    route === "get_version" ? { version: "1.0.0" } : { ...backend });
  try {
    render(); await settle();
    const tree = nodes(render());
    assert.ok(tree.some(n => n.type === "ToggleField"), "the SDR switch is shown");
    const advice = tree.find(n => n.type === "Field" && n.props.label === "What happens now");
    assert.match(advice.props.description, /switch the effect off and on/);
  } finally { close(); }
});
