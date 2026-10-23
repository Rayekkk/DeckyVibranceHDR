// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 Rayekkk
// https://github.com/Rayekkk/DeckyVibranceHDR

import { callable, definePlugin, toaster } from "@decky/api";
import {
  ButtonItem,
  Field,
  PanelSection,
  PanelSectionRow,
  SliderField,
  Spinner,
  staticClasses,
  ToggleField,
} from "@decky/ui";
import { FC, useCallback, useEffect, useRef, useState } from "react";
import { createStateSync, WRITE_PENDING } from "./stateSync";

interface State {
  ready: boolean;
  reason: string;
  sdr_enabled: boolean;
  hdr_enabled: boolean;
  sdr_saturation: number;
  hdr_saturation: number;
  sdr_applied: boolean;
  hdr_applied: boolean;
  hdr_applied_saturation: number | null;
  sdr_look_saturation: number | null;
  sdr_error: string;
  sdr_look_error: string;
  hdr_error: string;
  settings_error: string;
  settings_notice: string;
  settings_loaded: boolean;
  generating: boolean;
  conflict: string;
  external_display: boolean;
  sdr_generating: boolean;
  hdr_supported: boolean;
  sdr_min: number;
  sdr_max: number;
  sdr_soft_limit: number;
  hdr_min: number;
  hdr_max: number;
  hdr_soft_limit: number;
}

interface UpdateInfo {
  current_version?: string;
  latest_version?: string;
  update_available?: boolean;
  download_url?: string;
  asset_name?: string;
  error?: string;
}

const getState = callable<[], State>("get_state");
const setSdrEnabled = callable<[boolean, number], State>("set_sdr_enabled");
const setHdrEnabled = callable<[boolean, number], State>("set_hdr_enabled");
const setSdrSaturation = callable<[number], State>("set_sdr_saturation");
const setHdrSaturation = callable<[number], State>("set_hdr_saturation");
const resetAll = callable<[], State>("reset");
const getVersion = callable<[], { version: string }>("get_version");
const checkForUpdates = callable<[], UpdateInfo>("check_for_updates");
const performUpdate = callable<
  [string, string],
  { success: boolean; path?: string; error?: string }
>("perform_update");

const notify = (title: string, body: string) =>
  toaster.toast({ title, body, duration: 5000 });

const pct = (v: number) => `${Math.round(v * 100)}%`;
// A slow write is a backend still at work, not one that is unreachable.
const connectionLabel = (message: string) =>
  message === WRITE_PENDING ? "Waiting for backend" : "Backend unavailable";

// ── Updates ────────────────────────────────────────────────────────────────────
// Operations and results outlive the QAM component. A reopened panel subscribes
// to the same request rather than starting another or losing a completed file.
let updates = { info: null as UpdateInfo | null, version: "", checking: false,
  downloading: false, downloadPath: null as string | null };
