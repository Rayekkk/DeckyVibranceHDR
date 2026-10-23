// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 Rayekkk
// A stand-in for the Decky host: React's hooks, the @decky/ui components and
// the loader connection, just enough to render the built panel as plain objects.
import { readFileSync } from "node:fs";

/** A complete get_state reply (every key of PluginState.public); tests override what they need. */
export const baseState = { ready: true, reason: "ready", sdr_enabled: true, hdr_enabled: true,
  sdr_saturation: 1, hdr_saturation: 1, sdr_applied: true, sdr_look_applied: false, hdr_applied: false,
  hdr_applied_saturation: null, sdr_look_saturation: null, sdr_error: "", sdr_look_error: "",
  hdr_error: "", settings_error: "", settings_notice: "", settings_loaded: true, generating: false,
  sdr_generating: false, conflict: "", external_display: false, hdr_supported: true,
  sdr_min: 1, sdr_max: 3, sdr_soft_limit: 2, hdr_min: 1, hdr_max: 1.5, hdr_soft_limit: 1.3 };

/** Every element in a rendered tree, depth first. */
export const nodes = tree => !tree ? [] : Array.isArray(tree) ? tree.flatMap(nodes)
  : typeof tree !== "object" ? [] : [tree, ...nodes(tree.props?.children)];

/** Let pending promise chains run. */
export const settle = async () => { for (let i = 0; i < 20; ++i) await Promise.resolve(); };

/**
 * Load dist/index.js against *callable* (route => async (...args) => reply).
 * The built module keeps one state queue per process, so each test file that
 * mounts the panel runs in its own process (node --test does that per file).
 */
export async function loadPlugin(callable, toaster = { toast() {} }) {
  const states = [], refs = [], effects = [], cleanups = [], connections = [];
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
  globalThis.SP_JSX = { jsx: (type, props, key) => ({ type, props, key }),
    jsxs: (type, props, key) => ({ type, props, key }) };
  globalThis.DFL = { staticClasses: { Title: "title" } };
  for (const name of ["ButtonItem", "Field", "PanelSection", "PanelSectionRow", "SliderField", "Spinner", "ToggleField"])
    globalThis.DFL[name] = name;
  globalThis.window = { __DECKY_SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED_deckyLoaderAPIInit: {
    connect: (version, name) => { connections.push([version, name]); return { _version: version, callable, toaster }; },
  } };
  const source = readFileSync(new URL("../dist/index.js", import.meta.url), "utf8");
  const plugin = (await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`)).default();
  /** One render pass of *component* (the panel by default), hooks in call order. */
  const render = (component = plugin.content.type) => {
    stateIndex = refIndex = effectIndex = 0;
    return component();
  };
  /** The panel closes: effects clean up and the next render mounts afresh. */
  const unmount = () => {
    for (const cleanup of cleanups) cleanup?.();
    for (const values of [states, refs, effects, cleanups]) values.length = 0;
  };
  const close = () => {
    unmount();
    plugin.onDismount();
    for (const key of ["SP_REACT", "SP_JSX", "DFL", "window"]) delete globalThis[key];
  };
  return { plugin, render, unmount, close, states, cleanups, connections };
}
