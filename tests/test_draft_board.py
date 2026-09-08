"""Exercise actual board rendering without opening the user's browser."""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js unavailable")
def test_flex_board_uses_shared_values_and_respects_eligibility():
    script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const render = source.slice(source.indexOf('function renderPlayers()'),
                            source.indexOf('function renderRoster()'));
const elements = new Map();
const context = {
  $: (id) => {
    if (!elements.has(id)) elements.set(id, {value: ''});
    return elements.get(id);
  },
  escapeHtml: String,
  formatNumber: String,
  state: {flex_positions: ['RB', 'WR', 'TE'], players: [
    {Player: 'Tight End', Position: 'TE', Team: 'AAA', VOR_Points: 90,
     VOR_Floor: 80, VOR_Ceiling: 110, Points: 190, Floor: 180, Ceiling: 210,
     FLEX_VOR_Points: 10, FLEX_VOR_Floor: 0, FLEX_VOR_Ceiling: 30},
    {Player: 'Receiver', Position: 'WR', Team: 'BBB', VOR_Points: 30,
     VOR_Floor: 20, VOR_Ceiling: 60, Points: 210, Floor: 200, Ceiling: 240,
     FLEX_VOR_Points: 30, FLEX_VOR_Floor: 20, FLEX_VOR_Ceiling: 60},
  ]},
  activePosition: 'FLEX',
};
vm.createContext(context);
vm.runInContext(render, context);
for (const metric of ['VOR_Points', 'VOR_Floor', 'VOR_Ceiling']) {
  context.state.metric = metric;
  vm.runInContext('renderPlayers()', context);
  const html = elements.get('#players').innerHTML;
  assert.ok(html.indexOf('Receiver') < html.indexOf('Tight End'));
  assert.equal(elements.get('#metric-heading').textContent,
               `FLEX_${metric}`.replaceAll('_', ' '));
}
// Filtering/sorting must not mutate the all-position board.
assert.equal(context.state.players[0].Player, 'Tight End');
context.activePosition = 'ALL';
context.state.metric = 'VOR_Points';
vm.runInContext('renderPlayers()', context);
assert.equal(elements.get('#metric-heading').textContent, 'VOR Points');
assert.ok(elements.get('#players').innerHTML.indexOf('Tight End') <
          elements.get('#players').innerHTML.indexOf('Receiver'));
// TE remains eligible and wins when its actual FLEX value is higher.
context.activePosition = 'FLEX';
context.state.players[0].FLEX_VOR_Points = 50;
vm.runInContext('renderPlayers()', context);
assert.ok(elements.get('#players').innerHTML.indexOf('Tight End') <
          elements.get('#players').innerHTML.indexOf('Receiver'));
context.state.flex_positions = ['RB', 'WR'];
vm.runInContext('renderPlayers()', context);
assert.ok(!elements.get('#players').innerHTML.includes('Tight End'));
// Older data must fall back to raw points, never positional scarcity.
context.state.flex_positions = ['RB', 'WR', 'TE'];
for (const player of context.state.players) delete player.FLEX_VOR_Points;
vm.runInContext('renderPlayers()', context);
assert.equal(elements.get('#metric-heading').textContent, 'Points');
assert.ok(elements.get('#players').innerHTML.indexOf('Receiver') <
          elements.get('#players').innerHTML.indexOf('Tight End'));
"""
    path = Path(__file__).parents[1] / "src/fantasyfootball/web/draft.js"
    subprocess.run(["node", "-e", script, str(path)], check=True, timeout=10)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js unavailable")
def test_sync_displays_conflicts_and_refreshes_same_length_corrections():
    script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const elements = new Map();
let refreshed = 0;
const notices = [];
const initial = {
  sync_available: true, mode: 'live', site: 'ESPN', current_pick: 3,
  picks: [{number: 1, player: 'Wrong A'}, {number: 2, player: 'Wrong B'}],
};
const corrected = {...initial, picks: [
  {number: 1, player: 'Wrong B'}, {number: 2, player: 'Wrong A'}
]};
const payload = {state: corrected, added: [], conflicts: [
  {number: 19, local: 'Local player', platform: 'ESPN player'}
]};
const context = {
  state: initial, syncInFlight: false, lastSyncAt: null,
  syncConflicts: [], lastConflictNotice: '',
  $: (id) => {
    if (!elements.has(id)) elements.set(id, {});
    return elements.get(id);
  },
  request: async () => payload,
  afterStateChange: async () => { refreshed++; },
  scheduleLiveSync: () => {},
  toast: (message) => notices.push(message),
  formatLastRun: String,
};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('function renderSyncStatus('),
                             source.indexOf('function renderAdpStatus(')), context);
vm.runInContext(source.slice(source.indexOf('async function pollLiveDraft()')), context);
(async () => {
  await vm.runInContext('pollLiveDraft()', context);
  assert.equal(refreshed, 1);
  assert.equal(context.state.picks[0].player, 'Wrong B');
  assert.match(elements.get('#sync-badge').textContent, /1 pick-order conflicts/);
  assert.match(elements.get('#sync-badge').title, /#19: local Local player; ESPN ESPN player/);
  assert.ok(notices.some((message) => message.includes('Pick order differs')));
  const noticeCount = notices.length;
  await vm.runInContext('pollLiveDraft()', context);
  assert.equal(refreshed, 1);
  assert.equal(notices.length, noticeCount);
  payload.conflicts = [];
  await vm.runInContext('pollLiveDraft()', context);
  assert.ok(!elements.get('#sync-badge').textContent.includes('conflicts'));
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
    path = Path(__file__).parents[1] / "src/fantasyfootball/web/draft.js"
    subprocess.run(["node", "-e", script, str(path)], check=True, timeout=10)
