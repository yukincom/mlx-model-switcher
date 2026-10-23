let token = document.querySelector('meta[name="server-token"]').content;
let current = null;
let renderPending = false;
let busy = false;
let selectedAlias = '';
let thinkingEnabled = localStorage.getItem('mlx8080-thinking') === 'true';
const checkedAliases = new Set();
let cachedModels = [];
let cachedNames = {};
let loadingModels = false;
let cacheError = '';
let sortOrder = localStorage.getItem('mlx8080-model-sort') || 'acquired';
const $ = selector => document.querySelector(selector);
const esc = value => String(value ?? '').replace(/[&<>"']/g, character => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
})[character]);
const size = bytes => bytes == null ? 'サイズ未取得' : `${(bytes / 1e9).toFixed(1)} GB`;
const active = item => current.server.running && [item.model, item.name].includes(current.server.active_model);
function message(text, error = false) {
  $('#message').textContent = text;
  $('#message').className = `message${error ? ' error' : ''}`;
}
function sortedModels() {
  return [...current.models].sort((a, b) => {
    const key = sortOrder === 'size' ? 'size_bytes' : 'acquired_at';
    return (b[key] ?? -1) - (a[key] ?? -1) || a.order - b.order;
  });
}
function render() {
  if (!current) return;
  const server = current.server;
  const running = server.running;
  // Loading/stopped is authoritative backend state. A deadline-based optimistic
  // state would remain stuck after the last event now that there is no polling.
  const waiting = Boolean(server.loading);
  const locked = running || waiting || busy;
  $('#state-pill').className = `pill ${running ? 'on' : 'off'}`;
  $('#state-pill').textContent = waiting ? '◌ 読込中' : running ? '● 稼働中' : '○ 停止中';
  $('#status-title').textContent = waiting ? 'モデルを読込中' : running ? '稼働中' : '停止中';
  const runningItem = current.models.find(item => active(item));
  const runningName = runningItem?.name || server.active_model || 'モデル名を取得できません';
  $('#model-name').textContent = running ? runningName : '—';
  $('#process-id').textContent = running ? `PID ${server.pid}` : '—';
  $('#status-detail').textContent = running
    ? (server.trusted ? '' : '起動記録を確認できないため、この画面からは停止できません。')
    : '';
  const items = sortedModels();
  if (!items.some(item => item.alias === selectedAlias)) selectedAlias = current.default_alias;
  $('#controls').innerHTML = `<div class="start-controls"><select id="start-alias" aria-label="起動するモデル">${running && !runningItem ? `<option value="">${esc(runningName)}</option>` : ''}${items.map(item => `<option value="${esc(item.alias)}">${item.alias === current.default_alias ? '★ ' : ''}${esc(item.name)} / ${esc(item.format)} / ${size(item.size_bytes)}</option>`).join('')}</select><label class="thinking-toggle${locked ? ' disabled' : ''}" title="停止中に変更できます"><input type="checkbox" id="thinking-enabled">Thinking ON</label><button id="start" ${locked || !items.length ? 'disabled' : ''}>${waiting ? '読込中…' : '起動'}</button>${running ? `<button id="stop" class="danger" ${server.trusted && !busy ? '' : 'disabled'}>停止</button>` : ''}</div>`;
  $('#start-alias').value = running ? (runningItem?.alias || '') : selectedAlias;
  $('#start-alias').disabled = locked;
  $('#start-alias').addEventListener('change', event => { selectedAlias = event.target.value; });
  const thinking = $('#thinking-enabled');
  thinking.disabled = locked;
  thinking.checked = running ? server.thinking === true : thinkingEnabled;
  thinking.indeterminate = running && typeof server.thinking !== 'boolean';
  thinking.title = thinking.indeterminate ? '現在の起動時Thinking設定は取得できません' : '停止中に変更できます';
  thinking.addEventListener('change', event => {
    thinkingEnabled = event.target.checked;
    localStorage.setItem('mlx8080-thinking', String(thinkingEnabled));
  });
  $('#start').addEventListener('click', () => {
    if (!locked) run('/api/start', {alias: selectedAlias, thinking: thinkingEnabled}, '起動を受け付けました。読込みを確認しています。');
  });
  $('#stop')?.addEventListener('click', () => {
    if (confirm(`8080のモデルを停止しますか？\n${runningName}\nPID: ${server.pid}`))
      run('/api/stop', {pid: server.pid, command: server.command}, '停止しました。');
  });
  $('#model-sort').value = sortOrder;
  for (const alias of checkedAliases) {
    if (!items.some(item => item.alias === alias)) checkedAliases.delete(alias);
  }
  $('#model-list').innerHTML = items.length ? items.map(item => {
    const isDefault = item.alias === current.default_alias;
    const inUse = active(item);
    return `<div class="model${checkedAliases.has(item.alias) ? ' selected' : ''}"><label class="model-check"><input type="checkbox" data-check="${esc(item.alias)}" aria-label="${esc(item.name)}を選択" ${checkedAliases.has(item.alias) ? 'checked' : ''} ${busy ? 'disabled' : ''}></label><div class="model-content"><div class="model-heading"><strong>${esc(item.name)}</strong>${isDefault || inUse ? `<span class="model-badges">${isDefault ? '<span class="model-badge">★ デフォルト</span>' : ''}${inUse ? '<span class="model-badge">● 起動中</span>' : ''}</span>` : ''}</div><div class="model-meta"><span class="model-id" title="${esc(item.model)}">${esc(item.model)}</span><span class="model-size">${size(item.size_bytes)}</span><span class="format-tag">${esc(item.format)}</span></div></div></div>`;
  }).join('') : '<p>登録モデルはありません。</p>';
  document.querySelectorAll('[data-check]').forEach(checkbox => checkbox.addEventListener('change', () => {
    if (checkbox.checked) checkedAliases.add(checkbox.dataset.check);
    else checkedAliases.delete(checkbox.dataset.check);
    checkbox.closest('.model').classList.toggle('selected', checkbox.checked);
    updateSelection();
  }));
  updateSelection();
  $('#restore-gguf').hidden = !current.gguf_restore_available;
  $('#restore-gguf').disabled = busy;
  renderCachedModels();
  $('#log').textContent = current.log?.length ? current.log.join('\n') : '起動ログはありません。';
}
async function readStatus() {
  try {
    const response = await fetch('/api/status', {cache: 'no-store'});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const latest = await response.json();
    const changed = !current || JSON.stringify(latest) !== JSON.stringify(current);
    current = latest;
    token = current.token;
    renderPending ||= changed;
    if (renderPending && (current.server.running || current.server.loading || document.activeElement?.tagName !== 'SELECT')) {
      render();
      renderPending = false;
    }
  } catch { message('管理アプリに接続できません。アプリを開き直してください。', true); }
}
// Do not add periodic status polling. The user explicitly requires updates only
// when state changes: idle/closed windows must not repeatedly scan models or start Python.
// Native filesystem/process notifications and all API mutations use the same event stream,
// so changes made by Codex also reach this UI. Initial/reconnected views read one snapshot.
let refreshRequest = null;
let refreshAgain = false;
let nativeVisible = true;
let events = null;
function updatesVisible() { return nativeVisible && !document.hidden; }
async function refresh() {
  if (refreshRequest) { refreshAgain = true; return refreshRequest; }
  refreshRequest = (async () => {
    do {
      refreshAgain = false;
      await readStatus();
    } while (refreshAgain && updatesVisible());
  })();
  try { await refreshRequest; }
  finally { refreshRequest = null; }
}
function disconnectUpdates() {
  events?.close();
  events = null;
  refreshAgain = false;
}
function connectUpdates() {
  if (!updatesVisible()) { disconnectUpdates(); return; }
  if (events) return;
  const stream = new EventSource('/api/events');
  events = stream;
  const activeStream = () => events === stream && updatesVisible();
  stream.addEventListener('open', () => { if (activeStream()) { refresh(); refreshCachedModels(); } });
  stream.addEventListener('changed', () => { if (activeStream()) refresh(); });
  stream.addEventListener('error', () => {
    if (activeStream()) message('管理アプリとの接続を確認しています…', true);
  });
}
document.addEventListener('visibilitychange', connectUpdates);
document.addEventListener('focusout', event => {
  // Apply the deferred snapshot when a select closes; do not fetch it again.
  if (event.target?.tagName === 'SELECT' && renderPending) {
    render();
    renderPending = false;
  }
});
window.addEventListener('mlx-visibility', event => {
  nativeVisible = event.detail === true;
  connectUpdates();
});
window.addEventListener('pagehide', disconnectUpdates);
window.addEventListener('pageshow', connectUpdates);

