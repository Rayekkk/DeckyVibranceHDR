// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 Rayekkk

/** One write at a time; only the latest unsent value of each slider survives. */
export function createStateSync<T extends object>(
  read: () => Promise<T>,
  render: (state: T) => void,
  onError: (error: unknown) => void,
  connection: (message: string) => void = () => undefined,
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
  const queue: Job[] = [];
  const edits = new Map<keyof T, { id: number; value: T[keyof T] }>();
  const listeners = new Set<{ render: (state: T) => void; connection: (message: string) => void }>();
  let connectionMessage = "";
  const reportConnection = (message: string) => {
    connectionMessage = message;
    if (!active) return;
    connection(message);
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
    render(next);
    for (const listener of listeners) listener.render(next);
  };

  const refresh = (): Promise<void> => {
    if (!active || writing || queue.length) return Promise.resolve();
    if (reading) return reading;
    const started = epoch;
    let timer: ReturnType<typeof setTimeout>;
    const timeout = new Promise<never>((_, reject) => {
      timer = setTimeout(() => reject(new Error("Backend read timed out")), readTimeoutMs);
    });
    reading = (async () => {
      try {
        // A Promise callback receives its resolved value. Decky's callable
        // forwards every argument to Python, including an implicit undefined.
        const state = await Promise.race([Promise.resolve().then(() => read()), timeout]);
        if (!active || writing || started !== epoch) return;
        observed = state;
        reportConnection("");
        publish();
      } catch (error) {
        if (active && started === epoch) reportConnection(String(error));
      } finally {
        clearTimeout(timer!);
        reading = null;
        if (active && started !== epoch && !writing) void refresh();
      }
    })();
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
          if (active) reportConnection("Backend write is still pending; waiting for its result");
        }, writeNoticeMs);
        try {
          observed = await job.write();
          reportConnection("");
        } catch (error) {
          if (active) onError(error);
        } finally {
          clearTimeout(timer);
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
    activate() { if (!disposed) { active = true; ++epoch; publish(); } },
    deactivate() { active = false; ++epoch; },
  };
}
