// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 Rayekkk
// https://github.com/Rayekkk/DeckyVibranceHDR

import { callable, definePlugin, useQuickAccessVisible } from "@decky/api";
import {
  Field,
  PanelSection,
  PanelSectionRow,
  SliderField,
  staticClasses,
  ToggleField,
} from "@decky/ui";
import { FC, ReactNode, useEffect, useState } from "react";
import { AboutPage, UpdateCheck, UpdateController, UpdateDownload, UpdateSection } from "./about";
import { Result, settle } from "./result";
import { ActionButton, colors, notify, PageHeader, PageShell, SectionLink, statusPlaceholder } from "./ui";
import { createWriteQueue } from "./writeQueue";

const NAME = "Decky Vibrance HDR";

interface State {
  ready: boolean;
  reason: string;
  sdr_enabled: boolean;
  hdr_enabled: boolean;
  sdr_saturation: number;
  hdr_saturation: number;
  sdr_applied: boolean;
  sdr_look_applied: boolean;
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

const getState = callable<[], Result<State>>("get_state");
const setSdrEnabled = callable<[boolean, number], Result<State>>("set_sdr_enabled");
const setHdrEnabled = callable<[boolean, number], Result<State>>("set_hdr_enabled");
const setSdrSaturation = callable<[number], Result<State>>("set_sdr_saturation");
const setHdrSaturation = callable<[number], Result<State>>("set_hdr_saturation");
const resetAll = callable<[], Result<State>>("reset");
const getVersion = callable<[], Result<never> & { version?: string; home?: string }>("get_version");
const checkForUpdates = callable<[], UpdateCheck>("check_for_updates");
const performUpdate = callable<[string], UpdateDownload>("perform_update");

/** Shown while a write has not answered; the backend may still be working. */
const WRITE_PENDING = "Backend write is still pending; waiting for its result";
/** Shown while a read has not answered; Decky replays it, so no second read is sent. */
const READ_TIMED_OUT = "Error: Backend read timed out";
// Quick while the backend is busy or not ready (a LUT build, a reconnect), slow when nothing is moving: the
// backend itself re-reads the display every 5 s, so a faster poll would only repeat the same answer.
const POLL_BUSY_MS = 1000;
const POLL_IDLE_MS = 3000;

const pct = (v: number) => `${Math.round(v * 100)}%`;
// A damaged file brought back from its last good copy is no reset (vibrance_settings.load words both).
const noticeLabel = (notice: string) => notice.includes("last good copy") ? "Settings restored" : "Settings reset";
// A slow write is a backend still at work, not one that is unreachable.
const connectionLabel = (message: string) =>
  message === WRITE_PENDING ? "Waiting for backend" : "Backend unavailable";

// QAM content can unmount while an RPC is in flight. Keep one queue across
// openings so a previous panel cannot send an old edit after the new one.
const queue = createWriteQueue<State>(getState,
  { onError: result => notify(NAME, result.error || "Unknown error") });
// Like the queue, a check or download outlives the panel; a reopened one shows its progress.
const updates = new UpdateController({ check: checkForUpdates, download: performUpdate }, "deckyvibrancehdr");

// The installed version and the home folder (for ~/ paths) are read once; a failed read is tried again by a later
// opening, and a reopened panel shares the read in flight.
let installed = { version: "", home: "" };
let reading: Promise<void> | null = null;
const readInstalled = () => installed.version ? Promise.resolve() : reading ??= settle(getVersion()).then(result => {
  if (result.ok && result.version) installed = { version: result.version, home: result.home ?? "" };
  reading = null;
});
const ABOUT_ICON = <><circle cx="12" cy="12" r="9" /><path d="M12 11v6m0-10h.01" /></>;

const busy = (state: State | null, problem: string) => !state || !state.ready || !!problem || state.generating
  || state.sdr_generating || !!(state.sdr_error || state.sdr_look_error || state.hdr_error || state.settings_error);

// Decky unmounts it while the Quick Access Menu is closed, which stops the polling.
const Content: FC = () => {
  const [view, setView] = useState(queue.get());
  const [page, setPage] = useState<"main" | "about">("main");
  const [left, setLeft] = useState(false);  // About was just closed: its row takes the focus back.
  const [about, setAbout] = useState(installed);
  const visible = useQuickAccessVisible();
  useEffect(() => {
    let live = true;
    void readInstalled().then(() => { if (live) setAbout(installed); });
    return () => { live = false; };
  }, []);
  useEffect(() => {
    setView(queue.get());
    return queue.subscribe(() => setView(queue.get()));
  }, []);

  useEffect(() => {
    let stopped = false, polling = false, due = 0;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const schedule = () => {
      if (stopped || polling) return;
      const now = queue.get();
      const wait = busy(now.state, now.error || now.slow) ? POLL_BUSY_MS : POLL_IDLE_MS;
      if (timer !== undefined && due <= Date.now() + wait) return;  // already that soon
      clearTimeout(timer);
      due = Date.now() + wait;
      timer = setTimeout(poll, wait);
    };
    const poll = async () => {
      timer = undefined;
      polling = true;
      await queue.read();
      polling = false;
      schedule();
    };
    void poll();
    // A change that starts work (a color table being prepared) brings the next poll forward.
    const off = queue.subscribe(schedule);
    return () => { stopped = true; off(); clearTimeout(timer); };
  }, []);

  const state = view.state;
  const connectionError = view.slow === "write" ? WRITE_PENDING : view.slow === "read" ? READ_TIMED_OUT : view.error;

  // Slider moves are shown at once so the control stays responsive; the
  // backend is what decides the value that actually lands.
  const slide = (key: "sdr_saturation" | "hdr_saturation", set: (v: number) => Promise<Result<State>>) =>
    (v: number) => queue.write(key, () => set(v), { patch: { [key]: v } });
  // A refused change says why under its control until that control is used again (UI.md §5); the toast says it too.
  const why = (key: string, description?: ReactNode) => view.refused[key]
    ? <>{description}<div role="alert" style={{ color: colors.error }}>{view.refused[key]}</div></> : description;
  const toggle = (half: "sdr" | "hdr", set: (on: boolean, saturation: number) => Promise<Result<State>>) =>
    (v: boolean) => {
      // The toggle carries the newest slider value and makes its unsent moves pointless.
      const saturation = queue.get().state?.[`${half}_saturation`] ?? 1;
      return queue.write(`${half}-toggle`, () => set(v, saturation),
        { patch: { [`${half}_enabled`]: v, [`${half}_saturation`]: saturation }, supersedes: [`${half}_saturation`] });
    };

  const version = about.version || "—";
  if (page === "about") {
    const back = () => { setLeft(true); setPage("main"); };
    return (
      <PageShell onBack={back}>
        <PageHeader title="About" back={NAME} onBack={back} focus={visible} />
        <AboutPage name={NAME} version={version} license="BSD-3-Clause" source="github.com/Rayekkk/DeckyVibranceHDR"
          description="Raises color saturation in SDR and HDR, calibrated from your built-in screen."
          notes="Copyright (c) 2026 Rayekkk. Dependency notices are included in the plugin package."
          updates={updates} home={about.home} />
      </PageShell>
    );
  }
  // The last row of every layout; the key keeps its focus when the layout above it changes.
  const aboutRow = (
    <PanelSection key="about">
      <SectionLink title="About" icon={ABOUT_ICON} focus={visible && left} onOpen={() => setPage("about")}
        description={`Version ${version} · updates`} />
    </PanelSection>
  );

  if (!state) {
    return (
      <>
        {statusPlaceholder({ label: `${NAME} unavailable`, error: connectionError, onRetry: () => void queue.read() })}
        {/* A plugin that cannot start can still be updated. */}
        {connectionError && <UpdateSection controller={updates} version={version} home={about.home} />}
      </>
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
          <Field label={noticeLabel(state.settings_notice)} description={state.settings_notice} />
        </PanelSectionRow>}
      </PanelSection>{aboutRow}</>
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
          <Field label={noticeLabel(state.settings_notice)} description={state.settings_notice} /></PanelSectionRow>}
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
            description={why("sdr-toggle", "Applies to the Steam UI and any game not running in HDR.")}
            checked={state.sdr_enabled}
            onChange={toggle("sdr", setSdrEnabled)}
          />
        </PanelSectionRow>

        {state.sdr_enabled && (
          <>
            <PanelSectionRow>
              <SliderField
                label="Saturation"
                description={why("sdr_saturation",
                  state.sdr_saturation > state.sdr_soft_limit
                    ? `Past ${pct(state.sdr_soft_limit)} the strongest colors start ` +
                      "clipping and lose detail."
                    : "100% is neutral; higher values make colors more saturated.")}
                value={state.sdr_saturation}
                min={state.sdr_min}
                max={state.sdr_max}
                step={0.01}
                minimumDpadGranularity={40}
                onChange={slide("sdr_saturation", setSdrSaturation)}
              />
            </PanelSectionRow>
            <PanelSectionRow>
              <Field
                label="Now"
                description={
                  state.sdr_generating
                    ? "Preparing the colors…"
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
            description={why("hdr-toggle", "Applies only to games actually running in HDR.")}
            checked={state.hdr_enabled}
            onChange={toggle("hdr", setHdrEnabled)}
          />
        </PanelSectionRow>

        {state.hdr_enabled && (
          <>
            <PanelSectionRow>
              <SliderField
                label="Saturation"
                description={why("hdr_saturation",
                  state.hdr_saturation > state.hdr_soft_limit
                    ? `Past ${pct(state.hdr_soft_limit)} the strongest colors start ` +
                      "clipping and lose detail."
                    : "Raises color saturation without changing brightness.")}
                value={state.hdr_saturation}
                min={state.hdr_min}
                max={state.hdr_max}
                step={0.01}
                minimumDpadGranularity={25}
                onChange={slide("hdr_saturation", setHdrSaturation)}
              />
            </PanelSectionRow>
            <PanelSectionRow>
              <Field
                label="Now"
                description={
                  state.generating
                    ? "Preparing the colors…"
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
          <ActionButton
            description={why("reset")}
            onClick={() => queue.write("reset", resetAll, {
              patch: { sdr_enabled: false, hdr_enabled: false, sdr_saturation: 1, hdr_saturation: 1 },
              supersedes: ["sdr_saturation", "hdr_saturation", "sdr-toggle", "hdr-toggle"] })}
          >
            Back to defaults
          </ActionButton>
        </PanelSectionRow>
      </PanelSection>

      {aboutRow}
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
  name: NAME,
  titleView: <div className={staticClasses.Title}>{NAME}</div>,
  content: <Content />,
  icon: <VibranceIcon />,
  onDismount() {
    queue.dispose();
    updates.dispose();
    /* the backend restores both settings in _unload */
  },
}));
