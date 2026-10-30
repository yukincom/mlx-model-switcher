// 見込み: 記録の有無と停止可否を分ければ、外部起動も同じ停止ボタンで扱える。
// 合格: 現在のidentityを確認後に送信し、取消・不可・処理中は送信ゼロ。DOM/HTTP/dialogは模擬。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const source = fs.readFileSync(path.join(__dirname, '../src/mlx_activation_lab/server_web/app.js'), 'utf8');
function setup(server = {running:false}, stored = null) {
  const nodes = new Map(), storage = new Map([['mlx8080-thinking', stored]]), calls = [], dialogs = [];
  const node = selector => {
    if (!nodes.has(selector)) nodes.set(selector, {content:'fixture', value:'', handlers:{},
      addEventListener(name, callback) {this.handlers[name] = callback;}, classList:{toggle(){}}});
    return nodes.get(selector);
  };
  const fixture = {token:'fixture', server, default_alias:'a', models:[
    {alias:'a', model:'owner/a', name:'A', order:0}, {alias:'b', model:'owner/b', name:'B', order:1}], log:[]};
  const context = vm.createContext({document:{addEventListener(){},querySelector:node,querySelectorAll:()=>[],activeElement:null},
    localStorage:{getItem:key=>storage.get(key),setItem:(key,value)=>storage.set(key,value)}, window:{addEventListener(){}}, EventSource:class {addEventListener(){} close(){}},
    fetch:()=>new Promise(()=>{}), confirm:text=>{dialogs.push(text);return true;}, fixture, record:(url,payload)=>calls.push({url,payload:JSON.parse(JSON.stringify(payload))})});
  vm.runInContext(source, context);
  const execute = code => vm.runInContext(code, context);
  execute('current=fixture; render();');
  return {node,execute,context,fixture,storage,calls,dialogs};
}

test('model and thinking controls lock during running, loading and an in-flight operation; unlock only when stopped', () => {
  for (const state of [{running:true}, {running:true,loading:true}, {running:false,loading:true}]) {
    const ui=setup(state);
    assert.equal(ui.node('#start-alias').disabled,true);
    assert.equal(ui.node('#thinking-enabled').disabled,true);
    ui.execute('current.server={running:false}; render();');
    assert.equal(ui.node('#start-alias').disabled,false);
    assert.equal(ui.node('#thinking-enabled').disabled,false);
  }
  const ui=setup();
  ui.execute('busy=true; render();');
  assert.equal(ui.node('#start-alias').disabled,true);
  assert.equal(ui.node('#thinking-enabled').disabled,true);
});

test('checkbox choice persists across renders and reaches the start request as boolean', async () => {
  for (const enabled of [true,false]) {
    const ui=setup({running:false},'true');
    ui.node('#thinking-enabled').handlers.change({target:{checked:enabled}});
    ui.node('#start-alias').handlers.change({target:{value:'b'}});
    ui.execute('render(); request=async(url,payload)=>{record(url,payload);}; refresh=async()=>{current.server={running:true,loading:true};};');
    assert.equal(ui.node('#thinking-enabled').checked,enabled);
    assert.equal(ui.node('#start-alias').value,'b');
    ui.node('#start').handlers.click();
    await new Promise(resolve=>setImmediate(resolve));
    assert.deepEqual(ui.calls,[{url:'/api/start',payload:{alias:'b',thinking:enabled}}]);
    assert.equal(ui.storage.get('mlx8080-thinking'),String(enabled));
    assert.equal(ui.node('#message').textContent, '');
    assert.equal(ui.node('#thinking-enabled').disabled,true);
  }
});

test('running display uses actual model and launch setting, and unknown setting is indeterminate', () => {
  const ui=setup({running:true,active_model:'owner/b',thinking:false},'true');
  assert.equal(ui.node('#start-alias').value,'b');
  assert.equal(ui.node('#thinking-enabled').checked,false);
  assert.equal(ui.node('#thinking-enabled').indeterminate,false);
  ui.execute('delete current.server.thinking;render();');
  assert.equal(ui.node('#thinking-enabled').indeterminate,true);
});

test('a state update locks an open model selector when an external server starts', async () => {
  const ui=setup();
  ui.execute(`document.activeElement={tagName:'SELECT'};
    fetch=async()=>({ok:true,json:async()=>({...fixture,server:{running:true,active_model:'owner/b',thinking:true}})});`);
  await ui.execute('refresh()');
  assert.equal(ui.node('#start-alias').disabled,true);
  assert.equal(ui.node('#thinking-enabled').disabled,true);
  assert.equal(ui.node('#start-alias').value,'b');
});

