// SPDX-License-Identifier: BSD-3-Clause
// Shared from DeckyKit (shared/frontend/writeQueue.ts): change it there, then `kit sync`.
//
// The panel's view of the backend state and the one road its changes take there.
//
// Sources: DeckyVibranceHDR src/stateSync.ts (one write at a time and never overtaken, latest unsent value per key,
// writes that drop queued ones, edits shown at once on top of the backend state until their own write answers, one
// read at a time that Decky replays itself, stale reads dropped, slow-call notices, refusals toasted even with the
// panel closed), LegionGo2Companion src/shared.tsx mutationQueue and rgb.tsx (strict order, a write releases the
// debounced ones before it, pending values sent when the panel closes), DeckyEQ src/controller.ts (debounce, a
// snapshot store for React, D2 answers, a flush the caller can wait for, an unchanged poll that re-renders nothing),
// DeckyLS src/index.tsx (the time of the last good read for staleNote), docs/UI.md §4–§5 (a refusal under its control).
// Guarantees:
// - One write at a time, in the order they were made; a write that never answers is waited for, never overtaken
//   (the backend may still commit it), and `slow` says so after slowMs.
// - A new value for a key replaces its unsent one and goes to the back; `supersedes` drops the unsent writes of
//   other keys (a reset drops queued slider moves). A dropped write's promise answers with its replacement's result.
// - delayMs debounces a key: sent once it has been still that long. flush() releases every debounced write at
//   once (the panel is closing, or an export must see the latest edits) and resolves once every write made so far
//   has answered; a write without delay releases the debounced ones before it, so the order the user acted in is
//   kept.
// - Calls get no argument from the queue (a callable passed as `write(key, resetAll)` sends exactly what it is
//   given). Every call goes through settle(): a throwing call, a dead backend or a missing answer is { ok: false, error }
//   and the queue goes on. A write's state becomes the base; a refusal (ok: false) goes to onError, also with no
//   subscriber (Steam's toaster is global), and its state, when it has one, is shown.
// - A refused write's text stays in `refused[key]` (shown under that control, UI.md §5) until the next write of that
//   key is made; reads and other keys never clear it.
// - `readAt` is when the backend last sent its state (a read or a write's answer), for staleNote; it is exact
//   whenever `error` is set (an unchanged poll publishes nothing, so it may lag while all is well).
// - Edits (patch) are shown on top of the base at once and each stays until its own write answers, so a later move
//   never flickers back to an older answer.
// - read(): skipped while any write is queued or running; one read at a time (a second call returns the running
//   one, never a second request: Decky replays unanswered calls); a read that started before the newest write
//   is dropped; its error stays in `error` until a read or write succeeds. A read that brings nothing new publishes
//   nothing (a poll every second must not re-render the panel).
// - get() returns the same object until something changes (React's useSyncExternalStore needs that).
// - After dispose() no call starts, queued writes are dropped and nothing is published.
// Deliberately not here: retries and timeouts that give up on a call (Decky replays, the backend may commit),
// revision checks (a plugin can put one in its own call), polling and React hooks (the plugin's panel), texts.
import { Result, settle } from "./result";

export interface QueueView<T> {
  /** The backend's state with the edits not answered yet on top; null before the first answer. */
  state: T | null;
  /** Why the last read failed; "" once a read or write succeeds. */
  error: string;
  /** A call has not answered within slowMs (it is still waited for). */
  slow: "" | "read" | "write";
  /** Why the last write of each key was refused, until that key is written again (the text under its control). */
  refused: Readonly<Record<string, string>>;
  /** Date.now() of the backend's last state (0 before the first): staleNote(Date.now() - readAt) once `error` is set. */
  readAt: number;
}

export interface WriteOptions<T> {
  /** Shown at once on top of the backend state, until this write answers. */
  patch?: Partial<T>;
  /** Send only once this key has been still this long (sliders). */
  delayMs?: number;
  /** Keys whose unsent writes this one makes pointless. */
  supersedes?: string[];
}

type Job<T> = {
  id: number; key?: string; call: () => Promise<Result<T>>; patch: Partial<T>; ready: boolean;
  timer?: ReturnType<typeof setTimeout>; waiters: ((r: Result<T>) => void)[];
};

