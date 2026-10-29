// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 Rayekkk
// The panel's real TSX with controlled hooks, RPC calls and timers (DeckyKit's frontend_harness.cjs).
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const assert = require('node:assert/strict');
const { harness, all, find, settle } = require('./frontend_harness.cjs');

/** A complete get_state reply (every key of PluginState.public); tests override what they need. */
const baseState = { ready: true, reason: 'ready', sdr_enabled: true, hdr_enabled: true,
  sdr_saturation: 1, hdr_saturation: 1, sdr_applied: true, sdr_look_applied: false, hdr_applied: false,
  hdr_applied_saturation: null, sdr_look_saturation: null, sdr_error: '', sdr_look_error: '',
  hdr_error: '', settings_error: '', settings_notice: '', settings_loaded: true, generating: false,
  sdr_generating: false, conflict: '', external_display: false, hdr_supported: true,
  sdr_min: 1, sdr_max: 3, sdr_soft_limit: 2, hdr_min: 1, hdr_max: 1.5, hdr_soft_limit: 1.3 };
const ok = (over = {}) => ({ ok: true, state: { ...baseState, ...over } });

/** The panel, opened and answered once with `over` on top of baseState. */
const KEEP = ['UpdateSection', 'SectionLink', 'AboutPage', 'PageHeader'];
const VERSION = { ok: true, version: '1.2.0', home: '/home/deck' };
async function open(over = {}) {
  const h = harness({ keep: KEEP, responses: { get_version: VERSION } });
  h.render();
  await settle();
  h.respond('get_state', ok(over));
  await settle();
  return h;
}
/** The next poll (at most 3 s away) answered with `over`; the new tree. */
async function show(h, over = {}) {
  h.advance(3000);
  await settle();
  h.respond('get_state', ok(over));
  await settle();
  return h.render();
}
const titles = tree => all(tree).filter(n => n.type === 'PanelSection').map(n => n.props.title);
const labels = tree => all(tree).filter(n => n.type === 'Field').map(n => n.props.label);
const fields = (tree, label) => all(tree).filter(n => n.type === 'Field' && n.props.label === label);
const sliders = tree => all(tree).filter(n => n.type === 'SliderField');
const toggles = tree => all(tree).filter(n => n.type === 'ToggleField');
const writes = h => h.calls.filter(c => c.name !== 'get_state' && c.name !== 'get_version').map(c => [c.name, ...c.args]);
const aboutRows = tree => all(tree).filter(n => n.type?.name === 'SectionLink');
const aboutSections = tree => all(tree).filter(n => n.type === 'PanelSection' && n.key !== undefined).map(n => n.key);
const reads = h => h.calls.filter(c => c.name === 'get_state').length;

test('baseState has exactly the keys of the backend public state', () => {
  const source = fs.readFileSync(path.join(__dirname, '..', 'vibrance_state.py'), 'utf8');
  const block = source.split('self.public = {\n')[1].split(/^ {8}\}/m)[0];
  const keys = [...block.matchAll(/^ {12}"(\w+)":/gm)].map(m => m[1]);
  assert.ok(keys.length > 0, 'no keys found in PluginState.reset');
  assert.deepEqual(Object.keys(baseState).sort(), keys.sort());
});

