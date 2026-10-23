// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 Rayekkk
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const read = name => readFileSync(new URL(`../${name}`, import.meta.url), "utf8");

// The other tests answer to any route name, so this is the only place where
// the names and argument counts the panel calls are held against main.py.
test("every callable of the panel is a backend method that takes that many arguments", () => {
  const calls = [...read("src/index.tsx").matchAll(/callable<\s*\[([^\]]*)\][^>]*>\(\s*"(\w+)"\s*\)/g)]
    .map(([, args, name]) => ({ name, given: args.split(",").filter(arg => arg.trim()).length }));
  assert.ok(calls.length > 0, "no callable<...>(\"name\") found in src/index.tsx");
  // shortcut: signatures on one line, no commas inside annotations; revisit if main.py grows either.
  const body = read("main.py").split(/^class Plugin:\n/m)[1].split(/^\S/m)[0];
  const methods = new Map([...body.matchAll(/^    async def ([a-z]\w*)\(self((?:,[^)]*)?)\)/gm)].map(([, name, params]) => {
    const list = params.split(",").filter(param => param.trim());
    return [name, { required: list.filter(param => !param.includes("=")).length, all: list.length }];
  }));
  assert.ok(methods.size > 0, "no public async methods found in the Plugin class of main.py");
  for (const { name, given } of calls) {
    const method = methods.get(name);
    assert.ok(method, `the panel calls ${name}, which main.py does not define`);
    assert.ok(given >= method.required && given <= method.all,
      `${name}: the panel passes ${given} argument(s), main.py takes ${method.required} to ${method.all}`);
  }
});
