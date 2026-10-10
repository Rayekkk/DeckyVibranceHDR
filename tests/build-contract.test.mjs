import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

test("built frontend connects to Decky using host globals and manifest", async () => {
  const manifest = JSON.parse(readFileSync(new URL("../plugin.json", import.meta.url), "utf8"));
  const source = readFileSync(new URL("../dist/index.js", import.meta.url), "utf8");
  const connections = [];
  const states = [], refs = [], effects = [], cleanups = [];
  let stateIndex = 0, refIndex = 0, effectIndex = 0;
  globalThis.SP_REACT = {
    useState(initial) { const i = stateIndex++; if (!(i in states)) states[i] = initial;
      return [states[i], value => { states[i] = value; }]; },
    useRef(initial) { const i = refIndex++; return refs[i] ??= { current: initial }; },
    useCallback(fn) { return fn; },
    useEffect(fn, deps) { const i = effectIndex++;
      if (!effects[i] || deps.some((value, n) => effects[i][n] !== value)) {
        cleanups[i]?.(); effects[i] = deps; cleanups[i] = fn();
      } },
  };
  globalThis.SP_JSX = { jsx: (type, props) => ({ type, props }), jsxs: (type, props) => ({ type, props }) };
  globalThis.DFL = { staticClasses: { Title: "title" } };
  for (const name of ["ButtonItem", "Field", "PanelSection", "PanelSectionRow", "SliderField", "Spinner", "ToggleField"])
    globalThis.DFL[name] = name;
  let backend = { ready: true, sdr_enabled: true, hdr_enabled: true, sdr_saturation: 1,
    hdr_saturation: 1, sdr_applied: true, hdr_applied: false, hdr_applied_saturation: null,
    sdr_error: "", sdr_look_error: "", hdr_error: "", settings_error: "", generating: false,
    conflict: "", external_display: false, sdr_generating: false, hdr_supported: true,
    sdr_min: 1, sdr_max: 3, sdr_soft_limit: 2, hdr_min: 1, hdr_max: 1.5, hdr_soft_limit: 1.3 };
  const calls = [], reads = [];
  let releaseCheck, releaseDownload;
  const checkReply = new Promise(resolve => { releaseCheck = resolve; });
  const downloadReply = new Promise(resolve => { releaseDownload = resolve; });
  let releaseSlider;
  const sliderReply = new Promise(resolve => { releaseSlider = resolve; });
  globalThis.window = { __DECKY_SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED_deckyLoaderAPIInit: {
    connect: (version, name) => {
      connections.push([version, name]);
      return { _version: version, callable: route => async (...args) => {
        if (route === "get_state") {
          reads.push(JSON.parse(JSON.stringify(args)));
          if (args.length) throw new Error("get_state expects no arguments");
          return { ...backend };
        }
        calls.push([route, ...args]);
        if (route === "get_version") return { version: manifest.version };
        if (route === "check_for_updates") { await checkReply; return {
          current_version: manifest.version, latest_version: "9.0.0", update_available: true,
          download_url: "https://github.com/release.zip", asset_name: "release.zip" }; }
        if (route === "perform_update") { await downloadReply; return { success: true, path: "/Downloads/release.zip" }; }
        if (route === "set_sdr_saturation") { await sliderReply; backend.sdr_saturation = args[0]; }
        if (route === "set_sdr_enabled") { backend.sdr_enabled = args[0]; backend.sdr_saturation = args[1]; }
        if (route === "reset") backend = { ...backend, sdr_enabled: false, hdr_enabled: false,
          sdr_saturation: 1, hdr_saturation: 1 };
        return { ...backend };
      }, toaster: { toast() {} }, useQuickAccessVisible: () => true };
    },
  } };
  try {
    const built = await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);
    const plugin = built.default();
    assert.deepEqual(connections, [[2, manifest.name]]);
    assert.equal(plugin.name, manifest.name);
    assert.ok(plugin.content);
    assert.ok(plugin.icon);
    const render = () => {
      stateIndex = refIndex = effectIndex = 0;
      return plugin.content.type();
    };
    const settle = async () => { for (let i = 0; i < 12; ++i) await Promise.resolve(); };
    const nodes = tree => {
      if (!tree) return [];
      if (Array.isArray(tree)) return tree.flatMap(nodes);
      if (typeof tree !== "object") return [];
      return [tree, ...nodes(tree.props?.children)];
    };
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
    const updateComponent = nodes(render()).find(node => typeof node.type === "function" && node.type.name === "UpdateSection");
    assert.ok(updateComponent);
    for (const cleanup of cleanups) cleanup?.();
    for (const values of [states, refs, effects, cleanups]) values.length = 0;
    const renderUpdates = () => { stateIndex = refIndex = effectIndex = 0; return updateComponent.type(); };
    renderUpdates(); await settle();
    const check = nodes(renderUpdates()).find(node => node.props?.children === "Check for updates");
    const checking = check.props.onClick();
    await check.props.onClick();
    assert.equal(calls.filter(call => call[0] === "check_for_updates").length, 1);
    releaseCheck(); await checking; await settle();
    const download = nodes(renderUpdates()).find(node => node.props?.children === "Download v9.0.0");
    const downloading = download.props.onClick();
    await download.props.onClick();
    assert.equal(calls.filter(call => call[0] === "perform_update").length, 1);
    for (const cleanup of cleanups) cleanup?.();
    const beforeUnmountedReply = structuredClone(states);
    releaseDownload(); await downloading; await settle();
    assert.deepEqual(states, beforeUnmountedReply, "an unmounted updater must ignore the download reply");
    plugin.onDismount();
  } finally {
    for (const cleanup of cleanups) cleanup?.();
    for (const key of ["SP_REACT", "SP_JSX", "DFL", "window"]) delete globalThis[key];
  }
});
