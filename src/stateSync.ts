// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 Rayekkk

/** Shown while a write has not answered; the backend may still be working. */
export const WRITE_PENDING = "Backend write is still pending; waiting for its result";
/** Shown while a read has not answered; Decky replays it, so no second read is sent. */
export const READ_TIMED_OUT = "Error: Backend read timed out";

/** One write at a time; only the latest unsent value of each slider survives. */
export function createStateSync<T extends object>(
  read: () => Promise<T>,
  onError: (error: unknown) => void,
  readTimeoutMs = 10000,
  writeNoticeMs = 10000,
) {
  type Job = { id: number; patch: Partial<T>; write: () => Promise<T>;
    key?: keyof T; control?: string; resolve: () => void };
  type Control = { key: string; supersedes?: string[] };
  let active = false;
  let disposed = false;
  let epoch = 0;
  let observed: T | null = null;
  let writing = false;
  let reading: Promise<void> | null = null;
  // The outstanding read has timed out; what a finished write falls back to.
  let readTimedOut = false;
  const queue: Job[] = [];
  const edits = new Map<keyof T, { id: number; value: T[keyof T] }>();
  const listeners = new Set<{ render: (state: T) => void; connection: (message: string) => void }>();
  let connectionMessage = "";
  const reportConnection = (message: string) => {
    connectionMessage = message;
    if (!active) return;
    for (const listener of listeners) listener.connection(message);
  };

  const snapshot = () => {
    if (!observed) return null;
    const next = { ...observed };
    for (const [key, edit] of edits) next[key] = edit.value;
    return next;
  };
  const publish = () => {
    const next = snapshot();
    if (!active || !next) return;
    for (const listener of listeners) listener.render(next);
  };

  const refresh = (): Promise<void> => {
    if (!active || writing || queue.length) return Promise.resolve();
    if (reading) return reading;
    const started = epoch;
    let timer: ReturnType<typeof setTimeout>;
    // Decky retains unresolved calls and replays them on reconnect. A notice
    // timeout must not release the transport barrier and enqueue more reads.
    const timeout = new Promise<void>(resolve => {
      timer = setTimeout(() => {
        // Stored even while closed or superseded: this read still blocks every
        // other one, and a reopened panel must say so rather than spin.
        readTimedOut = true;
        reportConnection(READ_TIMED_OUT);
        resolve();
      }, readTimeoutMs);
    });
    const pending = (async () => {
      let failure: string | null = null;
      try {
        const state = await Promise.resolve().then(() => read());
        if (!active || writing || started !== epoch) return;
        observed = state;
        reportConnection("");
        publish();
      } catch (error) {
        failure = String(error);
      } finally {
        clearTimeout(timer!);
        // Released before the failure is shown, so the next poll can read.
        reading = null;
        readTimedOut = false;
        if (failure !== null && active && started === epoch) reportConnection(failure);
        // Any answer ends the timeout; one nobody is shown must not leave it behind.
        else if (connectionMessage === READ_TIMED_OUT) reportConnection("");
        if (active && started !== epoch && !writing) void refresh();
      }
    })();
    reading = Promise.race([pending, timeout]);
    return reading;
  };

  const finish = (job: Job) => {
    for (const key of Object.keys(job.patch) as (keyof T)[]) {
      if (edits.get(key)?.id === job.id) edits.delete(key);
    }
    job.resolve();
  };
  const pump = async () => {
    if (writing) return;
    writing = true;
    try {
      while (queue.length) {
        const job = queue.shift()!;
        // A slow/lost write reply is visible, but never let another write pass
        // it: the original backend operation may still commit.
        const timer = setTimeout(() => {
          // Stored even while closed, so reopening shows the value is unconfirmed.
          reportConnection(WRITE_PENDING);
        }, writeNoticeMs);
        try {
          observed = await job.write();
          reportConnection(readTimedOut ? READ_TIMED_OUT : "");
        } catch (error) {
          if (active) onError(error);
        } finally {
          clearTimeout(timer);
          // A failure while closed reports nothing, so the notice must not
          // outlive the write it was about.
          if (connectionMessage === WRITE_PENDING) reportConnection(readTimedOut ? READ_TIMED_OUT : "");
          finish(job);
          publish();
        }
      }
    } finally { writing = false; void refresh(); }
  };
  const enqueue = (patch: Partial<T>, write: () => Promise<T>,
    key?: keyof T, cancel: (keyof T)[] = [], control?: Control): Promise<void> => {
    if (disposed) return Promise.resolve();
    const id = ++epoch;
    for (let i = queue.length - 1; i >= 0; --i) {
      const queued = queue[i];
      const staleSlider = queued.key !== undefined && (queued.key === key || cancel.includes(queued.key));
      const staleControl = control && queued.control !== undefined
        && (queued.control === control.key || control.supersedes?.includes(queued.control));
      if (staleSlider || staleControl) {
        queue.splice(i, 1); finish(queued);
      }
    }
    for (const k of Object.keys(patch) as (keyof T)[]) edits.set(k, { id, value: patch[k]! });
    const promise = new Promise<void>(resolve => {
      const job = { id, patch, write, key, control: control?.key, resolve };
      const slider = key === undefined ? queue.findIndex(item => item.key !== undefined) : -1;
      if (slider < 0) queue.push(job); else queue.splice(slider, 0, job);
    });
    publish(); void pump();
    return promise;
  };

  return {
    refresh, snapshot,
    mutate<K extends keyof T>(key: K, value: T[K], write: () => Promise<T>) {
      const patch: Partial<T> = {};
      patch[key] = value;
      return enqueue(patch, write, key);
    },
    command(patch: Partial<T>, write: () => Promise<T>, cancel: (keyof T)[] = [], control?: Control) {
      return enqueue(patch, write, undefined, cancel, control);
    },
    subscribe(render: (state: T) => void, connection: (message: string) => void) {
      if (disposed) return () => undefined;
      const listener = { render, connection };
      listeners.add(listener);
      active = true; ++epoch;
      const next = snapshot();
      if (next) render(next);
      connection(connectionMessage);
      return () => {
        listeners.delete(listener);
        if (!listeners.size) { active = false; ++epoch; }
      };
    },
    dispose() {
      disposed = true; active = false; ++epoch;
      for (const job of queue.splice(0)) finish(job);
      listeners.clear();
    },
  };
}
