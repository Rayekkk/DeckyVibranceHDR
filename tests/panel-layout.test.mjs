// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 Rayekkk
import assert from "node:assert/strict";
import test from "node:test";
import { baseState, loadPlugin, nodes, settle } from "./panel-harness.mjs";

test("each section and row appears only when its state asks for it", async t => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  let backend = { ...baseState }, gate = null;
  const { render, close, states } = await loadPlugin(route => async () => {
    if (route === "get_version") return { version: "1.0.0" };
    if (route === "get_state") return { ...backend };
    await new Promise(resolve => { gate = resolve; });
    return { ...backend };
  });
  const show = async state => {
    backend = { ...baseState, ...state }; t.mock.timers.tick(1000); await settle(); return nodes(render());
  };
  const titles = tree => tree.filter(n => n.type === "PanelSection").map(n => n.props.title);
  const labels = tree => tree.filter(n => n.type === "Field").map(n => n.props.label);
  const field = (tree, label) => tree.find(n => n.type === "Field" && n.props.label === label);
  try {
    render(); await settle();
    let tree = await show({});
    assert.deepEqual(titles(tree), ["SDR saturation", "HDR saturation", "Reset"]);
    assert.deepEqual(labels(tree), ["Now", "Now"]);
    assert.deepEqual(tree.filter(n => n.type === "SliderField").map(n => n.props.step), [0.01, 0.01]);

    tree = await show({ hdr_error: "no LUT" });
    assert.deepEqual(labels(tree).slice(0, 2), ["HDR", "What happens now"]);
    assert.equal(field(tree, "What happens now").props.description, "The plugin retries on its own. "
      + "If this stays, switch the effect off and on again, and check that Game Mode is running.");

    tree = await show({ sdr_error: "could not write SDR to gamescope" });
    assert.deepEqual(labels(tree).slice(0, 2), ["SDR", "What happens now"]);

    tree = await show({ settings_error: "could not save settings: disk full" });
    assert.equal(titles(tree)[0], "Connection and settings");
    assert.deepEqual(labels(tree).slice(0, 2), ["Settings not saved", "Now"]);

    tree = await show({ external_display: true });
    assert.deepEqual(titles(tree), ["External display", "Reset"]);
    assert.equal(field(tree, "Standing down").props.description, "Settings here are tuned for the "
      + "built-in screen, so nothing is applied to an external display. Unplug it and your saved settings apply again.");

    tree = await show({ hdr_saturation: 1.3 });           // at the limit is not past it
    assert.match(tree.filter(n => n.type === "SliderField")[1].props.description, /^Raises colour saturation without changing brightness\.$/);

    // Not ready: the settings failure takes the top row only when it is the reason.
    const failure = "could not load settings: denied";
    tree = await show({ ready: false, reason: failure, settings_error: failure, settings_loaded: false });
    assert.deepEqual(labels(tree), ["Could not load settings"]);
    tree = await show({ ready: false, reason: "waiting for gamescope", settings_error: failure,
      settings_loaded: false });
    assert.deepEqual(labels(tree), ["Plugin not ready", "Could not load settings"]);
    assert.equal(field(tree, "Plugin not ready").props.description, "waiting for gamescope");
    tree = await show({ ready: false, reason: "waiting for gamescope", settings_notice: "kept as x" });
    assert.deepEqual(labels(tree), ["Plugin not ready", "Settings reset"]);

    // Reset shows both switches off at once, before the backend answers.
    tree = await show({});
    tree.find(n => n.type === "ButtonItem" && n.props.children === "Back to defaults").props.onClick();
    await settle();
    assert.deepEqual([states[0].sdr_enabled, states[0].hdr_enabled], [false, false]);
  } finally {
    gate?.(); await settle();
    close();
  }
});
