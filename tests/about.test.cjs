// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 Rayekkk
// The About row and page on the panel's real TSX (DeckyKit's frontend_harness.cjs). The page and its updater are
// DeckyKit's about.tsx, tested there; here: the wiring to this plugin's RPCs, version, home folder and navigation.
const test = require('node:test');
const assert = require('node:assert/strict');
const { harness, all, settle } = require('./frontend_harness.cjs');

const state = { ready: true, reason: 'ready', sdr_enabled: false, hdr_enabled: false, sdr_saturation: 1,
  hdr_saturation: 1, sdr_applied: false, sdr_look_applied: false, hdr_applied: false, hdr_applied_saturation: null,
  sdr_look_saturation: null, sdr_error: '', sdr_look_error: '', hdr_error: '', settings_error: '',
  settings_notice: '', settings_loaded: true, generating: false, sdr_generating: false, conflict: '',
  external_display: false, hdr_supported: true, sdr_min: 1, sdr_max: 3, sdr_soft_limit: 2, hdr_min: 1,
  hdr_max: 1.5, hdr_soft_limit: 1.3 };
const VERSION = { ok: true, version: '1.2.0', home: '/home/deck' };

async function open(keep = ['SectionLink', 'AboutPage', 'PageHeader']) {
  const h = harness({ keep, responses: { get_state: { ok: true, state }, get_version: VERSION } });
  h.render();
  await settle();
  return h;
}
const of = (tree, name) => all(tree).find(n => n.type?.name === name);
const asked = (h, name) => h.calls.filter(c => c.name === name).length;

test('About is the last row and opens its page; back and B return to its row', async () => {
  const h = await open();
  let tree = h.render();
  const sections = all(tree).filter(n => n.type === 'PanelSection');
  const row = of(tree, 'SectionLink');
  assert.equal(of(sections.at(-1), 'SectionLink'), row, 'the last section');
  assert.deepEqual([row.props.title, row.props.description, row.props.focus], ['About', 'Version 1.2.0 · updates', false]);

  row.props.onOpen();
  tree = h.render();
  assert.equal(of(tree, 'SectionLink'), undefined, 'a page of its own');
  const header = of(tree, 'PageHeader');
  assert.deepEqual([header.props.title, header.props.back, header.props.focus], ['About', 'Decky Vibrance HDR', true]);
  const page = of(tree, 'AboutPage').props;
  assert.deepEqual([page.name, page.version, page.license, page.source, page.home],
    ['Decky Vibrance HDR', '1.2.0', 'BSD-3-Clause', 'github.com/Rayekkk/DeckyVibranceHDR', '/home/deck']);
  assert.match(page.description, /^[A-Z].*\.$/, 'one sentence');

  header.props.onBack();
  assert.equal(of(h.render(), 'SectionLink').props.focus, true, 'the row it came from takes the focus');
  of(h.render(), 'SectionLink').props.onOpen();
  const event = { prevented: 0, preventDefault() { this.prevented++; }, stopPropagation() { this.prevented++; } };
  all(h.render()).find(n => n.type === 'Focusable').props.onCancelButton(event);
  assert.equal(event.prevented, 2, 'B stays in the plugin');
  assert.ok(of(h.render(), 'SectionLink'), 'B goes back');
  h.hide();
  assert.equal(of(h.render(), 'SectionLink').props.focus, false, 'no focus while the menu is hidden');
});

test('the page is info first and Updates last', async () => {
  const h = await open([]);
  all(h.render()).find(n => n.type === 'Field' && n.props.label === 'About').props.onActivate();
  const titles = all(h.render()).filter(n => n.type === 'PanelSection').map(n => n.props.title);
  assert.deepEqual(titles, ['About', 'Decky Vibrance HDR', 'Updates']);
});

test('the updater uses this plugin\'s RPCs and stops when the plugin unloads', async () => {
  const h = harness({ keep: ['SectionLink', 'AboutPage', 'PageHeader'], responses: { get_state: { ok: true, state },
    get_version: VERSION, check_for_updates: { ok: true, current_version: '1.2.0', latest_version: '2.0.0',
      update_available: true, download_available: true },
    perform_update: { ok: true, path: '/home/deck/Downloads/DeckyVibranceHDR-2.0.0.zip', version: '2.0.0' } } });
  h.render();
  await settle();
  of(h.render(), 'SectionLink').props.onOpen();
  const updates = of(h.render(), 'AboutPage').props.updates;
  await updates.run('check');
  await updates.run('download', '2.0.0');
  assert.deepEqual(h.calls.filter(c => c.name.includes('update')).map(c => [c.name, ...c.args]),
    [['check_for_updates'], ['perform_update', '2.0.0']]);
  assert.equal(updates.getSnapshot().download.version, '2.0.0');

  h.exports.default().onDismount();
  await updates.run('check');
  assert.equal(asked(h, 'check_for_updates'), 1, 'nothing starts after unload');
});

test('the installed version is read once, shared while in flight and retried after a failure', async () => {
  const h = harness({ keep: ['SectionLink'], responses: { get_state: { ok: true, state } } });
  h.render();
  h.remount();  // reopened before the first read answers
  await settle();
  assert.equal(asked(h, 'get_version'), 1, 'one read in flight is shared');
  h.fail('get_version', new Error('not yet'));
  await settle();
  assert.equal(of(h.render(), 'SectionLink').props.description, 'Version — · updates');
  h.remount();
  await settle();
  h.respond('get_version', VERSION);
  await settle();
  assert.equal(of(h.render(), 'SectionLink').props.description, 'Version 1.2.0 · updates');
  h.remount();
  await settle();
  assert.equal(asked(h, 'get_version'), 2, 'known now: never asked again');
});
