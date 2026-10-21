// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 Rayekkk
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { loadPlugin, nodes, settle } from "./panel-harness.mjs";

test("built frontend connects to Decky using host globals and manifest", async () => {
  const manifest = JSON.parse(readFileSync(new URL("../plugin.json", import.meta.url), "utf8"));
  let backend = { ready: true, sdr_enabled: true, hdr_enabled: true, sdr_saturation: 1,
    hdr_saturation: 1, sdr_applied: true, hdr_applied: false, hdr_applied_saturation: null,
    sdr_error: "", sdr_look_error: "", hdr_error: "", settings_error: "", settings_loaded: true, generating: false,
    conflict: "", external_display: false, sdr_generating: false, hdr_supported: true,
    sdr_min: 1, sdr_max: 3, sdr_soft_limit: 2, hdr_min: 1, hdr_max: 1.5, hdr_soft_limit: 1.3 };
  const calls = [], reads = [];
  let releaseCheck, releaseDownload;
  let downloadError = "";
  let latest = "9.0.0";
  let checkError = "";
  const checkReply = new Promise(resolve => { releaseCheck = resolve; });
  const downloadReply = new Promise(resolve => { releaseDownload = resolve; });
  let releaseSlider;
  const sliderReply = new Promise(resolve => { releaseSlider = resolve; });
  const { plugin, render, unmount, close, connections, states, cleanups } = await loadPlugin(route => async (...args) => {
    if (route === "get_state") {
      reads.push(JSON.parse(JSON.stringify(args)));
      if (args.length) throw new Error("get_state expects no arguments");
      return { ...backend };
    }
    calls.push([route, ...args]);
    if (route === "get_version") return { version: manifest.version };
    if (route === "check_for_updates") { await checkReply;
      if (checkError) return { current_version: manifest.version, error: checkError };
      return {
      current_version: manifest.version, latest_version: latest, update_available: true,
      download_url: "https://github.com/release.zip", asset_name: `release-${latest}.zip` }; }
    if (route === "perform_update") { await downloadReply; if (downloadError) throw Error(downloadError); return { success: true, path: `/Downloads/${args[1]}` }; }
    if (route === "set_sdr_saturation") { await sliderReply; backend.sdr_saturation = args[0]; }
    if (route === "set_sdr_enabled") { backend.sdr_enabled = args[0]; backend.sdr_saturation = args[1]; }
    if (route === "reset") backend = { ...backend, sdr_enabled: false, hdr_enabled: false,
      sdr_saturation: 1, hdr_saturation: 1 };
    return { ...backend };
  });
  try {
    assert.deepEqual(connections, [[2, manifest.name]]);
    assert.equal(plugin.name, manifest.name);
    assert.ok(plugin.content);
    assert.ok(plugin.icon);
    render(); await settle();
    assert.deepEqual(reads, [[]], "the built panel must call get_state without a Promise callback argument");
    const controls = nodes(render());
    const slider = controls.find(node => node.type === "SliderField");
    const toggle = controls.find(node => node.type === "ToggleField");
    const a = slider.props.onChange(1.1);
    const b = slider.props.onChange(1.5);
    const off = toggle.props.onChange(false);
    const toggles = Array.from({ length: 40 }, (_, n) => toggle.props.onChange(n % 2 === 0));
    releaseSlider(); await Promise.all([a, b, off, ...toggles]); await settle();
    assert.deepEqual(calls, [["set_sdr_saturation", 1.1], ["set_sdr_enabled", false, 1.5]]);
    const reset = nodes(render()).find(node => node.type === "ButtonItem" && node.props.children === "Back to defaults");
    await reset.props.onClick();
    assert.deepEqual(calls.at(-1), ["reset"]);
    assert.equal(states[0].hdr_enabled, false);
    assert.equal(states[0].hdr_saturation, 1);
    // The label follows settings_loaded, not the wording of the backend reason.
    backend = { ...backend, ready: false, reason: "settings unreadable", settings_loaded: false,
      settings_error: "invalid JSON" };
    await toggle.props.onChange(false);
    const blocked = nodes(render()).filter(node => node.type === "Field");
    assert.ok(blocked.some(node => node.props.label === "Could not load settings"));
    // When the settings failure is also the reason startup waits, it is said once.
    backend = { ...backend, reason: "could not load settings: EACCES", settings_error: "could not load settings: EACCES" };
    await toggle.props.onChange(false);
    const once = nodes(render()).filter(node => node.type === "Field" && node.props.label === "Could not load settings");
    assert.equal(once.length, 1);
    backend = { ...backend, reason: "could not load settings: lookalike", settings_loaded: true,
      settings_error: "disk full" };
    await toggle.props.onChange(false);
    const unsaved = nodes(render()).filter(node => node.type === "Field");
    assert.ok(unsaved.some(node => node.props.label === "Settings not saved"));
    assert.ok(blocked.every(node => !String(node.props.description).includes("only works in Game Mode")));
    backend = { ...backend, ready: true, reason: "ready", settings_error: "", settings_loaded: true };
    await toggle.props.onChange(false);
    const updateComponent = nodes(render()).find(node => typeof node.type === "function" && node.type.name === "UpdateSection");
    assert.ok(updateComponent);
    unmount();
    const renderUpdates = () => render(updateComponent.type);
    renderUpdates(); await settle();
    const check = nodes(renderUpdates()).find(node => node.props?.children === "Check for updates");
    const checking = check.props.onClick();
    await check.props.onClick();
    assert.equal(calls.filter(call => call[0] === "check_for_updates").length, 1);
    const remountUpdates = async () => {
      unmount();
      renderUpdates(); await settle();
    };
    await remountUpdates();
    const resumedCheck = nodes(renderUpdates()).find(node => node.props?.children === "Checking...");
    assert.ok(resumedCheck, "a reopened updater must retain its pending check");
    await resumedCheck.props.onClick();
    assert.equal(calls.filter(call => call[0] === "check_for_updates").length, 1);
    releaseCheck(); await checking; await settle();
    const download = nodes(renderUpdates()).find(node => node.props?.children === "Download v9.0.0");
    const downloading = download.props.onClick();
    await download.props.onClick();
    assert.equal(calls.filter(call => call[0] === "perform_update").length, 1);
    await remountUpdates();
    const resumedDownload = nodes(renderUpdates()).find(node => node.props?.children === "Downloading...");
    assert.ok(resumedDownload, "a reopened updater must retain its pending download");
    await resumedDownload.props.onClick();
    assert.equal(calls.filter(call => call[0] === "perform_update").length, 1);
    for (const cleanup of cleanups) cleanup?.();
    const beforeUnmountedReply = structuredClone(states);
    releaseDownload(); await downloading; await settle();
    assert.deepEqual(states, beforeUnmountedReply, "an unmounted updater must ignore the download reply");
    await remountUpdates();
    const completed = nodes(renderUpdates()).find(node => node.props?.label === "Downloaded");
    // The Download button that had focus is gone; the result must take it.
    assert.equal(completed?.props.focusable, true);
    assert.ok(completed?.props.description.includes("/Downloads/release-9.0.0.zip"),
      "a download that finished with the panel closed must be visible after reopening");
    const oldCallCount = calls.length;
    await check.props.onClick();
    await download.props.onClick();
    assert.equal(calls.length, oldCallCount, "handlers from a closed updater must not start new RPCs");
    const freshCheck = nodes(renderUpdates()).find(node => node.props?.children === "Check for updates");
    latest = "9.0.1";  // a newer release replaces the finished download's offer
    await freshCheck.props.onClick(); await settle();
    assert.ok(!nodes(renderUpdates()).some(node => node.props?.label === "Downloaded"));
    const retryDownload = nodes(renderUpdates()).find(node => node.props?.children === "Download v9.0.1");
    downloadError = "temporary network error";
    const failedDownload = retryDownload.props.onClick();
    for (const cleanup of cleanups) cleanup?.();
    const closedState = structuredClone(states);
    await failedDownload; await settle();
    assert.deepEqual(states, closedState, "a closed panel must not receive error state updates");
    await remountUpdates();
    const failure = nodes(renderUpdates()).find(node => node.props?.label === "Error");
    assert.equal(failure?.props.description, "temporary network error",
      "a download failure while closed must remain visible after reopening");
    assert.equal(calls.filter(call => call[0] === "get_version").length, 1,
      "reopening must share its cached installed version");
    downloadError = "";
    await nodes(renderUpdates()).find(node => node.props?.children === "Download v9.0.1").props.onClick();
    await settle();
    let labels = nodes(renderUpdates()).map(node => node.props?.label);
    assert.ok(labels.includes("Downloaded"));
    assert.ok(!labels.includes("Error"), "a successful retry must clear the earlier download error");
    await nodes(renderUpdates()).find(node => node.props?.children === "Check for updates").props.onClick();
    await settle();
    labels = nodes(renderUpdates()).map(node => node.props?.label);
    assert.ok(labels.includes("Downloaded"), "re-checking the same release keeps its finished download");
    assert.ok(!nodes(renderUpdates()).some(node => node.props?.children === "Download v9.0.1"));
    checkError = "API rate limit exceeded";
    await nodes(renderUpdates()).find(node => node.props?.children === "Check for updates").props.onClick();
    await settle();
    labels = nodes(renderUpdates()).map(node => node.props?.label);
    assert.ok(labels.includes("Error"));
    assert.ok(labels.includes("Downloaded"), "a failed check offers nothing new and keeps the download");
    checkError = "";
    const afterDispose = nodes(renderUpdates()).find(node => node.props?.children === "Check for updates");
    plugin.onDismount();
    const disposedCallCount = calls.length;
    await afterDispose.props.onClick();
    assert.equal(calls.length, disposedCallCount);
  } finally {
    close();
  }
});