export function createWriteQueue<T extends object>(read: () => Promise<Result<T>>, options: {
  slowMs?: number; onError?: (result: Result<T>) => void;
} = {}) {
  const slowMs = options.slowMs ?? 10000;
  const jobs: Job<T>[] = [];
  const edits = new Map<keyof T, { id: number; value: T[keyof T] }>();
  const listeners = new Set<() => void>();
  let base: T | null = null, error = "", slowWrite = false, slowRead = false, readAt = 0;
  let refused: Record<string, string> = {};
  let gen = 0, writing = false, disposed = false, running: Job<T> | null = null;
  let reading: Promise<void> | null = null;
  let view: QueueView<T> = { state: null, error: "", slow: "", refused, readAt };

  const publish = () => {
    if (disposed) return;
    let state = base;
    if (state && edits.size) {
      state = { ...state };
      for (const [key, edit] of edits) state[key] = edit.value;
    }
    view = { state, error, slow: slowWrite ? "write" : slowRead ? "read" : "", refused, readAt };
    for (const listener of [...listeners]) listener();
  };
  const unpatch = (job: Job<T>) => {
    for (const key of Object.keys(job.patch) as (keyof T)[]) if (edits.get(key)?.id === job.id) edits.delete(key);
  };
  const drop = (job: Job<T>, into?: Job<T>) => {
    clearTimeout(job.timer);
    unpatch(job);
    if (into) into.waiters.push(...job.waiters);
    else for (const done of job.waiters) done({ ok: false, error: "The panel was closed before this was sent." });
  };
  const release = (upTo: number) => {
    for (const job of jobs.slice(0, upTo)) { clearTimeout(job.timer); job.ready = true; }
  };

  const pump = async () => {
    if (writing || disposed) return;
    writing = true;
    try {
      while (jobs.length && jobs[0].ready && !disposed) {
        const job = running = jobs.shift()!;
        const timer = setTimeout(() => { slowWrite = true; publish(); }, slowMs);
        // Called with no argument: `.then(job.call)` would hand the callable undefined, which Decky sends on.
        const result = await settle(Promise.resolve().then(() => job.call()));
        clearTimeout(timer);
        slowWrite = false;
        if (result.state) { base = result.state; readAt = Date.now(); }
        if (result.ok) error = "";
        else if (job.key !== undefined) refused = { ...refused, [job.key]: result.error || "Unknown error" };
        if (!result.ok && !disposed) {
          try { options.onError?.(result); } catch (e) { console.error(e); }
        }
        unpatch(job);
        running = null;
        for (const done of job.waiters) done(result);
        publish();
      }
    } finally {
      writing = false;
      running = null;
    }
  };

  const write = (key: string | undefined, call: () => Promise<Result<T>>, opts: WriteOptions<T> = {}) => {
    if (disposed) return Promise.resolve<Result<T>>({ ok: false, error: "The panel was closed." });
    const job: Job<T> = { id: ++gen, key, call, patch: opts.patch ?? {}, ready: !opts.delayMs, waiters: [] };
    const promise = new Promise<Result<T>>(resolve => job.waiters.push(resolve));
    if (key !== undefined && key in refused) {  // the next action on a control clears its text
      refused = { ...refused };
      delete refused[key];
    }
    for (let i = jobs.length - 1; i >= 0; --i) {
      const queued = jobs[i];
      if (queued.key !== undefined && (queued.key === key || opts.supersedes?.includes(queued.key))) {
        jobs.splice(i, 1);
        drop(queued, job);
      }
    }
    for (const field of Object.keys(job.patch) as (keyof T)[]) edits.set(field, { id: job.id, value: job.patch[field]! });
    jobs.push(job);
    if (job.ready) release(jobs.length);
    else job.timer = setTimeout(() => { job.ready = true; void pump(); }, opts.delayMs);
    publish();
    void pump();
    return promise;
  };

  const refresh = (): Promise<void> => {
    if (disposed || writing || jobs.length) return Promise.resolve();
    if (reading) return reading;
    const started = gen;
    const timer = setTimeout(() => { slowRead = true; publish(); }, slowMs);
    reading = (async () => {
      const result = await settle(Promise.resolve().then(() => read()));
      clearTimeout(timer);
      const wasSlow = slowRead;
      slowRead = false;
      reading = null;
      if (started === gen && !writing && !jobs.length) {
        if (result.ok && result.state) {
          readAt = Date.now();
          if (!wasSlow && !error && base && JSON.stringify(result.state) === JSON.stringify(base)) return;
          base = result.state;
          error = "";
        } else error = result.error || "The backend sent no state.";
      }
      publish();
    })();
    return reading;
  };

  return {
    write,
    read: refresh,
    /** Send every debounced write now; resolves once every write made so far has answered. */
    flush(): Promise<void> {
      release(jobs.length);
      // Every write so far: a later edit may move a value queued now behind the last one (a new value goes last).
      const done = Promise.all([...jobs, ...(running ? [running] : [])].map(job =>
        new Promise<void>(resolve => job.waiters.push(() => resolve()))));
      void pump();
      return done.then(() => undefined);
    },
    get: () => view,
    subscribe(listener: () => void) {
      listeners.add(listener);
      return () => { listeners.delete(listener); };
    },
    dispose() {
      disposed = true;
      for (const job of jobs.splice(0)) drop(job);
      listeners.clear();
    },
  };
}
