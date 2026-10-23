const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const source = fs.readFileSync(path.join(__dirname, '../src/mlx_activation_lab/server_web/app.js'), 'utf8');
const settle = () => new Promise(resolve => setImmediate(resolve));

function setup() {
  const calls = [], streams = [], nodes = new Map(), documentEvents = {}, windowEvents = {};
  const node = selector => {
    if (!nodes.has(selector)) nodes.set(selector, {content:'fixture', value:'', handlers:{},
      addEventListener(name, fn) {this.handlers[name] = fn;}, classList:{toggle(){}}});
    return nodes.get(selector);
  };
  const fixture = {token:'fixture', server:{running:false}, models:[], aliases:[], default_alias:'', log:[]};
  class Events {
    constructor(url) {this.url = url; this.handlers = {}; this.closed = false; streams.push(this);}
    addEventListener(name, fn) {this.handlers[name] = fn;}
    close() {this.closed = true;}
    emit(name) {return this.handlers[name]?.();}
  }
  const document = {hidden:false, activeElement:null, querySelector:node, querySelectorAll:()=>[],
    addEventListener(name, fn) {documentEvents[name] = fn;}};
  const context = vm.createContext({document, window:{addEventListener(name, fn) {windowEvents[name] = fn;}},
    EventSource:Events, localStorage:{getItem:()=>null},
    setInterval() {throw new Error('Periodic polling is forbidden');},
    setTimeout() {throw new Error('UI status timers are forbidden');},
    fetch:async url => {calls.push(url); return {ok:true, json:async()=>JSON.parse(JSON.stringify(
      url === '/api/cached-models' ? {models:[],names:{}} : fixture))};}, fixture});
  vm.runInContext(source, context);
  return {calls, streams, node, document, documentEvents, windowEvents, fixture,
    execute: code => vm.runInContext(code, context)};
}

test('initial view waits for one event connection and never installs polling timers', async () => {
  const ui = setup();
  assert.equal(ui.streams.length, 1);
  assert.equal(ui.streams[0].url, '/api/events');
  assert.deepEqual(ui.calls, []);
  ui.streams[0].emit('open');
  await settle();
  assert.deepEqual(ui.calls.sort(), ['/api/cached-models', '/api/status']);
  await settle();
  assert.equal(ui.calls.length, 2);
});

test('Codex/API state-change notifications update the active model and stop state', async () => {
  const ui = setup();
  ui.streams[0].emit('open');
  await settle();
  ui.fixture.server = {running:true, pid:123, active_model:'owner/model'};
  ui.streams[0].emit('changed');
  await settle();
  assert.equal(ui.node('#status-title').textContent, '稼働中');
  assert.equal(ui.node('#process-id').textContent, 'PID 123');
  ui.fixture.server = {running:false};
  ui.streams[0].emit('changed');
  await settle();
  assert.equal(ui.node('#status-title').textContent, '停止中');
  assert.equal(ui.calls.filter(url => url === '/api/status').length, 3);
});

test('closing the native window disconnects; reopening reads the current snapshot', async () => {
  const ui = setup();
  ui.streams[0].emit('open');
  await settle();
  ui.windowEvents['mlx-visibility']({detail:false});
  assert.equal(ui.streams[0].closed, true);
  const count = ui.calls.length;
  ui.streams[0].emit('changed');
  await settle();
  assert.equal(ui.calls.length, count);
  ui.fixture.server = {running:true, pid:456};
  ui.windowEvents['mlx-visibility']({detail:true});
  assert.equal(ui.streams.length, 2);
  ui.streams[1].emit('open');
  await settle();
  assert.equal(ui.node('#status-title').textContent, '稼働中');
});

test('document visibility and page unload close the notification connection', async () => {
  const ui = setup();
  ui.document.hidden = true;
  ui.documentEvents.visibilitychange();
  assert.equal(ui.streams[0].closed, true);
  ui.document.hidden = false;
  ui.documentEvents.visibilitychange();
  assert.equal(ui.streams.length, 2);
  ui.windowEvents.pagehide();
  assert.equal(ui.streams[1].closed, true);
});

test('loading state also uses notifications without a short polling loop', async () => {
  const ui = setup();
  ui.fixture.server = {running:true, loading:true, pid:789};
  ui.streams[0].emit('open');
  await settle();
  assert.equal(ui.node('#status-title').textContent, 'モデルを読込中');
  const count = ui.calls.length;
  await settle();
  assert.equal(ui.calls.length, count);
  ui.fixture.server.loading = false;
  ui.streams[0].emit('changed');
  await settle();
  assert.equal(ui.node('#status-title').textContent, '稼働中');
});

test('an early process exit clears loading immediately without a future timer', async () => {
  const ui = setup();
  ui.fixture.server = {running:true, loading:true, pid:123};
  ui.streams[0].emit('open');
  await settle();
  ui.fixture.server = {running:false};
  ui.streams[0].emit('changed');
  await settle();
  assert.equal(ui.node('#status-title').textContent, '停止中');
  assert.equal(ui.node('#start-alias').disabled, false);
});

test('closing a select applies deferred state without another server read', async () => {
  const ui = setup();
  ui.fixture.log = ['before'];
  ui.streams[0].emit('open');
  await settle();
  ui.document.activeElement = {tagName:'SELECT'};
  ui.fixture.log = ['after'];
  ui.streams[0].emit('changed');
  await settle();
  assert.equal(ui.node('#log').textContent, 'before');
  const count = ui.calls.length;
  ui.document.activeElement = null;
  ui.documentEvents.focusout({target:{tagName:'SELECT'}});
  assert.equal(ui.node('#log').textContent, 'after');
  assert.equal(ui.calls.length, count);
});

test('a late open callback from a closed stream cannot start background work', async () => {
  const ui = setup();
  ui.windowEvents['mlx-visibility']({detail:false});
  ui.streams[0].emit('open');
  await settle();
  assert.deepEqual(ui.calls, []);
  ui.windowEvents['mlx-visibility']({detail:true});
  ui.streams[0].emit('open');
  await settle();
  assert.deepEqual(ui.calls, []);
  ui.streams[1].emit('open');
  await settle();
  assert.equal(ui.calls.length, 2);
});
