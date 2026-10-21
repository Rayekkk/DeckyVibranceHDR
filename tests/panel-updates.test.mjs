// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 Rayekkk
import assert from "node:assert/strict";
import test from "node:test";
import { loadPlugin, nodes, settle } from "./panel-harness.mjs";

test("download replies that carry no file say so, and only toast while open", async t => {
  // The not-ready panel this reaches the section through polls; keep its timers fake.
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const calls = [], toasts = [];
  let reply = null, release;
  const notReady = { ready: false, reason: "starting", settings_loaded: true, settings_error: "" };
  const { render, unmount, close, states } = await loadPlugin(route => async () => {
    calls.push(route);
    if (route === "get_state") return { ...notReady };
    if (route === "get_version") return { version: "1.0.0" };
    if (route === "check_for_updates") return { current_version: "1.0.0", latest_version: "2.0.0",
      update_available: true, download_url: "https://example.invalid/a.zip", asset_name: "a.zip" };
    if (route === "perform_update") { await new Promise(resolve => { release = resolve; }); return reply; }
    throw new Error(`unexpected ${route}`);
  }, { toast(message) { toasts.push(message); } });
  let UpdateSection;
  const section = () => render(UpdateSection);
  const mount = async () => { unmount(); section(); await settle(); };
  const button = text => nodes(section()).find(node => node.props?.children === text);
  const labels = () => nodes(section()).map(node => node.props?.label);
  const error = () => nodes(section()).find(node => node.props?.label === "Error")?.props.description;
  try {
    // Reach UpdateSection through the not-ready panel, which renders it as a child element.
    states[0] = notReady;                 // Content's first useState: the backend state
    UpdateSection = nodes(render()).find(node => typeof node.type === "function"
      && node.type.name === "UpdateSection").type;
    await mount();

    await button("Check for updates").props.onClick(); await settle();
    const downloading = button("Download v2.0.0").props.onClick(); await settle();
    // A check while a download runs would replace the offer under it.
    const before = calls.filter(call => call === "check_for_updates").length;
    assert.ok(button("Downloading..."));
    assert.equal(button("Check for updates").props.disabled, true);
    await button("Check for updates").props.onClick(); await settle();
    assert.equal(calls.filter(call => call === "check_for_updates").length, before);

    // An unsuccessful reply (no exception) is shown, with a toast while open.
    reply = { success: false, error: "disk full" };
    release(); await downloading; await settle();
    assert.ok(labels().includes("Error"));
    assert.equal(error(), "disk full");
    assert.equal(toasts.length, 1);

    // Success without a path is not a finished download.
    let pending = button("Download v2.0.0").props.onClick(); await settle();
    reply = { success: true };
    release(); await pending; await settle();
    assert.ok(!labels().includes("Downloaded"));
    assert.equal(error(), "Unknown error");

    // A failure that lands while the panel is closed is kept but not toasted.
    pending = button("Download v2.0.0").props.onClick(); await settle();
    unmount();
    reply = { success: false, error: "gone" };
    const toastsBefore = toasts.length;
    release(); await pending; await settle();
    assert.equal(toasts.length, toastsBefore);
    await mount();
    assert.equal(error(), "gone");
  } finally {
    release?.(); await settle();
    close();
  }
});
