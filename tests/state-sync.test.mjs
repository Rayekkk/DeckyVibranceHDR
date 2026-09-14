// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 Rayekkk
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import ts from "typescript";

// Exercise the production helper without React/Steam and without build output.
const source = readFileSync(new URL("../src/stateSync.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.ESNext },
});
const { createStateSync } = await import(
  `data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`);

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

async function fixture() {
  let backend = { sdr: 1, hdr: 1, enabled: true };
  let reader = async () => ({ ...backend });
  const renders = [], errors = [];
  const sync = createStateSync(() => reader(), s => renders.push(s), e => errors.push(e));
  sync.activate();
  await sync.refresh();
  return {
    sync, renders, errors,
    latest: () => renders.at(-1),
    reader(fn) { reader = fn; },
    write(delta) { backend = { ...backend, ...delta }; return { ...backend }; },
  };
}

test("an old poll cannot replace a completed slider change", async () => {
  const f = await fixture(), old = deferred();
  f.reader(() => old.promise);
  const refresh = f.sync.refresh();
  f.reader(async () => f.write({}));
  await f.sync.mutate("sdr", 1.5, async () => f.write({ sdr: 1.5 }));
  old.resolve({ sdr: 1, hdr: 1, enabled: true });
  await refresh;
  assert.equal(f.latest().sdr, 1.5);
});

test("queued slider responses preserve the newest optimistic edit", async () => {
  const f = await fixture(), first = deferred(), second = deferred();
  const calls = [];
  const a = f.sync.mutate("sdr", 1.2, async () => {
    calls.push(1); await first.promise; return f.write({ sdr: 1.2 });
  });
  const b = f.sync.mutate("sdr", 1.5, async () => {
    calls.push(2); await second.promise; return f.write({ sdr: 1.5 });
  });
  await Promise.resolve();
  assert.deepEqual(calls, [1]);
  assert.equal(f.latest().sdr, 1.5);
  first.resolve(); await a;
  assert.equal(f.latest().sdr, 1.5);
  second.resolve(); await b;
  assert.deepEqual(calls, [1, 2]);
  assert.equal(f.latest().sdr, 1.5);
});

test("a response for one half preserves pending edits to the other", async () => {
  const f = await fixture(), wait = deferred();
  const a = f.sync.mutate("sdr", 1.5, async () => f.write({ sdr: 1.5 }));
  const b = f.sync.mutate("hdr", 1.3, async () => {
    await wait.promise; return f.write({ hdr: 1.3 });
  });
  await a;
  assert.deepEqual(f.latest(), { sdr: 1.5, hdr: 1.3, enabled: true });
  wait.resolve(); await b;
});

test("failed RPC does not block later edits or leave the optimistic value stuck", async () => {
  const f = await fixture();
  const a = f.sync.mutate("sdr", 1.5, async () => { throw new Error("offline"); });
  const b = f.sync.mutate("enabled", false, async () => f.write({ enabled: false }));
  await Promise.all([a, b]);
  assert.equal(f.errors.length, 1);
  assert.deepEqual(f.latest(), { sdr: 1, hdr: 1, enabled: false });
});

test("only the newest overlapping poll is accepted", async () => {
  const f = await fixture(), old = deferred(), fresh = deferred();
  f.reader(() => old.promise); const a = f.sync.refresh();
  f.reader(() => fresh.promise); const b = f.sync.refresh();
  fresh.resolve({ sdr: 1.5, hdr: 1, enabled: true }); await b;
  old.resolve({ sdr: 1, hdr: 1, enabled: true }); await a;
  assert.equal(f.latest().sdr, 1.5);
});

test("unmounted content ignores late snapshots", async () => {
  const f = await fixture(), old = deferred();
  f.reader(() => old.promise);
  const refresh = f.sync.refresh();
  const count = f.renders.length;
  f.sync.deactivate();
  old.resolve({ sdr: 1.5, hdr: 1, enabled: true }); await refresh;
  assert.equal(f.renders.length, count);
});