test('each section and row appears only when its state asks for it', async () => {
  const h = await open();
  let tree = h.render();
  assert.deepEqual(titles(tree), ['SDR saturation', 'HDR saturation', 'Reset', undefined]);
  assert.deepEqual(labels(tree), ['Now', 'Now']);
  assert.deepEqual(sliders(tree).map(n => n.props.step), [0.01, 0.01]);

  tree = await show(h, { hdr_error: 'no LUT' });
  assert.deepEqual(labels(tree).slice(0, 2), ['HDR', 'What happens now']);
  assert.equal(fields(tree, 'What happens now')[0].props.description, 'The plugin retries on its own. '
    + 'If this stays, switch the effect off and on again, and check that Game Mode is running.');
  tree = await show(h, { sdr_error: 'could not write SDR to gamescope' });
  assert.deepEqual(labels(tree).slice(0, 2), ['SDR', 'What happens now']);
  tree = await show(h, { settings_error: 'could not save settings: disk full' });
  assert.equal(titles(tree)[0], 'Connection and settings');
  assert.deepEqual(labels(tree).slice(0, 2), ['Settings not saved', 'Now']);
  tree = await show(h, { conflict: 'vibrantDeck', sdr_look_error: 'no LUT' });
  assert.ok(titles(tree).includes('Heads up'));
  assert.equal(fields(tree, 'Conflict')[0].props.description, 'vibrantDeck');
  assert.equal(fields(tree, 'SDR look')[0].props.description, 'no LUT');

  tree = await show(h, { external_display: true });
  assert.deepEqual(titles(tree), ['External display', 'Reset', undefined]);
  assert.equal(fields(tree, 'Standing down')[0].props.description, 'Settings here are tuned for the '
    + 'built-in screen, so nothing is applied to an external display. Unplug it and your saved settings apply again.');
  tree = await show(h, { hdr_supported: false });
  assert.ok(titles(tree).includes('SDR saturation') && !titles(tree).includes('HDR saturation'));
  tree = await show(h, { sdr_enabled: false, hdr_enabled: false });
  assert.equal(sliders(tree).length, 0);

  // Not ready: the settings failure takes the top row only when it is the reason.
  const failure = 'could not load settings: denied';
  tree = await show(h, { ready: false, reason: failure, settings_error: failure, settings_loaded: false });
  assert.deepEqual(labels(tree), ['Could not load settings']);
  assert.deepEqual(aboutRows(tree).map(n => n.props.title), ['About'], 'updates stay reachable while not ready');
  tree = await show(h, { ready: false, reason: 'waiting for gamescope', settings_error: failure, settings_loaded: false });
  assert.deepEqual(labels(tree), ['Plugin not ready', 'Could not load settings']);
  assert.equal(fields(tree, 'Plugin not ready')[0].props.description, 'waiting for gamescope');
  tree = await show(h, { ready: false, reason: 'waiting for gamescope', settings_notice: 'kept as x' });
  assert.deepEqual(labels(tree), ['Plugin not ready', 'Settings reset']);
  tree = await show(h, { settings_notice: 'The saved settings were damaged, so the last good copy is in use. '
    + 'The damaged file is kept as settings.json.corrupt-1.' });
  assert.deepEqual(labels(tree).slice(0, 1), ['Settings restored'], 'nothing was reset');
});

test('slider and Now texts follow the backend', async () => {
  const h = await open();
  // At the limit is not past it; a build on one half is not shown on the other.
  let tree = await show(h, { sdr_saturation: 2, hdr_saturation: 1.3, generating: true });
  assert.match(sliders(tree)[0].props.description, /^100% is neutral/);
  assert.equal(sliders(tree)[1].props.description, 'Raises color saturation without changing brightness.');
  assert.equal(fields(tree, 'Now')[0].props.description, undefined);
  assert.equal(fields(tree, 'Now')[1].props.description, 'Preparing the colors…');
  tree = await show(h);
  assert.equal(fields(tree, 'Now')[1].props.description, '100% is neutral, nothing to apply');
  tree = await show(h, { hdr_saturation: 1.2 });
  assert.equal(fields(tree, 'Now')[1].props.description, 'Waiting to apply the HDR setting');
  tree = await show(h, { hdr_saturation: 1.4, hdr_applied_saturation: 1.3 });
  assert.equal(fields(tree, 'Now')[1].props.description, 'Last applied: 130%');
  // The SDR look exists only on a standard-gamut panel past 200%; a wide one never reports it.
  tree = await show(h, { sdr_saturation: 2.8, sdr_look_saturation: 1.4 });
  assert.equal(fields(tree, 'Now')[0].props.description, 'Last applied: 280%');
  assert.equal(fields(tree, 'Now')[0].props.children, '280%');
  tree = await show(h, { sdr_saturation: 2.8, sdr_look_saturation: 1.4, sdr_generating: true });
  assert.equal(fields(tree, 'Now')[0].props.description, 'Preparing the colors…');
  tree = await show(h, { sdr_saturation: 2.5, hdr_saturation: 1.4 });
  assert.equal(sliders(tree)[0].props.description, 'Past 200% the strongest colors start clipping and lose detail.');
  assert.equal(sliders(tree)[1].props.description, 'Past 130% the strongest colors start clipping and lose detail.');
  assert.deepEqual(sliders(tree).map(n => [n.props.min, n.props.max]), [[1, 3], [1, 1.5]]);
});

