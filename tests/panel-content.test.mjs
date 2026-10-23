// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 Rayekkk
import assert from "node:assert/strict";
import test from "node:test";
import { baseState, loadPlugin, nodes, settle } from "./panel-harness.mjs";

test("sections, labels and the HDR and reset controls follow the backend", async t => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  let backend = { ...baseState }, failRead = false;
  const calls = [];
  let gate = null;
  const hold = () => new Promise(resolve => { gate = resolve; });
  const { render, close, states } = await loadPlugin(route => async (...args) => {
    if (route === "get_version") return { version: "1.0.0" };
    if (route === "get_state") { if (failRead) throw new Error("socket closed"); return { ...backend }; }
    calls.push([route, ...args]);
    if (calls.length === 1) await hold();       // the first write stalls, so later ones queue
    if (route === "set_hdr_saturation") backend.hdr_saturation = args[0];
    if (route === "set_hdr_enabled") { backend.hdr_enabled = args[0]; backend.hdr_saturation = args[1]; }
    if (route === "set_sdr_saturation") backend.sdr_saturation = args[0];
    if (route === "set_sdr_enabled") { backend.sdr_enabled = args[0]; backend.sdr_saturation = args[1]; }
    if (route === "reset") backend = { ...backend, sdr_enabled: false, hdr_enabled: false,
      sdr_saturation: 1, hdr_saturation: 1 };
    return { ...backend };
  });
  const poll = async () => { t.mock.timers.tick(1000); await settle(); };
  const show = async state => { backend = { ...baseState, ...state }; await poll(); return nodes(render()); };
  const titles = tree => tree.filter(n => n.type === "PanelSection").map(n => n.props.title);
  const field = (tree, label) => tree.filter(n => n.type === "Field" && n.props.label === label);
  try {
    render(); await settle();

    let tree = await show({ conflict: "vibrantDeck", sdr_look_error: "no LUT" });
    assert.ok(titles(tree).includes("Heads up"));
    assert.equal(field(tree, "Conflict")[0]?.props.description, "vibrantDeck");
    assert.equal(field(tree, "SDR look")[0]?.props.description, "no LUT");

    tree = await show({ external_display: true });
    assert.ok(titles(tree).includes("External display"));
    assert.ok(!titles(tree).includes("SDR saturation") && !titles(tree).includes("HDR saturation"));
    tree = await show({ hdr_supported: false });
    assert.ok(titles(tree).includes("SDR saturation") && !titles(tree).includes("HDR saturation"));

    // At the limit is not past it; a build on one half is not shown on the other.
    tree = await show({ sdr_saturation: 2, generating: true });
    const sliders = tree.filter(n => n.type === "SliderField");
    assert.match(sliders[0].props.description, /^100% is neutral/);
    const nows = field(tree, "Now");
    assert.equal(nows[0].props.description, undefined);
    assert.equal(nows[1].props.description, "Preparing the colours...");
    tree = await show({});
    assert.equal(field(tree, "Now")[1].props.description, "100% is neutral, nothing to apply");
    tree = await show({ sdr_saturation: 2.5, hdr_saturation: 1.4 });
    const [sdrSlider, hdrSlider] = tree.filter(n => n.type === "SliderField");
    assert.match(sdrSlider.props.description, /^Past 200% the strongest colours start clipping and lose detail\.$/);
    assert.match(hdrSlider.props.description, /^Past 130% the strongest colours start clipping and lose detail\.$/);
    assert.deepEqual([sdrSlider.props.max, hdrSlider.props.max], [3, 1.5]);
    tree = await show({ sdr_enabled: false, hdr_enabled: false });
    assert.equal(tree.filter(n => n.type === "SliderField").length, 0);
    tree = await show({ ready: false, reason: "waiting for gamescope" });
    assert.ok(tree.some(n => typeof n.type === "function" && n.type.name === "UpdateSection"),
      "updates stay reachable while the backend is not ready");
    failRead = true; await poll(); failRead = false;
    assert.deepEqual(field(nodes(render()), "Backend unavailable").map(n => n.props.description),
      ["Error: socket closed"]);
    tree = await show({});

    // Ready, then the backend drops: the notice says which.
    failRead = true; await poll(); failRead = false;
    tree = nodes(render());
    assert.equal(field(tree, "Backend unavailable")[0]?.props.description, "Error: socket closed");
    await poll();

    // HDR controls: edits show at once, a toggle sends the newest slider value
    // and drops queued slider edits, and repeated toggles collapse.
    let hdr = nodes(render()).filter(n => n.type === "SliderField")[1];
    let hdrToggle = nodes(render()).filter(n => n.type === "ToggleField")[1];
    const pending = [hdr.props.onChange(1.1), hdr.props.onChange(1.4)];
    await settle();
    assert.equal(field(nodes(render()), "Now")[1].props.children, "140%");
    pending.push(hdrToggle.props.onChange(false));
    for (let n = 0; n < 20; ++n) pending.push(hdrToggle.props.onChange(n % 2 === 0));
    gate(); await Promise.all(pending); await settle();
    assert.deepEqual(calls, [["set_hdr_saturation", 1.1], ["set_hdr_enabled", false, 1.4]]);

    // Reset drops queued slider edits and toggles and shows defaults at once.
    calls.length = 0;
    backend = { ...baseState, hdr_saturation: 1.2 }; await poll();
    const controls = nodes(render());
    const sdr = controls.filter(n => n.type === "SliderField")[0];
    const sdrToggle = controls.filter(n => n.type === "ToggleField")[0];
    const reset = controls.find(n => n.type === "ButtonItem" && n.props.children === "Back to defaults");
    const queued = [sdr.props.onChange(1.2), sdr.props.onChange(1.6), sdrToggle.props.onChange(false)];
    hdrToggle = controls.filter(n => n.type === "ToggleField")[1];
    queued.push(hdrToggle.props.onChange(false));
    queued.push(reset.props.onClick());
    await settle();
    assert.equal(states[0].hdr_saturation, 1);
    assert.equal(states[0].sdr_saturation, 1);
    gate(); await Promise.all(queued); await settle();
    assert.deepEqual(calls, [["set_sdr_saturation", 1.2], ["reset"]]);
  } finally {
    gate?.(); await settle();
    close();
  }
});
