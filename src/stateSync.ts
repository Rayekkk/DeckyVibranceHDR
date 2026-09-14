// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 Rayekkk

/** Serialize edits and keep their optimistic values above older snapshots. */
export function createStateSync<T extends object>(
  read: () => Promise<T>,
  render: (state: T) => void,
  onError: (error: unknown) => void,
) {
  let active = false;
  let epoch = 0;
  let poll = 0;
  let pending = 0;
  let observed: T | null = null;
  let tail: Promise<void> = Promise.resolve();
  const edits = new Map<keyof T, { id: number; value: T[keyof T] }>();

  const publish = () => {
    if (!active || !observed) return;
    const next = { ...observed };
    for (const [key, edit] of edits) next[key] = edit.value;
    render(next);
  };

  const refresh = async () => {
    if (!active || pending) return;
    const started = epoch;
    const ticket = ++poll;
    try {
      const state = await read();
      if (!active || pending || started !== epoch || ticket !== poll) return;
      observed = state;
      publish();
    } catch {
      // Startup and temporary backend outages are retried by the next poll.
    }
  };

  const mutate = <K extends keyof T>(
    key: K, value: T[K], write: () => Promise<T>,
  ): Promise<void> => {
    const id = ++epoch;
    ++pending;
    edits.set(key, { id, value });
    publish();
    const run = async () => {
      try {
        observed = await write();
      } catch (error) {
        if (active) onError(error);
      } finally {
        if (edits.get(key)?.id === id) edits.delete(key);
        --pending;
        publish();
        // Refresh after the final edit, including an RPC whose reply was lost.
        if (!pending) void refresh();
      }
    };
    tail = tail.then(run, run);
    return tail;
  };

  return {
    refresh, mutate,
    activate() { active = true; ++epoch; },
    deactivate() { active = false; ++epoch; },
  };
}