test('switch advice under an apply error names only controls that are shown', async () => {
  const advice = async over => fields((await open(over)).render(), 'What happens now')[0].props.description;
  // Docked: no switch is shown.
  assert.doesNotMatch(await advice({ sdr_error: 'x', external_display: true }), /switch the effect off and on/);
  // The panel stopped advertising PQ while an HDR look was applied: only SDR's switch is on screen.
  const hidden = { sdr_enabled: false, hdr_error: 'could not restore the previous HDR look', hdr_supported: false };
  assert.doesNotMatch(await advice(hidden), /switch the effect off and on/);
  assert.deepEqual(toggles((await open(hidden)).render()).map(n => n.props.label), ['Enabled']);
  // An SDR error next to it keeps the advice: that switch is shown.
  assert.match(await advice({ ...hidden, sdr_enabled: true, sdr_error: 'could not set the SDR atom' }),
    /switch the effect off and on/);
});

test('edits show at once, toggles carry the newest slider value and collapse, reset drops queued edits', async () => {
  const h = await open({ hdr_saturation: 1 });
  let tree = h.render();
  const hdr = sliders(tree)[1], hdrToggle = toggles(tree)[1];
  hdr.props.onChange(1.1);        // in flight; everything after it queues
  hdr.props.onChange(1.4);
  await settle();
  assert.equal(fields(h.render(), 'Now')[1].props.children, '140%', 'shown before any answer');
  hdrToggle.props.onChange(false);
  for (let n = 0; n < 20; ++n) hdrToggle.props.onChange(n % 2 === 0);
  await settle();
  h.respond('set_hdr_saturation', ok({ hdr_saturation: 1.1 }));
  await settle();
  h.respond('set_hdr_enabled', ok({ hdr_enabled: false, hdr_saturation: 1.4 }));
  await settle();
  assert.deepEqual(writes(h), [['set_hdr_saturation', 1.1], ['set_hdr_enabled', false, 1.4]]);

  h.calls.length = 0;
  tree = h.render();
  sliders(tree)[0].props.onChange(1.2);   // in flight
  sliders(tree)[0].props.onChange(1.6);
  toggles(tree)[0].props.onChange(false);
  toggles(tree)[1].props.onChange(true);
  all(tree).find(n => n.type === 'ButtonItem' && n.props.children === 'Back to defaults').props.onClick();
  await settle();
  tree = h.render();
  assert.deepEqual([toggles(tree).map(n => n.props.checked), sliders(tree).length], [[false, false], 0],
    'defaults shown at once, before the backend answers');
  h.respond('set_sdr_saturation', ok({ sdr_saturation: 1.2 }));
  await settle();
  h.respond('reset', ok({ sdr_enabled: false, hdr_enabled: false }));
  await settle();
  assert.deepEqual(writes(h), [['set_sdr_saturation', 1.2], ['reset']]);
});

test('a refused or failed change is toasted and the backend state shown', async () => {
  const h = await open({ sdr_saturation: 1.5 });
  sliders(h.render())[0].props.onChange(9);
  await settle();
  h.respond('set_sdr_saturation', { ok: false, error: 'saturation must be a finite number', state: { ...baseState, sdr_saturation: 1.5 } });
  await settle();
  assert.deepEqual(h.toasts.map(t => [t.title, t.body]), [['Decky Vibrance HDR', 'saturation must be a finite number']]);
  assert.equal(fields(h.render(), 'Now')[0].props.children, '150%');
  sliders(h.render())[0].props.onChange(1.6);
  await settle();
  h.fail('set_sdr_saturation', new Error('socket closed'));
  await settle();
  assert.deepEqual(h.toasts.at(-1).body, 'socket closed');
});

test('a refused change says why under its control until that control is used again', async () => {
  const h = await open({ sdr_saturation: 1.5 });
  const refusal = node => all(node).find(n => n.props?.role === 'alert')?.props.children;
  sliders(h.render())[0].props.onChange(9);
  await settle();
  h.respond('set_sdr_saturation', { ok: false, error: 'saturation must be a finite number', state: { ...baseState, sdr_saturation: 1.5 } });
  await settle();
  let tree = h.render();
  assert.equal(refusal(sliders(tree)[0]), 'saturation must be a finite number');
  assert.equal(sliders(tree)[0].props.description.props.children[0],
    '100% is neutral; higher values make colors more saturated.', 'the usual description stays above it');
  assert.deepEqual([refusal(sliders(tree)[1]), ...toggles(tree).map(refusal)], [undefined, undefined, undefined],
    'only under the control that was refused');

  toggles(tree)[1].props.onChange(false);
  await settle();
  h.respond('set_hdr_enabled', { ok: false, error: 'enabled must be a boolean', state: baseState });
  await settle();
  tree = h.render();
  assert.equal(refusal(toggles(tree)[1]), 'enabled must be a boolean');
  assert.equal(refusal(sliders(tree)[0]), 'saturation must be a finite number', 'another control keeps its text');

  all(tree).find(n => n.type === 'ButtonItem' && n.props.children === 'Back to defaults').props.onClick();
  await settle();
  h.respond('reset', { ok: false, error: 'settings have not loaded; retry after startup', state: baseState });
  await settle();
  assert.equal(refusal(all(h.render()).find(n => n.type === 'ButtonItem' && n.props.children === 'Back to defaults')),
    'settings have not loaded; retry after startup');

  sliders(h.render())[0].props.onChange(1.6);  // the next action on that control clears its text
  await settle();
  assert.equal(refusal(sliders(h.render())[0]), undefined);
});