const updateListeners = new Set<(state: typeof updates) => void>();
let updatesDisposed = false;
let versionReading: Promise<void> | null = null;
const updateSnapshot = (patch: Partial<typeof updates>) => {
  if (updatesDisposed) return;
  updates = { ...updates, ...patch };
  for (const listener of updateListeners) listener(updates);
};
const updateError = (title: string, message: string) => {
  updateSnapshot({ info: { ...updates.info, error: message } });
  if (!updatesDisposed && updateListeners.size) notify(title, message);
};
const readVersion = () => {
  if (updatesDisposed || versionReading || updates.version) return;
  versionReading = (async () => {
    try { const result = await getVersion(); updateSnapshot({ version: result.version ?? "" }); }
    catch { /* A later mount can retry the version read. */ }
    finally { versionReading = null; }
  })();
};
const checkUpdate = async () => {
  if (updatesDisposed || updates.checking || updates.downloading) return;
  updateSnapshot({ checking: true, info: null });
  try {
    const info = await checkForUpdates();
    // Keep a finished download while it is still the release on offer, and
    // when the check failed, since a failed check offers nothing new.
    const same = !!info.asset_name && !!updates.downloadPath?.endsWith(`/${info.asset_name}`);
    updateSnapshot({ info, downloadPath: same || info.error ? updates.downloadPath : null });
  }
  catch (e) { updateError("Update check failed", e instanceof Error ? e.message : String(e)); }
  finally { updateSnapshot({ checking: false }); }
};
const downloadUpdate = async () => {
  const info = updates.info;
  if (updatesDisposed || updates.checking || updates.downloading
      || !info?.download_url || !info?.asset_name) return;
  updateSnapshot({ downloading: true, info: { ...info, error: undefined } });
  try {
    const result = await performUpdate(info.download_url, info.asset_name);
    if (result.success && result.path) updateSnapshot({ downloadPath: result.path });
    else updateError("Download failed", result.error ?? "Unknown error");
  } catch (e) { updateError("Download failed", e instanceof Error ? e.message : String(e)); }
  finally { updateSnapshot({ downloading: false }); }
};
const UpdateSection: FC = () => {
  const [state, setState] = useState(updates);
  const { info, version, checking, downloading, downloadPath } = state;
  const mounted = useRef(false);
  useEffect(() => {
    mounted.current = true;
    updateListeners.add(setState);
    setState(updates);
    readVersion();
    return () => { mounted.current = false; updateListeners.delete(setState); };
  }, []);
  const check = useCallback(async () => {
    if (mounted.current) await checkUpdate();
  }, []);
  const download = useCallback(async () => {
    if (mounted.current) await downloadUpdate();
  }, []);

  return (
    <PanelSection title="Updates">
      <PanelSectionRow>
        <Field label="Installed" focusable>
          {`v${info?.current_version || version || "?"}`}
        </Field>
      </PanelSectionRow>

      {info?.latest_version && !info.error && (
        <PanelSectionRow>
          <Field label="Latest" focusable>{`v${info.latest_version}`}</Field>
        </PanelSectionRow>
      )}

      {info?.error && (
        <PanelSectionRow>
          <Field label="Error" description={info.error} focusable />
        </PanelSectionRow>
      )}

      {info && !info.error && !info.update_available && !downloadPath && (
        <PanelSectionRow>
          <Field label="Up to date" focusable />
        </PanelSectionRow>
      )}

      {info?.update_available && info.download_url && info.asset_name && !downloadPath && (
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={download} disabled={downloading}>
            {downloading ? "Downloading..." : `Download v${info.latest_version}`}
          </ButtonItem>
        </PanelSectionRow>
      )}

      {downloadPath && (
        <PanelSectionRow>
          <Field
            label="Downloaded"
            focusable
            description={
              `${downloadPath}. To install it, open Decky's settings, turn on ` +
              "Developer mode under General, then open Developer, choose Browse next to " +
              "Install Plugin from ZIP File, Continue on Decky's warning, pick this " +
              "file and confirm. It replaces this version; your " +
              "settings are kept."
            }
          />
        </PanelSectionRow>
      )}

      <PanelSectionRow>
        <ButtonItem layout="below" onClick={check} disabled={checking || downloading}>
          {checking ? "Checking..." : "Check for updates"}
        </ButtonItem>
      </PanelSectionRow>
    </PanelSection>
  );
};

// ── Main content ───────────────────────────────────────────────────────────────
// QAM content can unmount while an RPC is in flight. Keep one queue across
// openings so a previous panel cannot send an old edit after the new one.
const pluginStateSync = createStateSync<State>(getState,
  error => notify("Setting failed", String(error)));
