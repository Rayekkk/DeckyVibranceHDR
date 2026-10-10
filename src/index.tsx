// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 Rayekkk
// https://github.com/Rayekkk/DeckyVibranceHDR

import { callable, definePlugin, toaster, useQuickAccessVisible } from "@decky/api";
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
import { createStateSync } from "./stateSync";

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
  sdr_error: string;
  sdr_look_error: string;
  hdr_error: string;
  settings_error: string;
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

// ── Updates ────────────────────────────────────────────────────────────────────
const UpdateSection: FC = () => {
  const [info, setInfo] = useState<UpdateInfo | null>(null);
  const [version, setVersion] = useState("");
  const [checking, setChecking] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [downloadPath, setDownloadPath] = useState<string | null>(null);
  const mounted = useRef(false);
  const busy = useRef(false);

  useEffect(() => {
    let active = true;
    mounted.current = true;
    getVersion()
      .then((v) => { if (active) setVersion(v.version ?? ""); })
      .catch(() => undefined);
    return () => { active = false; mounted.current = false; };
  }, []);

  const check = useCallback(async () => {
    if (busy.current) return;
    busy.current = true;
    setChecking(true);
    setInfo(null);
    setDownloadPath(null);
    try {
      const result = await checkForUpdates();
      if (mounted.current) setInfo(result);
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      if (mounted.current) { notify("Update check failed", msg); setInfo({ error: msg }); }
    } finally {
      busy.current = false;
      if (mounted.current) setChecking(false);
    }
  }, []);

  const download = useCallback(async () => {
    if (busy.current || !info?.download_url || !info?.asset_name) return;
    busy.current = true;
    setDownloading(true);
    try {
      const res = await performUpdate(info.download_url, info.asset_name);
      if (!mounted.current) return;
      if (res.success && res.path) setDownloadPath(res.path);
      else {
        setInfo({ ...info, error: res.error });
        notify("Download failed", res.error ?? "Unknown error");
      }
    } catch (e) {
      if (mounted.current) notify("Download failed", e instanceof Error ? e.message : String(e));
    } finally {
      busy.current = false;
      if (mounted.current) setDownloading(false);
    }
  }, [info]);

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
          <Field label="Error" description={info.error} />
        </PanelSectionRow>
      )}

      {info && !info.error && !info.update_available && !downloadPath && (
        <PanelSectionRow>
          <Field label="Up to date" />
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
            description={
              `${downloadPath} - to install: Decky, Developer, uninstall ` +
              "DeckyVibranceHDR, then Install Plugin from ZIP and pick that file. " +
              "Your settings are kept."
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
const pluginStateSync = createStateSync<State>(getState, () => undefined,
  error => notify("Setting failed", String(error)));
const Content: FC = () => {
  const visible = useQuickAccessVisible();
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
      if (!stopped && visible) timer = setTimeout(poll, 1000);
    };
    void poll();
    return () => { stopped = true; clearTimeout(timer); };
  }, [visible, sync]);

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
          {connectionError ? <Field label="Backend unavailable" description={connectionError} /> : <Spinner />}
        </PanelSectionRow>
        {connectionError && <PanelSectionRow><ButtonItem onClick={() => void sync.refresh()}>
          Retry
        </ButtonItem></PanelSectionRow>}
      </PanelSection>
    );
  }

  if (!state.ready) {
    return (
      <><PanelSection title="Not available">
        <PanelSectionRow>
          <Field
            label="gamescope not reachable"
            description={
              `${connectionError || state.reason}. This plugin talks to gamescope directly, so it ` +
              "only works in Game Mode."
            }
          />
        </PanelSectionRow>
        {state.settings_error && <PanelSectionRow>
          <Field label="Settings not saved" description={state.settings_error} />
        </PanelSectionRow>}
        {connectionError && <PanelSectionRow><ButtonItem onClick={() => void sync.refresh()}>
          Retry
        </ButtonItem></PanelSectionRow>}
      </PanelSection><UpdateSection /></>
    );
  }

  return (
    <>
      {(connectionError || state.settings_error) && <PanelSection title="Connection and settings">
        <PanelSectionRow><Field label={connectionError ? "Backend unavailable" : "Settings not saved"}
          description={connectionError || state.settings_error} /></PanelSectionRow>
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
        </PanelSection>
      )}

      {state.external_display && (
        <PanelSection title="External display">
          <PanelSectionRow>
            <Field
              label="Standing down"
              description={
                "Everything here is calibrated against the built-in panel - " +
                "its gamut decides the mapping and its EDID decides whether " +
                "the HDR half exists. Nothing is applied to an external " +
                "display. Unplug it and the settings come back."
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
                    ? `Past ${pct(state.sdr_soft_limit)} colours start clipping ` +
                      "and detail in saturated areas goes with them."
                    : "100% is neutral. How much room there is above it " +
                      "depends on the panel."
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
                  state.sdr_generating ? "building the LUT..." : undefined
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
            description="Applies only to games actually running in HDR. gamescope has no dial for this, so the plugin builds a colour LUT for it."
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
                    ? `Above ${pct(state.hdr_soft_limit)} channels start clipping ` +
                      "at the edge of the gamut."
                    : "ICtCp preserves intensity and chroma direction before clipping."
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
                    ? "building the LUT..."
                    : state.hdr_applied_saturation !== null
                      ? `Last LUT sent: ${pct(state.hdr_applied_saturation)}`
                      : state.hdr_saturation === 1
                        ? "100% needs no LUT"
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

      <UpdateSection />
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
    /* the backend restores both settings in _unload */
  },
}));
