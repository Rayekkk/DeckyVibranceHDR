// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 Rayekkk
// https://github.com/Rayekkk/DeckyVibranceHDR

// The Updates section: DeckyKit's shared updater checks GitHub and saves the verified release ZIP to Downloads;
// the user installs it by hand.
import { callable, toaster } from "@decky/api";
import { ButtonItem, Field, PanelSection, PanelSectionRow } from "@decky/ui";
import { FC, useCallback, useEffect, useRef, useState } from "react";
import { Result, settle } from "./result";

export interface UpdateInfo extends Result<never> {
  current_version?: string;
  latest_version?: string;
  update_available?: boolean;
  download_available?: boolean;
}

const getVersion = callable<[], Result<never> & { version?: string }>("get_version");
const checkForUpdates = callable<[], UpdateInfo>("check_for_updates");
const performUpdate = callable<[string], Result<never> & { path?: string; version?: string }>("perform_update");

export const notify = (title: string, body: string) => toaster.toast({ title, body, duration: 5000 });

// Operations and results outlive the QAM component. A reopened panel subscribes
// to the same request rather than starting another or losing a completed file.
let updates = { info: null as UpdateInfo | null, version: "", checking: false, downloading: false,
  downloadPath: null as string | null, downloadVersion: "" };
const updateListeners = new Set<(state: typeof updates) => void>();
let updatesDisposed = false;
let versionReading: Promise<void> | null = null;
const updateSnapshot = (patch: Partial<typeof updates>) => {
  if (updatesDisposed) return;
  updates = { ...updates, ...patch };
  for (const listener of updateListeners) listener(updates);
};
const updateError = (title: string, message: string) => {
  updateSnapshot({ info: { ...updates.info, ok: false, error: message } });
  if (!updatesDisposed && updateListeners.size) notify(title, message);
};
const readVersion = () => {
  if (updatesDisposed || versionReading || updates.version) return;
  versionReading = (async () => {
    const result = await settle(getVersion());
    if (result.ok) updateSnapshot({ version: result.version ?? "" });
    versionReading = null;  // a failed read is tried again by a later opening
  })();
};
const checkUpdate = async () => {
  if (updatesDisposed || updates.checking || updates.downloading) return;
  updateSnapshot({ checking: true, info: null });
  const info = await settle(checkForUpdates());
  if (info.ok) {
    // Keep a finished download while it is still the release on offer.
    const same = !!updates.downloadVersion && info.latest_version === updates.downloadVersion;
    updateSnapshot({ info, downloadPath: same ? updates.downloadPath : null });
  } else {
    // A failed check offers nothing new, so a finished download stays.
    updateError("Update check failed", info.error ?? "Unknown error");
  }
  updateSnapshot({ checking: false });
};
const downloadUpdate = async () => {
  const info = updates.info;
  if (updatesDisposed || updates.checking || updates.downloading || !info?.download_available
      || !info.latest_version) return;
  updateSnapshot({ downloading: true, info: { ...info, error: undefined } });
  const result = await settle(performUpdate(info.latest_version));
  if (result.ok && result.path) updateSnapshot({ downloadPath: result.path, downloadVersion: result.version ?? "" });
  else updateError("Download failed", result.error ?? "Unknown error");
  updateSnapshot({ downloading: false });
};

/** The plugin is unloading: nothing more is shown or started. */
export const disposeUpdates = () => {
  updatesDisposed = true;
  updateListeners.clear();
};

export const UpdateSection: FC = () => {
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
  const failed = !!info && !info.ok;

  return (
    <PanelSection title="Updates">
      <PanelSectionRow>
        <Field label="Installed" focusable>
          {`v${info?.current_version || version || "?"}`}
        </Field>
      </PanelSectionRow>

      {info?.latest_version && !failed && (
        <PanelSectionRow>
          <Field label="Latest" focusable>{`v${info.latest_version}`}</Field>
        </PanelSectionRow>
      )}

      {failed && (
        <PanelSectionRow>
          <Field label="Error" description={info.error || "Unknown error"} focusable />
        </PanelSectionRow>
      )}

      {/* A release that exists but cannot be offered (or none at all) says why instead of "Up to date". */}
      {info && !failed && info.message && !downloadPath && (
        <PanelSectionRow>
          <Field label="Not offered" description={info.message} focusable />
        </PanelSectionRow>
      )}

      {info && !failed && !info.message && !info.update_available && !downloadPath && (
        <PanelSectionRow>
          <Field label="Up to date" focusable />
        </PanelSectionRow>
      )}

      {/* Kept after a failed download, so it can be tried again. */}
      {info?.download_available && !downloadPath && (
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
