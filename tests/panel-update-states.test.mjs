// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 Rayekkk
import assert from "node:assert/strict";
import test from "node:test";
import { loadPlugin, nodes, settle } from "./panel-harness.mjs";

test("the updates section shows each answer as it is and asks for the version once", async t => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const calls = [], toasts = [];
  let check = null, versionFails = true, stall = null;
  const notReady = { ready: false, reason: "starting", settings_loaded: true, settings_error: "" };
  const { render, unmount, close, states } = await loadPlugin(route => async () => {
    calls.push(route);
    if (route === "get_state") return { ...notReady };
    if (route === "get_version") {
      if (versionFails) { versionFails = false; throw new Error("not yet"); }
      return { version: "1.0.0" };
    }
    if (route === "check_for_updates") {
      if (stall) await stall;
      if (check instanceof Error) throw check;
      return check;
    }
    if (route === "perform_update") await new Promise(() => {});
    throw new Error(`unexpected ${route}`);
  }, { toast(message) { toasts.push(message); } });
  states[0] = notReady;
  const UpdateSection = nodes(render()).find(node => typeof node.type === "function"
    && node.type.name === "UpdateSection").type;
  const section = () => nodes(render(UpdateSection));
  const mount = async () => { unmount(); section(); await settle(); };
  const labels = () => section().filter(n => n.type === "Field").map(n => n.props.label);
  const installed = () => section().find(n => n.props?.label === "Installed").props.children;
  const button = text => section().find(n => n.props?.children === text);
  const asked = route => calls.filter(call => call === route).length;
  try {
    unmount(); section(); unmount(); section();      // reopened before the first read answers
    await settle();
    assert.equal(asked("get_version"), 1);           // one read in flight is shared
    assert.equal(installed(), "v?");                 // and it failed
    await mount();                                   // a later opening retries it
    assert.equal(installed(), "v1.0.0");
    await mount();
    assert.equal(asked("get_version"), 2);           // known now: never asked again

    check = new Error("offline");
    await button("Check for updates").props.onClick(); await settle();
    assert.deepEqual(labels(), ["Installed", "Error"]);
    assert.equal(section().find(n => n.props?.label === "Error").props.description, "offline");
    assert.equal(toasts.length, 1);

    check = { current_version: "1.0.1", latest_version: "1.0.1", update_available: false };
    let release;
    stall = new Promise(resolve => { release = resolve; });
    const checking = button("Check for updates").props.onClick(); await settle();
    assert.equal(button("Checking...").props.disabled, true);
    release(); stall = null; await checking; await settle();
    assert.deepEqual(labels(), ["Installed", "Latest", "Up to date"]);
    assert.equal(installed(), "v1.0.1");

    check = { current_version: "1.0.1", latest_version: "2.0.0", error: "rate limited" };
    await button("Check for updates").props.onClick(); await settle();
    assert.deepEqual(labels(), ["Installed", "Error"]);

    check = { current_version: "1.0.1", latest_version: "2.0.0", update_available: true };
    await button("Check for updates").props.onClick(); await settle();
    assert.deepEqual(labels(), ["Installed", "Latest"]);        // no asset: nothing to download
    assert.equal(button("Download v2.0.0"), undefined);

    check = { ...check, download_url: "https://example.invalid/a.zip", asset_name: "a.zip" };
    await button("Check for updates").props.onClick(); await settle();
    let download = button("Download v2.0.0");
    stall = new Promise(resolve => { release = resolve; });
    const rechecking = button("Check for updates").props.onClick(); await settle();
    download.props.onClick(); await settle();        // the offer is being replaced: not now
    assert.equal(asked("perform_update"), 0);
    release(); stall = null; await rechecking; await settle();
    download = button("Download v2.0.0");
    download.props.onClick(); download.props.onClick(); await settle();
    assert.equal(asked("perform_update"), 1);        // a second press while it runs sends nothing
  } finally {
    close();
  }
});