// Decky unmounts it while the Quick Access Menu is closed, which stops the polling.
const Content: FC = () => {
  const [state, setState] = useState<State | null>(null);
  const [connectionError, setConnectionError] = useState("");

  const sync = pluginStateSync;

  useEffect(() => {
    return sync.subscribe(setState, setConnectionError);
  }, [sync]);

  useEffect(() => {
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      await sync.refresh();
      if (!stopped) timer = setTimeout(poll, 1000);
    };
    void poll();
    return () => { stopped = true; clearTimeout(timer); };
  }, [sync]);

  // Slider moves are applied optimistically so the control stays responsive;
  // the backend is what decides the value that actually lands.
  const push = useCallback(
    <K extends keyof State>(fn: (v: State[K]) => Promise<State>, key: K, value: State[K]) =>
      sync.mutate(key, value, () => fn(value)),
    [sync],
  );

  if (!state) {
    return (
      <PanelSection>
        <PanelSectionRow>
          {connectionError ? <Field label={connectionLabel(connectionError)} description={connectionError} /> : <Spinner />}
        </PanelSectionRow>
      </PanelSection>
    );
  }

  if (!state.ready) {
    const settingsLabel = state.settings_loaded ? "Settings not saved" : "Could not load settings";
    // A settings failure is often also the reason startup waits; say it once,
    // unless a connection notice takes the top row.
    const settingsOnTop = !connectionError && !!state.settings_error
      && state.settings_error === state.reason;
    return (
      <><PanelSection title="Not available">
        <PanelSectionRow>
          <Field
            label={connectionError ? connectionLabel(connectionError)
              : settingsOnTop ? settingsLabel : "Plugin not ready"}
            description={connectionError || state.reason}
          />
        </PanelSectionRow>
        {state.settings_error && !settingsOnTop && <PanelSectionRow>
          <Field label={settingsLabel} description={state.settings_error} />
        </PanelSectionRow>}
        {state.settings_notice && <PanelSectionRow>
          <Field label="Settings reset" description={state.settings_notice} />
        </PanelSectionRow>}
      </PanelSection><UpdateSection key="updates" /></>
    );
  }

  return (
    <>
      {(connectionError || state.settings_error || state.settings_notice) && <PanelSection title="Connection and settings">
        {connectionError && <PanelSectionRow>
          <Field label={connectionLabel(connectionError)} description={connectionError} /></PanelSectionRow>}
        {state.settings_error && <PanelSectionRow>
          <Field label="Settings not saved" description={state.settings_error} /></PanelSectionRow>}
        {state.settings_notice && <PanelSectionRow>
          <Field label="Settings reset" description={state.settings_notice} /></PanelSectionRow>}
      </PanelSection>}
      {state.conflict && (
        <PanelSection title="Heads up">
          <PanelSectionRow>
            <Field label="Conflict" description={state.conflict} />
          </PanelSectionRow>
        </PanelSection>
      )}

      {(state.sdr_error || state.sdr_look_error || state.hdr_error) && (
        <PanelSection title="Could not apply the setting">
          {state.sdr_error && <PanelSectionRow>
            <Field label="SDR" description={state.sdr_error} />
          </PanelSectionRow>}
          {state.sdr_look_error && <PanelSectionRow>
            <Field label="SDR look" description={state.sdr_look_error} />
          </PanelSectionRow>}
          {state.hdr_error && <PanelSectionRow>
            <Field label="HDR" description={state.hdr_error} />
          </PanelSectionRow>}
          <PanelSectionRow>
            <Field label="What happens now" description={"The plugin retries on its own." +
              // The switches are hidden while docked, and HDR's once the panel drops PQ.
              (state.external_display || (!state.sdr_error && !state.sdr_look_error && !state.hdr_supported) ? ""
                : " If this stays, switch the effect off and on again, " +
                "and check that Game Mode is running.")} />
          </PanelSectionRow>
        </PanelSection>
      )}

      {state.external_display && (
        <PanelSection title="External display">
          <PanelSectionRow>
            <Field
              label="Standing down"
              description={
                "Settings here are tuned for the built-in screen, so nothing is " +
                "applied to an external display. Unplug it and your saved " +
                "settings apply again."
              }
            />
          </PanelSectionRow>
        </PanelSection>
      )}

      {!state.external_display && (
      <PanelSection title="SDR saturation">
        <PanelSectionRow>
          <ToggleField
            label="Enabled"
            description="Applies to the Steam UI and any game not running in HDR."
            checked={state.sdr_enabled}
            onChange={(v) => {
              const saturation = sync.snapshot()?.sdr_saturation ?? state.sdr_saturation;
              return sync.command({ sdr_enabled: v, sdr_saturation: saturation },
                () => setSdrEnabled(v, saturation), ["sdr_saturation"], { key: "sdr-toggle" });
            }}
          />
        </PanelSectionRow>

        {state.sdr_enabled && (
          <>
            <PanelSectionRow>
              <SliderField
                label="Saturation"
                description={
                  state.sdr_saturation > state.sdr_soft_limit
                    ? `Past ${pct(state.sdr_soft_limit)} the strongest colours start ` +
                      "clipping and lose detail."
                    : "100% is neutral; higher values make colours more saturated."
                }
                value={state.sdr_saturation}
                min={state.sdr_min}
                max={state.sdr_max}
                step={0.01}
                minimumDpadGranularity={40}
                onChange={(v) => push(setSdrSaturation, "sdr_saturation", v)}
              />
            </PanelSectionRow>
            <PanelSectionRow>
              <Field
                label="Now"
                description={
                  state.sdr_generating
                    ? "Preparing the colours..."
                    // Only a standard-gamut panel past the atom's range has a look,
                    // and there the slider is twice the look (test_controls SdrLookSplit).
                    : state.sdr_look_saturation !== null
                      ? `Last applied: ${pct(2 * state.sdr_look_saturation)}`
                      : undefined
                }
                focusable
              >
                {pct(state.sdr_saturation)}
              </Field>
            </PanelSectionRow>
          </>
        )}
      </PanelSection>

      )}

      {/* A panel with no PQ in its EDID can never reach the look, so the
          section is not rendered at all rather than shown as unusable. */}
      {!state.external_display && state.hdr_supported && (
      <PanelSection title="HDR saturation">
        <PanelSectionRow>
          <ToggleField
            label="Enabled"
            description="Applies only to games actually running in HDR."
            checked={state.hdr_enabled}
            onChange={(v) => {
              const saturation = sync.snapshot()?.hdr_saturation ?? state.hdr_saturation;
              return sync.command({ hdr_enabled: v, hdr_saturation: saturation },
                () => setHdrEnabled(v, saturation), ["hdr_saturation"], { key: "hdr-toggle" });
            }}
          />
        </PanelSectionRow>

        {state.hdr_enabled && (
          <>
            <PanelSectionRow>
              <SliderField
                label="Saturation"
                description={
                  state.hdr_saturation > state.hdr_soft_limit
                    ? `Past ${pct(state.hdr_soft_limit)} the strongest colours start ` +
                      "clipping and lose detail."
                    : "Raises colour saturation without changing brightness."
                }
                value={state.hdr_saturation}
                min={state.hdr_min}
                max={state.hdr_max}
                step={0.01}
                minimumDpadGranularity={25}
                onChange={(v) => push(setHdrSaturation, "hdr_saturation", v)}
              />
            </PanelSectionRow>
            <PanelSectionRow>
              <Field
                label="Now"
                description={
                  state.generating
                    ? "Preparing the colours..."
                    : state.hdr_applied_saturation !== null
                      ? `Last applied: ${pct(state.hdr_applied_saturation)}`
                      : state.hdr_saturation === 1
                        ? "100% is neutral, nothing to apply"
                        : "Waiting to apply the HDR setting"
                }
                focusable
              >
                {pct(state.hdr_saturation)}
              </Field>
            </PanelSectionRow>
          </>
        )}
      </PanelSection>
      )}

      <PanelSection title="Reset">
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            onClick={() => sync.command({ sdr_enabled: false, hdr_enabled: false,
              sdr_saturation: 1, hdr_saturation: 1 }, resetAll,
              ["sdr_saturation", "hdr_saturation"],
              { key: "reset", supersedes: ["sdr-toggle", "hdr-toggle"] })}
          >
            Back to defaults
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>

      <UpdateSection key="updates" />
    </>
  );
};

