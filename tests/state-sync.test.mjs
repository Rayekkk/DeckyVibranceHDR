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

test("Decky zero-argument reads stay empty across initial polls and write refreshes", async () => {
  const calls = [], renders = [], connections = [];
  const sync = createStateSync(async (...args) => {
    // Decky's rest arguments are JSON-encoded before Python dispatch.
    const wireArgs = JSON.parse(JSON.stringify(args));
    calls.push(wireArgs);
    if (wireArgs.length) throw new Error("Plugin.get_state() takes 1 positional argument but 2 were given");
    return { value: 1 };
  }, state => renders.push(state), () => {}, message => connections.push(message));
  try {
    sync.activate();
    await sync.refresh();
    assert.deepEqual(calls, [[]]);
    assert.deepEqual(renders.at(-1), { value: 1 });
    assert.equal(connections.at(-1), "");
    await sync.mutate("value", 2, async () => ({ value: 2 }));
    await sync.refresh();
    assert.ok(calls.length >= 2);
    assert.ok(calls.every(args => args.length === 0));
    assert.equal(connections.at(-1), "");
  } finally { sync.dispose(); }
});

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

test("slow polls share one request and still publish its response", async () => {
  const f = await fixture(), old = deferred();
  let calls = 0;
  f.reader(() => { ++calls; return old.promise; }); const a = f.sync.refresh();
  const b = f.sync.refresh();
  old.resolve({ sdr: 1.5, hdr: 1, enabled: true }); await Promise.all([a, b]);
  assert.equal(calls, 1);
  assert.equal(f.latest().sdr, 1.5);
});

test("timed-out reads remain bounded and recover when Decky replays the pending call", async () => {
  const pending = deferred(), errors = [], renders = [];
  let calls = 0;
  const sync = createStateSync(() => { ++calls; return pending.promise; },
    state => renders.push(state), () => {}, message => errors.push(message), 5);
  try {
    sync.activate(); await sync.refresh();
    assert.match(errors.at(-1), /timed out/);
    for (let n = 0; n < 4; ++n) await sync.refresh();
    assert.equal(calls, 1, "Decky retains pending calls and replays each on reconnect");
    pending.resolve({ value: 2 });
    for (let n = 0; n < 8; ++n) await Promise.resolve();
    assert.deepEqual(renders.at(-1), { value: 2 });
    assert.equal(errors.at(-1), "");
  } finally { sync.dispose(); }
});

test("a settled failed read releases the barrier and a later read recovers", async () => {
  const errors = [], renders = [];
  let reader = async () => { throw new Error("connection lost"); };
  const sync = createStateSync(() => reader(), s => renders.push(s), () => {},
    message => errors.push(message), 5);
  sync.activate(); await sync.refresh();
  assert.match(errors.at(-1), /connection lost/);
  reader = async () => ({ value: 1 });
  await sync.refresh();
  assert.equal(errors.at(-1), "");
  assert.deepEqual(renders.at(-1), { value: 1 });
  sync.deactivate();
});

test("200 slider moves send only the in-flight value and latest unsent value", async () => {
  const f = await fixture(), wait = deferred();
  const calls = [], pending = [];
  for (let n = 0; n < 200; ++n) pending.push(f.sync.mutate("sdr", n,
    async () => { calls.push(n); if (n === 0) await wait.promise; return f.write({ sdr: n }); }));
  assert.equal(f.latest().sdr, 199);
  wait.resolve(); await Promise.all(pending);
  assert.deepEqual(calls, [0, 199]);
});

test("disable cancels unsent moves and carries the newest slider value", async () => {
  const f = await fixture(), wait = deferred(), calls = [];
  const first = f.sync.mutate("sdr", 1.1, async () => {
    calls.push("first"); await wait.promise; return f.write({ sdr: 1.1 });
  });
  const slider = f.sync.mutate("sdr", 1.5, async () => { calls.push("stale"); return f.write({ sdr: 1.5 }); });
  const value = f.sync.snapshot().sdr;
  const off = f.sync.command({ enabled: false, sdr: value }, async () => {
    calls.push("off"); return f.write({ enabled: false, sdr: value });
  }, ["sdr"]);
  assert.equal(f.latest().enabled, false);
  wait.resolve(); await Promise.all([first, slider, off]);
  assert.deepEqual(calls, ["first", "off"]);
  assert.deepEqual(f.latest(), { sdr: 1.5, hdr: 1, enabled: false });
});

