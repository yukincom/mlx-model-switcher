const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const source = fs.readFileSync(path.join(__dirname, '../src/mlx_activation_lab/server_web/app.js'), 'utf8');
function setup(server = {running:false}, stored = null) {
  const nodes = new Map(), storage = new Map([['mlx8080-thinking', stored]]), calls = [];
  const node = selector => {
    if (!nodes.has(selector)) nodes.set(selector, {content:'fixture', value:'', handlers:{},
      addEventListener(name, callback) {this.handlers[name] = callback;}, classList:{toggle(){}}});
    return nodes.get(selector);
  };
  const fixture = {token:'fixture', server, default_alias:'a', models:[
    {alias:'a', model:'owner/a', name:'A', order:0}, {alias:'b', model:'owner/b', name:'B', order:1}], log:[]};
  const context = vm.createContext({document:{addEventListener(){},querySelector:node,querySelectorAll:()=>[],activeElement:null},
    localStorage:{getItem:key=>storage.get(key),setItem:(key,value)=>storage.set(key,value)}, window:{addEventListener(){}}, EventSource:class {addEventListener(){} close(){}},
    fetch:()=>new Promise(()=>{}), fixture, record:(url,payload)=>calls.push({url,payload:JSON.parse(JSON.stringify(payload))})});
  vm.runInContext(source, context);
  const execute = code => vm.runInContext(code, context);
  execute('current=fixture; render();');
  return {node,execute,context,fixture,storage,calls};
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