// ── Icon ───────────────────────────────────────────────────────────────────────
const VibranceIcon: FC = () => (
  <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor"
    style={{ width: "1em", height: "1em" }}>
    <path d="M12 2a10 10 0 1 0 0 20c1.1 0 2-.9 2-2 0-.5-.2-1-.5-1.3-.3-.4-.5-.8-.5-1.2 0-1.1.9-2 2-2h2.4c3.1 0 5.6-2.5 5.6-5.6C23 5.6 18.1 2 12 2zm-6 10a1.5 1.5 0 1 1 0-3 1.5 1.5 0 0 1 0 3zm3-4a1.5 1.5 0 1 1 0-3 1.5 1.5 0 0 1 0 3zm6 0a1.5 1.5 0 1 1 0-3 1.5 1.5 0 0 1 0 3zm3 4a1.5 1.5 0 1 1 0-3 1.5 1.5 0 0 1 0 3z" />
  </svg>
);

export default definePlugin(() => ({
  name: "Decky Vibrance HDR",
  titleView: <div className={staticClasses.Title}>Decky Vibrance HDR</div>,
  content: <Content />,
  icon: <VibranceIcon />,
  onDismount() {
    pluginStateSync.dispose();
    updatesDisposed = true;
    updateListeners.clear();
    /* the backend restores both settings in _unload */
  },
}));