test("reset optimistically updates all controls and cancels both sliders", async () => {
  const f = await fixture(), wait = deferred();
  const a = f.sync.mutate("sdr", 1.4, async () => { await wait.promise; return f.write({ sdr: 1.4 }); });
  const b = f.sync.mutate("hdr", 1.3, async () => { throw new Error("must be cancelled"); });
  const c = f.sync.command({ sdr: 1, hdr: 1, enabled: false }, async () =>
    f.write({ sdr: 1, hdr: 1, enabled: false }), ["sdr", "hdr"]);
  assert.deepEqual(f.latest(), { sdr: 1, hdr: 1, enabled: false });
  wait.resolve(); await Promise.all([a, b, c]);
  assert.equal(f.errors.length, 0);
});

/* A late response is already covered by the mutation epoch test above. */
test("a read failure reports the connection error", async () => {
  const errors = [];
  const sync = createStateSync(async () => { throw new Error("offline"); }, () => {},
    () => {}, message => errors.push(message));
  sync.activate(); await sync.refresh(); sync.deactivate();
  assert.match(errors[0], /offline/);
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

test("slow writes report their status and cannot be overtaken", async () => {
  const wait = deferred(), errors = [], calls = [];
  const sync = createStateSync(async () => ({ value: 0 }), () => {}, () => {},
    message => errors.push(message), 100, 5);
  sync.activate(); await sync.refresh();
  const a = sync.mutate("value", 1, async () => { calls.push(1); await wait.promise; return { value: 1 }; });
  const b = sync.command({ value: 0 }, async () => { calls.push(0); return { value: 0 }; });
  await new Promise(resolve => setTimeout(resolve, 10));
  assert.match(errors.at(-1), /pending/);
  assert.deepEqual(calls, [1]);
  wait.resolve(); await Promise.all([a, b]);
  assert.deepEqual(calls, [1, 0]);
  assert.equal(errors.at(-1), "");
  sync.deactivate();
});

test("reopening QAM shares the queue and cannot resurrect old slider requests", async () => {
  let backend = { value: 1, enabled: true };
  const wait = deferred(), calls = [], oldRenders = [], freshRenders = [];
  const sync = createStateSync(async () => ({ ...backend }), () => {}, () => {});
  const close = sync.subscribe(s => oldRenders.push(s), () => {});
  await sync.refresh();
  const first = sync.mutate("value", 1.1, async () => {
    calls.push("first"); await wait.promise; return backend = { ...backend, value: 1.1 };
  });
  const oldSlider = sync.mutate("value", 1.4, async () => {
    calls.push("old slider"); return backend = { ...backend, value: 1.4 };
  });
  close();
  const oldCount = oldRenders.length;
  const closeNew = sync.subscribe(s => freshRenders.push(s), () => {});
  assert.equal(freshRenders.at(-1).value, 1.4);
  const latest = sync.command({ value: 1.4, enabled: false }, async () => {
    calls.push("off"); return backend = { value: 1.4, enabled: false };
  }, ["value"]);
  wait.resolve(); await Promise.all([first, oldSlider, latest]);
  assert.deepEqual(calls, ["first", "off"]);
  assert.equal(oldRenders.length, oldCount);
  assert.deepEqual(freshRenders.at(-1), { value: 1.4, enabled: false });
  closeNew();
});

test("plugin disposal stops queued and future RPCs", async () => {
  const f = await fixture(), wait = deferred(), calls = [];
  const a = f.sync.mutate("sdr", 1.1, async () => { calls.push(1); await wait.promise; return f.write({ sdr: 1.1 }); });
  const b = f.sync.mutate("sdr", 1.4, async () => { calls.push(2); return f.write({ sdr: 1.4 }); });
  f.sync.dispose();
  const count = f.renders.length;
  await f.sync.mutate("sdr", 1.5, async () => { calls.push(3); return f.write({ sdr: 1.5 }); });
  wait.resolve(); await Promise.all([a, b]);
  assert.deepEqual(calls, [1]);
  assert.equal(f.renders.length, count);
});

test("rapid toggles keep only the latest unsent control intent", async () => {
  const f = await fixture(), wait = deferred(), calls = [], pending = [];
  for (let n = 0; n < 200; ++n) {
    const enabled = n % 2 === 0;
    pending.push(f.sync.command({ enabled, sdr: 1.4 }, async () => {
      calls.push(n); if (n === 0) await wait.promise;
      return f.write({ enabled, sdr: 1.4 });
    }, ["sdr"], { key: "sdr-toggle" }));
  }
  assert.equal(f.latest().enabled, false);
  wait.resolve(); await Promise.all(pending);
  assert.deepEqual(calls, [0, 199]);
  f.sync.deactivate();
});

test("reset supersedes queued toggles as well as slider moves", async () => {
  const f = await fixture(), wait = deferred(), calls = [];
  const first = f.sync.mutate("sdr", 1.2, async () => {
    await wait.promise; calls.push("slider"); return f.write({ sdr: 1.2 });
  });
  const toggle = f.sync.command({ enabled: true, sdr: 1.4 }, async () => {
    calls.push("toggle"); return f.write({ enabled: true, sdr: 1.4 });
  }, ["sdr"], { key: "sdr-toggle" });
  const reset = f.sync.command({ enabled: false, sdr: 1, hdr: 1 }, async () => {
    calls.push("reset"); return f.write({ enabled: false, sdr: 1, hdr: 1 });
  }, ["sdr", "hdr"], { key: "reset", supersedes: ["sdr-toggle", "hdr-toggle"] });
  wait.resolve(); await Promise.all([first, toggle, reset]);
  assert.deepEqual(calls, ["slider", "reset"]);
  assert.deepEqual(f.latest(), { sdr: 1, hdr: 1, enabled: false });
  f.sync.deactivate();
});

test("a toggle after reset preserves reset and the other half's intent", async () => {
  const f = await fixture(), wait = deferred(), calls = [];
  const first = f.sync.mutate("sdr", 1.2, async () => {
    await wait.promise; return f.write({ sdr: 1.2 });
  });
  const reset = f.sync.command({ enabled: false, sdr: 1, hdr: 1 }, async () => {
    calls.push("reset"); return f.write({ enabled: false, sdr: 1, hdr: 1 });
  }, ["sdr", "hdr"], { key: "reset", supersedes: ["sdr-toggle", "hdr-toggle"] });
  const hdr = f.sync.command({ hdr: 1.3 }, async () => {
    calls.push("hdr"); return f.write({ hdr: 1.3 });
  }, ["hdr"], { key: "hdr-toggle" });
  const on = f.sync.command({ enabled: true, sdr: 1 }, async () => {
    calls.push("on"); return f.write({ enabled: true, sdr: 1 });
  }, ["sdr"], { key: "sdr-toggle" });
  wait.resolve(); await Promise.all([first, reset, hdr, on]);
  assert.deepEqual(calls, ["reset", "hdr", "on"]);
  assert.deepEqual(f.latest(), { enabled: true, sdr: 1, hdr: 1.3 });
  f.sync.deactivate();
});

test("commands without a coalescing identity keep all side effects", async () => {
  const f = await fixture(), wait = deferred(), calls = [];
  const first = f.sync.mutate("sdr", 1.2, async () => { await wait.promise; return f.write({ sdr: 1.2 }); });
  const commands = Array.from({ length: 5 }, (_, n) => f.sync.command({}, async () => {
    calls.push(n); return f.write({});
  }));
  wait.resolve(); await Promise.all([first, ...commands]);
  assert.deepEqual(calls, [0, 1, 2, 3, 4]);
  f.sync.deactivate();
});

test("reopen and reset during a timed-out read keep one call and ignore its stale reply", async () => {
  const pending = deferred(), screens = [];
  let backend = { value: 1 }, calls = 0;
  const sync = createStateSync(() => ++calls === 1 ? pending.promise : Promise.resolve({ ...backend }),
    () => {}, () => {}, () => {}, 5);
  try {
    const close = sync.subscribe(s => screens.push(s), () => {});
    await sync.refresh();
    close();
    sync.subscribe(s => screens.push(s), () => {});
    await sync.refresh();
    await sync.command({ value: 0 }, async () => backend = { value: 0 });
    await sync.refresh();
    assert.equal(calls, 1);
    pending.resolve({ value: 9 });
    for (let i = 0; i < 16; ++i) await Promise.resolve();
    assert.equal(calls, 2);
    assert.deepEqual(screens.at(-1), { value: 0 });
    assert.ok(screens.every(s => s.value !== 9));
  } finally { sync.dispose(); }
});