async function request(path, payload) {
  const response = await fetch(path, {method: 'POST',
    headers: {'Content-Type': 'application/json', 'X-Server-Token': token}, body: JSON.stringify(payload)});
  const body = await response.json();
  if (response.status === 403) {
    await refresh();
    throw new Error('管理アプリが再起動しました。操作をもう一度実行してください。');
  }
  if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
  return body.result;
}
async function run(path, payload, success) {
  if (busy) return false;
  busy = true;
  render();
  try {
    await request(path, payload);
    message(success);
    await refresh();
    return true;
  } catch (error) { message(error.message, true); return false; }
  finally { busy = false; render(); }
}
function selectedModels() {
  return sortedModels().filter(item => checkedAliases.has(item.alias));
}
function updateSelection() {
  const items = selectedModels();
  const one = items.length === 1;
  const protectedModel = items.some(item => active(item));
  const unknownModel = current.server.running && !current.server.active_model;
  $('#selection-count').textContent = `${items.length}件選択`;
  $('#select-all').checked = items.length > 0 && items.length === current.models.length;
  $('#select-all').indeterminate = items.length > 0 && items.length < current.models.length;
  $('#select-all').disabled = busy || !current.models.length;
  $('#rename-selected').disabled = busy || !one;
  $('#default-selected').disabled = busy || !one || items[0]?.alias === current.default_alias;
  $('#remove-selected').disabled = busy || !items.length || protectedModel || unknownModel;
  const deleteButton = $('#delete-selected');
  deleteButton.disabled = busy || !items.length || protectedModel || unknownModel;
  deleteButton.title = unknownModel ? '起動中のモデルを確認できません。'
    : protectedModel ? '起動中のモデルは削除できません。' : '';
}
async function removeSelected(body) {
  if (busy) return;
  const items = selectedModels();
  if (items.some(item => active(item)) || (current.server.running && !current.server.active_model)) return;
  const aliases = items.map(item => item.alias);
  if (!aliases.length) return;
  busy = true;
  render();
  try {
    const preview = await request('/api/delete-preview', {aliases, body});
    const listing = preview.items.map(item => body
      ? `${item.name} — ${size(item.bytes)} / ${item.file_count}ファイル\n${item.location}`
      : item.name).join('\n\n');
    const detail = body
      ? `${preview.items.length}件のモデル本体と登録を削除します。元に戻せません。\n選択モデルの共有キャッシュは他のアプリからも使えなくなります。GGUFはMTP用・画像用ファイルも対象です。\n合計 ${size(preview.bytes)}\n\n${listing}`
      : `${preview.items.length}件の登録を起動一覧から外します。モデル本体は残ります。\n\n${listing}`;
    if (!confirm(detail)) return;
    await request('/api/delete', {aliases, body, fingerprint: preview.fingerprint});
    message(body ? '選択したモデル本体と登録を削除しました。' : '選択した登録を外しました。モデル本体は残っています。');
    checkedAliases.clear();
  } catch (error) { message(error.message, true); }
  finally { await refresh(); await refreshCachedModels(); busy = false; render(); }
}
function renderCachedModels() {
  const select = $('#cached-model');
  const selected = select.value;
  const registered = new Set((current?.aliases || []).map(item => item.model));
  const available = cachedModels.filter(model => !registered.has(model));
  select.innerHTML = loadingModels ? '<option value="">読み込み中…</option>' : available.length
    ? '<option value="">モデルを選択してください</option>' + available.map(model => `<option value="${esc(model)}">${esc(current?.display_names?.[model] || cachedNames[model] || model)}</option>`).join('')
    : '<option value="">追加できるモデルはありません</option>';
  if (!loadingModels && available.includes(selected)) select.value = selected;
  select.disabled = busy || loadingModels || !available.length;
  $('#register-model').disabled = busy || loadingModels || !select.value;
  $('#refresh-cache').disabled = busy || loadingModels;
  $('#cache-status').textContent = cacheError || (loadingModels ? 'キャッシュを確認中…' : available.length ? `${available.length}件のモデルを追加できます。` : '追加できる保存済みMLXモデルはありません。');
}
async function refreshCachedModels() {
  if (loadingModels) return;
  loadingModels = true; cacheError = ''; renderCachedModels();
  try {
    const response = await fetch('/api/cached-models', {cache: 'no-store'});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const result = await response.json();
    cachedModels = result.models;
    cachedNames = result.names || {};
  } catch { cachedModels = []; cacheError = 'キャッシュを読み取れませんでした。一覧を更新してください。'; }
  finally { loadingModels = false; renderCachedModels(); }
}
$('#select-all').addEventListener('change', event => {
  checkedAliases.clear();
  if (event.target.checked) current.models.forEach(item => checkedAliases.add(item.alias));
  render();
});
$('#rename-selected').addEventListener('click', () => {
  const items = selectedModels();
  if (busy || items.length !== 1) return;
  const name = prompt('表示名（1〜60文字）', items[0].name);
  if (name !== null) run('/api/rename', {alias: items[0].alias, name}, '表示名を変更しました。');
});
$('#default-selected').addEventListener('click', async () => {
  const items = selectedModels();
  if (busy || items.length !== 1) return;
  if (await run('/api/default', {alias: items[0].alias}, 'デフォルトを変更しました。')) {
    selectedAlias = current.default_alias;
    render();
  }
});
$('#remove-selected').addEventListener('click', () => removeSelected(false));
$('#delete-selected').addEventListener('click', () => removeSelected(true));
$('#model-sort').addEventListener('change', event => {
  sortOrder = event.target.value;
  localStorage.setItem('mlx8080-model-sort', sortOrder);
  render();
});
$('#restore-gguf').addEventListener('click', () => run('/api/gguf/register', {}, '保存済みGGUFモデルを起動一覧に戻しました。'));
$('#refresh-cache').addEventListener('click', refreshCachedModels);
$('#cached-model').addEventListener('change', renderCachedModels);
$('#register-form').addEventListener('submit', event => {
  event.preventDefault();
  run('/api/register', Object.fromEntries(new FormData(event.target).entries()), '起動リストに追加しました。');
});
connectUpdates();
