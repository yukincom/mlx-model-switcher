// UI control-flow tests: all network calls and dialogs are fixtures.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const source = fs.readFileSync(path.join(__dirname, '../src/mlx_activation_lab/server_web/app.js'), 'utf8');

function setup({running = true, trusted = true, confirmations = [], previewError, deleteError, activeModel = 'owner/default'} = {}) {
  const nodes = new Map();
  const node = selector => {
    if (!nodes.has(selector)) nodes.set(selector, {
      content: 'fixture-token', value: '', addEventListener() {}, classList: {toggle() {}},
    });
    return nodes.get(selector);
  };
  const calls = [], dialogs = [];
  const fixture = {
    server: {running, trusted, pid: 123, command: 'fixture server', active_model: activeModel, active_name: 'Default'},
    models: [
      {alias: 'default', model: 'owner/default', name: 'Default', order: 0},
      {alias: 'other', model: 'owner/other', name: 'Other', order: 1},
      {alias: 'duplicate', model: 'owner/default', name: 'Duplicate', order: 2},
    ], default_alias: 'default', aliases: [],
  };
  const context = vm.createContext({
    document: {addEventListener() {}, querySelector: node, querySelectorAll: () => []},
    localStorage: {getItem: () => null}, window:{addEventListener(){}}, EventSource:class {addEventListener(){} close(){}},
    // Keep bootstrap fetches pending; no real HTTP endpoint is contacted.
    fetch: () => new Promise(() => {}),
    confirm: text => {
      dialogs.push(text);
      assert.ok(confirmations.length, 'unexpected confirmation');
      return confirmations.shift();
    }, fixture,
    mockRequest: async (url, payload) => {
      calls.push({url, payload: JSON.parse(JSON.stringify(payload))});
      if (url === '/api/delete-preview' && previewError) throw new Error(previewError);
      if (url === '/api/delete' && deleteError) throw new Error(deleteError);
      if (url === '/api/delete-preview') return {
        items: [{name: 'Other', bytes: 7, file_count: 1, location: '/fixture'}], bytes: 7, fingerprint: 'fixture',
      };
      if (url === '/api/delete') return {};
      throw new Error(`unexpected request ${url}`);
    },
  });
  vm.runInContext(source, context);
  vm.runInContext("current = fixture; checkedAliases.add('other'); request = mockRequest; refresh = async () => {}; refreshCachedModels = async () => {};", context);
  return {context, calls, dialogs, node, execute: code => vm.runInContext(code, context)};
}


test('only the active model and aliases pointing to it are disabled; inactive default is deletable', () => {
  const ui=setup();
  ui.execute("current.default_alias='other';updateSelection();");
  assert.equal(ui.node('#delete-selected').disabled,false);
  assert.equal(ui.node('#remove-selected').disabled,false);
  for (const alias of ['default','duplicate']) {
    ui.execute(`checkedAliases.clear();checkedAliases.add('${alias}');updateSelection();`);
    assert.equal(ui.node('#delete-selected').disabled,true);
    assert.equal(ui.node('#remove-selected').disabled,true);
  }
});

test('active or unidentified models do not send deletion requests', async () => {
  for (const body of [true,false]) {
    const active=setup();
    active.execute("checkedAliases.clear();checkedAliases.add('duplicate');");
    await active.execute(`removeSelected(${body})`);
    assert.deepEqual(active.calls,[]);
    const unknown=setup({trusted:false,activeModel:''});
    unknown.execute('updateSelection()');
    assert.equal(unknown.node('#delete-selected').disabled,true);
    assert.equal(unknown.node('#remove-selected').disabled,true);
    await unknown.execute(`removeSelected(${body})`);
    assert.deepEqual(unknown.calls,[]);
  }
});

test('cancelling deletion only requests a preview and never stops the server', async () => {
  const ui=setup({confirmations:[false]});
  await ui.execute('removeSelected(true)');
  assert.deepEqual(ui.calls.map(call=>call.url),['/api/delete-preview']);
  assert.equal(ui.dialogs.length,1);
  assert.match(ui.dialogs[0],/本体と登録を削除/);
  assert.equal(ui.execute('current.server.running'),true);
  assert.equal(ui.execute('busy'),false);
});

test('one approval deletes only the selected model, never requesting a stop', async () => {
  for (const trusted of [true,false]) {
    const ui=setup({trusted,confirmations:[true]});
    await ui.execute('removeSelected(true)');
    assert.deepEqual(ui.calls.map(call=>call.url),['/api/delete-preview','/api/delete']);
    assert.deepEqual(ui.calls[1].payload,{aliases:['other'],body:true,fingerprint:'fixture'});
    assert.equal(ui.execute('current.server.running'),true);
    assert.equal(ui.execute('current.server.active_model'),'owner/default');
    assert.equal(ui.dialogs.length,1);
  }
});

test('preview failure or active-model change at deletion is reported without stopping or retrying', async () => {
  const preview=setup({previewError:'active model changed'});
  await preview.execute('removeSelected(true)');
  assert.deepEqual(preview.calls.map(call=>call.url),['/api/delete-preview']);
  assert.deepEqual(preview.dialogs,[]);
  const changed=setup({deleteError:'model is now active',confirmations:[true]});
  await changed.execute('removeSelected(true)');
  assert.deepEqual(changed.calls.map(call=>call.url),['/api/delete-preview','/api/delete']);
  assert.equal(changed.node('#message').className,'message error');
  assert.equal(changed.execute('current.server.running'),true);
});

test('stopped models including the default and running-server registration removals use one confirmation', async () => {
  for (const [running,body] of [[false,true],[false,false],[true,false]]) {
    const ui=setup({running,confirmations:[true]});
    if (!running) ui.execute("checkedAliases.clear();checkedAliases.add('default');updateSelection();");
    assert.notEqual(ui.node('#delete-selected').disabled,true);
    await ui.execute(`removeSelected(${body})`);
    assert.deepEqual(ui.calls.map(call=>call.url),['/api/delete-preview','/api/delete']);
    assert.equal(ui.dialogs.length,1);
  }
});