test('a slow write says the backend is busy and keeps reads back; settings notices show after', async () => {
  const failure = 'could not load settings: [Errno 13] Permission denied';
  const h = await open({ ready: false, reason: failure, settings_error: failure, settings_loaded: false });
  let tree = h.render();
  assert.deepEqual(aboutSections(tree), ['about']);
  const ready = await show(h);
  const before = reads(h);
  sliders(ready)[0].props.onChange(1.2);
  await settle();
  h.advance(10300);  // past the slow-call notice
  await settle();
  tree = h.render();
  assert.deepEqual(labels(tree).slice(0, 1), ['Waiting for backend']);
  assert.equal(reads(h), before, 'the write still blocks every read');
  h.respond('set_sdr_saturation', ok({ settings_notice: 'The old file is kept as settings.json.corrupt-1.' }));
  await settle();
  tree = h.render();
  assert.deepEqual(labels(tree).slice(0, 1), ['Settings reset']);
  assert.deepEqual(aboutSections(tree), ['about'], 'one key in both layouts, so switching keeps the About row focused');
});

test('a failing read says why, a timed-out one too, and a closed panel stops polling', async () => {
  const h = harness({ keep: KEEP, responses: { get_version: VERSION } });
  assert.equal(find(h.render(), 'Spinner').type, 'Spinner', 'a spinner until the first answer');
  await settle();
  h.fail('get_state', new Error('socket closed'));
  await settle();
  let tree = h.render();
  let field = find(tree, 'Field');
  assert.deepEqual([field.props.label, field.props.description.props.children], ['Decky Vibrance HDR unavailable', 'socket closed']);
  assert.ok(all(tree).some(n => n.type === 'ButtonItem' && n.props.children === 'Try again'));
  const update = all(tree).find(n => n.type?.name === 'UpdateSection');
  assert.deepEqual([update.props.version, update.props.home], ['1.2.0', '/home/deck'], 'a plugin that cannot start can still be updated');

  h.advance(1000);  // not ready yet: the quick poll
  await settle();
  h.advance(10000);  // that read never answers
  await settle();
  field = find(h.render(), 'Field');
  assert.deepEqual([field.props.label, field.props.description.props.children], ['Decky Vibrance HDR unavailable', 'Error: Backend read timed out']);
  assert.equal(reads(h), 2, 'Decky replays an unanswered call: no second read');
  h.respond('get_state', ok());
  await settle();
  assert.deepEqual(labels(h.render()), ['Now', 'Now']);

  h.unmount();
  const closed = reads(h);
  h.advance(30000);
  await settle();
  assert.equal(reads(h), closed, 'no polling while the menu is closed');
  h.remount();
  await settle();
  assert.equal(reads(h), closed + 1, 'reopened: one poll loop again');
});

test('polls quickly while the backend works, slowly when nothing moves', async () => {
  const h = await open({ generating: true });
  const before = reads(h);
  h.advance(1000);
  await settle();
  assert.equal(reads(h), before + 1, 'busy: 1 s');
  h.respond('get_state', ok());
  await settle();
  h.advance(2900);
  await settle();
  assert.equal(reads(h), before + 1, 'idle: not before 3 s');
  h.advance(100);
  await settle();
  assert.equal(reads(h), before + 2);
  h.respond('get_state', ok());
  await settle();

  // Idle, then a change starts a color table: its progress shows within a second, not after the idle wait.
  sliders(h.render())[1].props.onChange(1.3);
  await settle();
  h.respond('set_hdr_saturation', ok({ hdr_saturation: 1.3, generating: true }));
  await settle();
  h.advance(1000);
  await settle();
  assert.equal(reads(h), before + 3);
});
