// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 Rayekkk
// The Updates section's real TSX on DeckyKit's frontend_harness.cjs; the answers are dk_update's (D2).
const test = require('node:test');
const assert = require('node:assert/strict');
const { harness, all, settle } = require('./frontend_harness.cjs');

const offer = { ok: true, current_version: '1.0.0', latest_version: '2.0.0', update_available: true,
  download_available: true, asset_name: 'DeckyVibranceHDR-2.0.0.zip' };

function section() {
  const h = harness({ entry: 'src/updates.tsx', component: 'UpdateSection' });
  const tree = () => all(h.render());
  return {
    h, tree,
    labels: () => tree().filter(n => n.type === 'Field').map(n => n.props.label),
    field: label => tree().find(n => n.type === 'Field' && n.props.label === label),
    button: text => tree().find(n => n.type === 'ButtonItem' && n.props.children === text),
    asked: name => h.calls.filter(c => c.name === name).length,
  };
}

test('the installed version is asked once, shared while in flight and retried after a failure', async () => {
  const s = section();
  s.h.render();
  s.h.remount();  // reopened before the first read answers
  await settle();
  assert.equal(s.asked('get_version'), 1, 'one read in flight is shared');
  s.h.fail('get_version', new Error('not yet'));
  await settle();
  assert.equal(s.field('Installed').props.children, 'v?');
  s.h.remount();
  await settle();
  s.h.respond('get_version', { ok: true, version: '1.0.0' });
  await settle();
  assert.equal(s.field('Installed').props.children, 'v1.0.0');
  s.h.remount();
  await settle();
  assert.equal(s.asked('get_version'), 2, 'known now: never asked again');
});

test('each check answer is shown as it is', async () => {
  const s = section();
  s.h.render();
  await settle();
  s.h.respond('get_version', { ok: true, version: '1.0.0' });
  const check = async answer => {
    const pending = s.button('Check for updates').props.onClick();
    await settle();
    assert.equal(s.button('Checking...').props.disabled, true);
    if (answer instanceof Error) s.h.fail('check_for_updates', answer); else s.h.respond('check_for_updates', answer);
    await pending;
    await settle();
  };

  await check(new Error('offline'));
  assert.deepEqual(s.labels(), ['Installed', 'Error']);
  assert.equal(s.field('Error').props.description, 'offline');
  assert.deepEqual(s.h.toasts.map(t => t.title), ['Update check failed']);

  await check({ ok: true, current_version: '1.0.1', latest_version: '1.0.1', update_available: false,
    download_available: false });
  assert.deepEqual(s.labels(), ['Installed', 'Latest', 'Up to date']);
  assert.equal(s.field('Installed').props.children, 'v1.0.1');

  await check({ ok: false, current_version: '1.0.1', error: "GitHub's request limit was reached. Try again later." });
  assert.deepEqual(s.labels(), ['Installed', 'Error']);

  await check({ ok: true, current_version: '1.0.1', update_available: false, download_available: false,
    no_release: true, message: 'No release has been published yet (or the repository is private).' });
  assert.deepEqual(s.labels(), ['Installed', 'Not offered'], 'never "Up to date" when there is no release');

  await check({ ...offer, download_available: false, message: 'The release has no single DeckyVibranceHDR-2.0.0.zip yet.' });
  assert.deepEqual(s.labels(), ['Installed', 'Latest', 'Not offered']);
  assert.equal(s.button('Download v2.0.0'), undefined, 'nothing verifiable to download');

  await check(offer);
  assert.deepEqual(s.labels(), ['Installed', 'Latest']);
  assert.ok(s.button('Download v2.0.0'));
});

test('downloads: one at a time, the version seen is the one asked for, failures shown and toasted only while open', async () => {
  const s = section();
  s.h.render();
  await settle();
  s.h.respond('get_version', { ok: true, version: '1.0.0' });
  s.button('Check for updates').props.onClick();
  await settle();
  s.h.respond('check_for_updates', offer);
  await settle();

  const download = s.button('Download v2.0.0').props.onClick();
  await settle();
  assert.ok(s.button('Downloading...'));
  assert.equal(s.button('Check for updates').props.disabled, true);
  s.button('Check for updates').props.onClick();  // would replace the offer under the download
  s.button('Downloading...').props.onClick();
  await settle();
  assert.equal(s.asked('check_for_updates'), 1);
  assert.equal(s.asked('perform_update'), 1, 'a second press while it runs sends nothing');
  assert.deepEqual(s.h.calls.find(c => c.name === 'perform_update').args, ['2.0.0']);

  s.h.respond('perform_update', { ok: false, error: 'The download did not match the SHA-256 GitHub states.' });
  await download;
  await settle();
  assert.equal(s.field('Error').props.description, 'The download did not match the SHA-256 GitHub states.');
  assert.deepEqual(s.h.toasts.map(t => t.title), ['Download failed']);
  assert.ok(s.button('Download v2.0.0'), 'it can be tried again');

  // An answer without a file is not a finished download.
  s.button('Check for updates').props.onClick();
  await settle();
  s.h.respond('check_for_updates', offer);
  await settle();
  let pending = s.button('Download v2.0.0').props.onClick();
  await settle();
  s.h.respond('perform_update', { ok: true });
  await pending;
  await settle();
  assert.ok(!s.labels().includes('Downloaded'));
  assert.equal(s.field('Error').props.description, 'Unknown error');

  // A failure that lands while the menu is closed is kept but not toasted.
  s.button('Check for updates').props.onClick();
  await settle();
  s.h.respond('check_for_updates', offer);
  await settle();
  pending = s.button('Download v2.0.0').props.onClick();
  await settle();
  s.h.unmount();
  const toasts = s.h.toasts.length;
  s.h.fail('perform_update', new Error('gone'));
  await pending;
  await settle();
  assert.equal(s.h.toasts.length, toasts);
  s.h.remount();
  assert.equal(s.field('Error').props.description, 'gone');
});

test('a finished download stays while that release is on offer', async () => {
  const s = section();
  s.h.render();
  await settle();
  s.h.respond('get_version', { ok: true, version: '1.0.0' });
  const check = async answer => {
    s.button('Check for updates').props.onClick();
    await settle();
    s.h.respond('check_for_updates', answer);
    await settle();
  };
  await check(offer);
  s.button('Download v2.0.0').props.onClick();
  await settle();
  s.h.respond('perform_update', { ok: true, path: '/home/deck/Downloads/DeckyVibranceHDR-2.0.0.zip', version: '2.0.0' });
  await settle();
  assert.match(s.field('Downloaded').props.description, /^\/home\/deck\/Downloads\/DeckyVibranceHDR-2\.0\.0\.zip\. To install it/);
  await check(offer);
  assert.ok(s.field('Downloaded'), 'the same release: the file is still the one to install');
  s.button('Check for updates').props.onClick();
  await settle();
  s.h.fail('check_for_updates', new Error('offline'));
  await settle();
  assert.ok(s.field('Downloaded'), 'a failed check offers nothing new');
  await check({ ...offer, latest_version: '2.0.1', asset_name: 'DeckyVibranceHDR-2.0.1.zip' });
  assert.equal(s.field('Downloaded'), undefined, 'a newer release replaces the offer');
  assert.ok(s.button('Download v2.0.1'));
});
