// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 Rayekkk
import assert from "node:assert/strict";
import test from "node:test";
import { baseState, loadPlugin, nodes, settle } from "./panel-harness.mjs";

test("an HDR error with the HDR section hidden does not ask to switch HDR off and on", async t => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  // The panel stopped advertising PQ while an HDR look was applied, and the
  // release that follows failed once (tests/test_controls.py
  // HdrLookStuckAfterPanelLosesPq): hdr_error is set, hdr_supported is false.
  const backend = { ...baseState, reason: "could not restore the previous HDR look",
    sdr_enabled: false, hdr_saturation: 1.3, sdr_applied: false, hdr_applied: true,
    hdr_applied_saturation: 1.3, hdr_error: "could not restore the previous HDR look",
    hdr_supported: false };
  const { render, close } = await loadPlugin(route => async () =>
    route === "get_version" ? { version: "1.0.0" } : { ...backend });
  try {
    render(); await settle();
    const tree = nodes(render());
    assert.deepEqual(tree.filter(n => n.type === "ToggleField").map(n => n.props.label), ["Enabled"],
      "only the SDR switch is shown");
    assert.ok(tree.some(n => n.type === "Field" && n.props.label === "HDR"));
    const advice = tree.find(n => n.type === "Field" && n.props.label === "What happens now");
    assert.ok(!/switch the effect off and on/.test(advice.props.description), advice.props.description);
  } finally { close(); }
});