test('default can move to another model while running or loading without changing the active model', async () => {
  for (const loading of [false,true]) {
    const ui=setup({running:true,loading,active_model:'owner/a',thinking:true});
    ui.execute(`checkedAliases.add('b');updateSelection();
      request=async(url,payload)=>{record(url,payload);current.default_alias=payload.alias;};
      refresh=async()=>{};`);
    assert.equal(ui.node('#default-selected').disabled,false);
    await ui.node('#default-selected').handlers.click();
    assert.deepEqual(ui.calls,[{url:'/api/default',payload:{alias:'b'}}]);
    assert.equal(ui.execute('current.default_alias'),'b');
    assert.equal(ui.execute('current.server.active_model'),'owner/a');
    assert.equal(ui.node('#start-alias').value,'a');
    assert.equal(ui.node('#start-alias').disabled,true);
    assert.equal(ui.node('#thinking-enabled').disabled,true);
    assert.equal(ui.node('#default-selected').disabled,true);
    ui.execute('current.server={running:false};render();');
    assert.equal(ui.node('#start-alias').value,'b');
    assert.equal(ui.node('#start-alias').disabled,false);
  }
});

test('default selection still requires exactly one non-default model and no operation in progress', () => {
  const ui=setup({running:true,active_model:'owner/a'});
  assert.equal(ui.node('#default-selected').disabled,true);
  ui.execute("checkedAliases.add('a');updateSelection();");
  assert.equal(ui.node('#default-selected').disabled,true);
  ui.execute("checkedAliases.add('b');updateSelection();");
  assert.equal(ui.node('#default-selected').disabled,true);
  ui.execute("checkedAliases.delete('a');updateSelection();");
  assert.equal(ui.node('#default-selected').disabled,false);
  ui.execute('busy=true;updateSelection();');
  assert.equal(ui.node('#default-selected').disabled,true);
});

// 見込み: 古い記録でもcan_stopがtrueなら停止可能。合格: ボタン有効、実commandを確認してpid/command/startedを送信。
// 範囲: 実フロントエンドのイベント処理。DOM・確認dialog・HTTPは模擬。
test('external listener without a matching record can stop using the displayed complete identity', async () => {
  const server={running:true,trusted:false,can_stop:true,pid:80818,command:'/runtime/llama-server --port 8080',started:'Fri Oct 30 09:24:48 2026'};
  const ui=setup(server);
  assert.match(ui.node('#controls').innerHTML,/<button id="stop" class="danger" >停止/);
  assert.equal(ui.node('#status-detail').textContent,'');
  ui.execute('request=async(url,payload)=>record(url,payload); refresh=async()=>{};');
  ui.node('#stop').handlers.click();
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(ui.dialogs.length,1);
  assert.ok(ui.dialogs[0].includes(server.command));
  assert.deepEqual(ui.calls,[{url:'/api/stop',payload:{pid:server.pid,command:server.command,started:server.started}}]);
});

// 見込み: 停止可否と処理中をボタン・handler両方で守る。合格: 不可/処理中/取消で停止POSTゼロ。
// 範囲: 模擬DOM/HTTP/dialog。OSの停止権限はSwift試験の担当。
test('unavailable, busy, and cancelled stop controls never send a request', async () => {
  for (const mode of ['unavailable','busy','cancel']) {
    const ui=setup({running:true,trusted:false,can_stop:mode!=='unavailable',stop_reason:'別のユーザー',pid:123,command:'fixture',started:'fixture start'});
    ui.execute('request=async(url,payload)=>record(url,payload); refresh=async()=>{};');
    if(mode==='busy') ui.execute('busy=true;render();');
    if(mode==='cancel') ui.execute('confirm=()=>false;');
    if(mode!=='cancel') assert.match(ui.node('#controls').innerHTML,/<button id="stop" class="danger" disabled>/);
    if(mode==='unavailable') assert.equal(ui.node('#status-detail').textContent,'別のユーザー');
    ui.node('#stop').handlers.click();
    await new Promise(resolve=>setImmediate(resolve));
    assert.deepEqual(ui.calls,[]);
  }
});
