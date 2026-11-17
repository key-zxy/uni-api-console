'use strict';
const $ = id => document.getElementById(id);

/* Local login persistence: token only, never sent to third parties. */
const storageKey = 'uni-api.audit.login.v1';
let token = '';
let offset = 0;
let selected = null;
let pollTimer;
let setupConfigured = true;
let managementData = null;
let currentPage = 'dashboard';
let cachedAuditRows = [], cachedUsageModels = [];

const pageNames = {dashboard: '仪表盘', keys: 'API 密钥', records: '使用记录', upstream: '上游接入', usage: '余量'};

const endpointInput = $('api-base-url');
if (endpointInput && typeof location !== 'undefined') endpointInput.value = location.origin + '/v1';
const copyEndpoint = $('copy-api-url');
if (copyEndpoint) copyEndpoint.onclick = async () => {
  const status = $('copy-api-status');
  try {
    if (navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText(endpointInput.value);
    else { endpointInput.focus(); endpointInput.select(); if (!document.execCommand('copy')) throw new Error('copy unavailable'); }
    status.textContent = '已复制接口地址';
  } catch {
    endpointInput.focus(); endpointInput.select();
    status.textContent = '请手动复制选中的地址';
  }
};

async function api(path, options = {}) {
  const response = await fetch('/_audit/api/' + path, {
    ...options, cache: 'no-store', credentials: 'omit',
    headers: {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}
  });
  if (response.status === 401 || response.status === 403) logout();
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}));
    throw new Error(typeof detail.error === 'string' ? detail.error : '操作失败（' + response.status + '）；请检查输入或原生服务状态，更新结果不确定时请先核对并回滚。');
  }
  return response.json();
}

function action(fn) {
  let busy = false;
  return async event => {
    if (event) event.preventDefault();
    if (busy) return;
    const form = event?.currentTarget?.tagName === 'FORM' ? event.currentTarget : null;
    let status = form?.querySelector('.form-message');
    if (form && !status) { status = document.createElement('p'); status.className = 'form-message'; status.setAttribute('role', 'status'); form.append(status); }
    const submit = form?.querySelector('button:not([type="button"])');
    busy = true;
    if (submit) submit.disabled = true;
    try { $('message').textContent = ''; if (status) status.textContent = ''; await fn(); }
    catch (error) { $('message').textContent = error.message; if (status) status.textContent = error.message; }
    finally { busy = false; if (submit) submit.disabled = false; }
  };
}

const statusClass = status => {
  const s = Number(status);
  if (!s) return 'neutral';
  if (s >= 200 && s < 300) return 'ok';
  if (s >= 400 && s < 500) return 'warn';
  return 'danger';
};
const statusText = status => {
  const s = Number(status);
  if (!s) return '—';
  if (s >= 200 && s < 300) return '成功';
  if (s >= 400 && s < 500) return '客户端错误';
  if (s >= 500) return '服务端错误';
  return String(s);
};
const unknown = value => value === null || value === undefined ? '未知' : value;
const num = value => typeof value === 'number' ? value : null;
const tokensText = row => {
  const input = num(row.prompt_tokens), output = num(row.completion_tokens), cached = num(row.cached_tokens);
  if (input === null && output === null && cached === null) return '未知';
  return `${unknown(input)} / ${unknown(output)} / ${unknown(cached)}`;
};
const cacheRatio = row => {
  const input = num(row.prompt_tokens), cached = num(row.cached_tokens);
  return input && cached !== null && input > 0 ? (cached / input * 100).toFixed(1) + '%' : '—';
};
const tokensPerSecond = row => {
  const output = num(row.completion_tokens), duration = num(row.duration), first = num(row.first_byte);
  if (!output || duration === null) return '—';
  const generation = first !== null ? duration - first : duration;
  return generation > 0.05 ? (output / generation).toFixed(1) + ' t/s' : '—';
};
const timingText = row => {
  const parts = [];
  if (num(row.first_byte) !== null) parts.push((row.first_byte * 1000).toFixed(0) + ' ms');
  if (num(row.duration) !== null) parts.push(row.duration.toFixed(1) + ' s');
  return parts.length ? parts.join(' / ') : '—';
};

async function refresh() {
  const params = new URLSearchParams(new FormData($('filters')));
  params.set('offset', offset);
  const rows = await api('requests?' + params);
  cachedAuditRows = rows;
  renderRecords(rows);
}
function renderRecords(rows) {
  const body = $('rows');
  body.replaceChildren();
  $('previous').disabled = offset === 0;
  $('next').disabled = rows.length < 100;
  $('record-page').textContent = '第 ' + (Math.floor(offset / 100) + 1) + ' 页 · 本页 ' + rows.length + ' 条';
  if (!rows.length) {
    const tr = document.createElement('tr');
    const td = document.createElement('td');
    td.colSpan = 9;
    td.className = 'empty';
    td.innerHTML = '<strong>暂无匹配记录</strong>尝试调整筛选条件，或先通过客户端发起一次请求。';
    tr.append(td);
    body.append(tr);
    return;
  }
  for (const row of rows) {
    const tr = document.createElement('tr');
    const cls = statusClass(row.status);
    const cells = [
      new Date(row.started * 1000).toLocaleString('zh-CN', {hour12: false}),
      (row.client_name || '未知') + (row.client_pool ? ' · ' + row.client_pool : ''),
      modelTagHtml(row),
      tokensText(row),
      cacheRatio(row),
      tokensPerSecond(row),
      timingText(row),
      `<span class="status-badge status-${cls}">${statusText(row.status)}${row.complete ? '' : ' · 截断'}</span>`,
    ];
    const labels = ['时间', '令牌 / 池', '上游 / 模型', '输入 / 输出 / 缓存', '缓存比例', '吐字速度', '首字 / 耗时', '状态'];
    cells.forEach((value, i) => {
      const td = document.createElement('td'); td.dataset.label = labels[i];
      if (i === 2) { td.className = 'model-cell'; td.innerHTML = value; }
      else if (i === 7) {
        const badge = makeTag(String(row.status ?? '—') + ' ' + statusText(row.status), 'status-' + cls);
        td.append(badge);
        if (!row.complete) td.append(makeTag('未完整捕获', 'neutral'));
      } else if (i === 1) { td.append(makeTag(row.client_name || '未识别令牌', 'neutral')); if (row.client_pool) td.append(makeTag(row.client_pool, 'p-tag')); }
      else td.textContent = String(value ?? '');
      tr.append(td);
    });
    const td = document.createElement('td');
    td.dataset.label = '上下文';
    const button = document.createElement('button');
    button.className = 'btn ghost';
    button.textContent = '查看';
    button.onclick = action(async () => {
      const detail = await api('requests/' + row.id);
      selected = row.id;
      $('delete').disabled = false;
      $('detail').textContent = JSON.stringify(readableRecord(detail), null, 2);
      $('audit-dialog').showModal();
    });
    td.append(button);
    tr.append(td);
    body.append(tr);
  }
}

async function start() {
  const settings = await api('settings');
  $('days').value = settings.retention_days;
  $('console').hidden = false;
  document.body.classList.remove('signed-out');
  try { localStorage.setItem(storageKey, token); } catch {}
  showPage(currentPage);
  await setupStatus();
  await refresh();
  try { await loadUsage(); } catch { $('usage-totals').textContent = '用量统计暂不可用。'; }
  try { await managementRefresh(); }
  catch {
    $('management-summary').textContent = '原生管理接口暂不可用；容器可能正在等待配置或启动，请稍后刷新。审计与用量统计仍可独立查看。';
  }
}

$('login').onsubmit = action(async () => {
  token = $('token').value;
  $('token').value = '';
  try { localStorage.removeItem(storageKey); } catch {}
  await start();
});

function logout() {
  clearModelPickers.forEach(clear => clear());
  document.querySelectorAll('dialog[open]').forEach(dialog => dialog.close());
  clearTimeout(pollTimer);
  document.body.classList.add('signed-out');
  $('console').hidden = true;
  token = '';
  try { localStorage.removeItem(storageKey); } catch {}
  $('detail').textContent = ''; $('snapshot').textContent = ''; $('usage-totals').replaceChildren(); $('usage-table').replaceChildren(); $('usage-chart').replaceChildren();
  $('rows').replaceChildren();
  managementData = null;
  $('provider-list').replaceChildren(); $('pool-choices').replaceChildren(); $('pool-list').replaceChildren();
  selected = null; offset = 0; $('delete').disabled = true; $('token').value = '';
  cachedAuditRows = []; cachedUsageModels = []; modelProviderMap.clear();
  $('new-key-result').hidden = true;
  $('key-list').replaceChildren(); $('runtime-status').textContent = ''; $('setup-status').textContent = '';
  $('provider-key').value = ''; $('client-key').textContent = ''; $('management-output').textContent = '';
  $('management-summary').replaceChildren(); $('client-pool').replaceChildren();
  $('setup-key').value = ''; $('setup-client-key').textContent = ''; $('setup-result').hidden = true;
  $('setup-form').hidden = true;
  $('stat-providers').textContent = '—'; $('stat-pools').textContent = '—'; $('stat-clients').textContent = '—';
}
$('logout').onclick = logout;

$('filters').onsubmit = action(async () => { offset = 0; await refresh(); });
$('previous').onclick = action(async () => { offset = Math.max(0, offset - 100); await refresh(); });
$('next').onclick = action(async () => { offset += 100; await refresh(); });
$('delete').onclick = action(async () => {
  if (selected === null) return;
  await api('requests/' + selected, {method: 'DELETE'});
  selected = null; $('delete').disabled = true; $('detail').textContent = ''; await refresh();
});
$('retention').onsubmit = action(async () => {
  await api('settings', {method: 'PUT', body: JSON.stringify({retention_days: Number($('days').value)})});
  await refresh();
});
$('cleanup').onclick = action(async () => { await api('cleanup', {method: 'POST'}); await refresh(); });
/* Usage statistics: local audit aggregation, one-api style daily chart. */
const usageDaysEl = $('usage-days');
if (usageDaysEl) usageDaysEl.onchange = action(loadUsage);

async function loadUsage() {
  if (!usageDaysEl) return;
  const days = Number(usageDaysEl.value) || 7;
  const rows = await api('usage?days=' + days);
  const totals = rows.totals || {};
  const fmt = n => (typeof n === 'number' ? n.toLocaleString('zh-CN') : '未知');
  $('usage-totals').replaceChildren(
    ...[['请求数', fmt(totals.requests)], ['输入 tokens', fmt(totals.prompt)], ['输出 tokens', fmt(totals.completion)], ['缓存 tokens', fmt(totals.cached)]]
      .map(([label, value]) => {
        const item = document.createElement('div');
        item.className = 'usage-total';
        const span = document.createElement('span'); span.textContent = label;
        const strong = document.createElement('strong'); strong.textContent = value;
        item.append(span, strong);
        return item;
      }));
  drawUsageChart(rows.daily || []);
  renderUsageTable(rows.models || []);
}

function drawUsageChart(daily) {
  const svg = $('usage-chart');
  if (!svg) return;
  svg.replaceChildren();
  const NS = 'http://www.w3.org/2000/svg';
  const el = (tag, attrs) => {
    const node = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
    return node;
  };
  const W = 720, H = 220, pad = {left: 64, right: 10, top: 14, bottom: 26};
  const iw = W - pad.left - pad.right, ih = H - pad.top - pad.bottom;
  const max = Math.max(1, ...daily.map(d => d.total || 0));
  const steps = 4;
  for (let i = 0; i <= steps; i++) {
    const y = pad.top + ih - ih * i / steps;
    svg.append(el('line', {x1: pad.left, y1: y, x2: W - pad.right, y2: y, stroke: 'var(--line)', 'stroke-width': 1}));
    const label = el('text', {x: pad.left - 6, y: y + 4, 'text-anchor': 'end', 'font-size': 10, fill: 'var(--muted)'});
    label.textContent = Math.round(max * i / steps).toLocaleString('zh-CN');
    svg.append(label);
  }
  const n = daily.length || 1;
  const bw = Math.max(6, Math.min(48, iw / n * 0.6));
  daily.forEach((d, i) => {
    const cx = pad.left + iw * (i + 0.5) / n;
    const total = d.total || 0;
    const h = ih * total / max;
    const bar = el('rect', {x: cx - bw / 2, y: pad.top + ih - h, width: bw, height: h, rx: 3, fill: 'var(--brand)'});
    const title = el('title', {});
    title.textContent = `${d.day}: 输入 ${d.prompt} · 输出 ${d.completion} · 缓存 ${d.cached}`;
    bar.append(title);
    svg.append(bar);
    if (n <= 16 || i % Math.ceil(n / 12) === 0) {
      const day = el('text', {x: cx, y: H - 8, 'text-anchor': 'middle', 'font-size': 10, fill: 'var(--muted)'});
      day.textContent = String(d.day).slice(5);
      svg.append(day);
    }
  });
}

function renderUsageTable(models) {
  cachedUsageModels = models;
  const table = $('usage-table');
  if (!table) return;
  table.replaceChildren();
  const head = document.createElement('tr');
  for (const text of ['模型', '请求数', '输入', '输出', '缓存']) {
    const th = document.createElement('th'); th.textContent = text; head.append(th);
  }
  table.append(head);
  for (const m of models) {
    const tr = document.createElement('tr');
    for (const value of [m.model, m.requests, m.prompt, m.completion, m.cached]) {
      const td = document.createElement('td');
      if (value === m.model) td.innerHTML = modelTagHtml({model:m.model});
      else td.textContent = typeof value === 'number' ? value.toLocaleString('zh-CN') : String(value ?? '未知');
      tr.append(td);
    }
    table.append(tr);
  }
}

$('config').onclick = action(async () => { $('snapshot').textContent = JSON.stringify(await api('config'), null, 2); });

/* Setup / bootstrap */
async function setupStatus() {
  const status = await api('setup');
  setupConfigured = status.configured;
  $('setup-status').textContent = status.configured ? '已有本地配置，正在检查原生服务…' : '尚未配置：添加第一个上游后自动启动。';
  if (!status.configured) {
    $('provider-list').textContent = '尚未接入渠道，点击新增渠道开始。';
    $('runtime-status').textContent = '等待首次配置，请前往上游接入。';
    showPage('upstream');
  } else pollNative();
  $('setup-form').hidden = status.configured;
  $('provider-form').hidden = !status.configured;
}

$('setup-form').onsubmit = action(async () => {
  const payload = {api: $('setup-key').value, provider: $('setup-provider').value, base_url: $('setup-url').value, models: parseModels('setup-models')};
  $('setup-key').value = '';
  $('setup-submit').disabled = true;
  try {
    const result = await api('setup', {method: 'POST', body: JSON.stringify(payload)});
    $('setup-client-key').textContent = result.client_api_key;
    $('setup-result').hidden = false;
    setupConfigured = true;
    $('provider-dialog').close();
    $('setup-form').hidden = true;
    $('setup-status').textContent = '本地配置已创建：' + result.provider;
    pollNative();
  } finally {
    payload.api = '';
    $('setup-submit').disabled = false;
  }
});

const lines = id => $(id).value.split('\n').map(s => s.trim()).filter(Boolean);

let modelProviderMap = new Map();
function buildModelProviderMap() {
  modelProviderMap.clear();
  if (!managementData) return;
  for (const p of managementData.providers) {
    for (const m of p.models) {
      const alias = typeof m === 'string' ? m : Object.values(m)[0];
      const source = typeof m === 'string' ? m : Object.keys(m)[0];
      for (const key of [alias, source, p.provider + '/' + alias]) {
        if (!modelProviderMap.has(key)) modelProviderMap.set(key, new Set());
        modelProviderMap.get(key).add(p.provider);
      }
    }
  }
}
function makeTag(text, type) {
  const node = document.createElement('span'); node.className = 'tag ' + type; node.textContent = text; return node;
}
function providerLabel(model) {
  if (!model) return null;
  const candidates = modelProviderMap.get(model);
  const providers = (managementData?.providers || []).map(p => p.provider);
  if (candidates && candidates.size) {
    const ordered = [...candidates].sort((a, b) => {
      const ia = providers.indexOf(a), ib = providers.indexOf(b);
      return (ia < 0 ? 999 : ia) - (ib < 0 ? 999 : ib);
    });
    return { names: ordered, count: ordered.length };
  }
  // A namespaced model id already carries its channel: provider/alias.
  const slash = model.indexOf('/');
  if (slash > 0) {
    const prefix = model.slice(0, slash);
    if (providers.includes(prefix)) return { names: [prefix], count: 1 };
  }
  return null;
}

/* 模型列显示客户端请求的别名；上游真实模型名放进 title。 */
function decodeStoredBody(base64Text) {
  try {
    const binary = atob(base64Text);
    const bytes = Uint8Array.from(binary, ch => ch.charCodeAt(0));
    const text = new TextDecoder('utf-8', {fatal: false}).decode(bytes);
    return stripBase64InText(text);
  } catch { return '[二进制内容，已省略]'; }
}

function stripBase64InText(text) {
  return String(text).replace(/data:[\w.+-]+\/[\w.+-]+;base64,[A-Za-z0-9+/=]{80,}/g, '[base64 已省略]')
                     .replace(/[A-Za-z0-9+/]{400,}={0,2}/g, m => '[base64 ' + m.length + ' 字符已省略]');
}

function readableRecord(record) {
  const view = {...record};
  for (const field of ['request_body_base64', 'response_body_base64']) {
    if (typeof view[field] === 'string') {
      const text = decodeStoredBody(view[field]);
      view[field.replace('_base64', '_text')] = text.length > 1200 ? text.slice(0, 1200) + ' …（共 ' + text.length + ' 字符）' : text;
      delete view[field];
    }
  }
  return view;
}

function modelTagHtml(row) {
  const alias = row.model || row.actual_model || null;
  const upstream = row.actual_model && row.actual_model !== alias ? row.actual_model : null;
  if (!alias) {
    return '<span class="tag neutral m-tag" title="' + escapeHtml(row.path || '') + '">' + escapeHtml(row.path || '未知') + '</span>';
  }
  const resolved = providerLabel(alias);
  let hint = null;
  try { hint = row.upstream_hint ? JSON.parse(row.upstream_hint) : null; } catch { hint = null; }
  /* 上游自报指纹：按关键字取，不绑定任何一家网关的头名 */
  const hintValues = hint ? Object.entries(hint)
    .filter(([key]) => /provider|channel|upstream|backend|vendor|served/i.test(key))
    .map(([, value]) => String(value))
    .filter(Boolean) : [];
  const vendor = hintValues[0] || '';
  /* uni-api 的请求统计给出精确渠道；拿不到时才回退到按配置推断 */
  const exact = row.upstream_channel || '';
  const parts = [exact || (resolved ? resolved.names.join(' · ') : '渠道未在配置中')];
  if (vendor) parts.push('上游 ' + vendor);
  const provider = parts.join(' · ');
  const hintText = hintValues.length ? '；上游自报 ' + hintValues.join(' / ') : '';
  const basis = exact ? 'uni-api 请求统计实测渠道' : '按当前配置匹配（该次请求的渠道未知）';
  const title = basis + (upstream ? '；上游实际模型 ' + upstream : '') + hintText;
  return '<span class="tag-pair"><span class="tag p-tag" title="' + escapeHtml(title) + '">' + escapeHtml(provider) + '</span>'
    + '<span class="tag m-tag" title="' + escapeHtml(escapeHtml(upstream || alias)) + '">' + escapeHtml(alias) + '</span></span>';
}
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":"&#39;"}[c]));
}

async function managementRefresh() {
  const data = await api('manage');
  renderManagement(data);
  $('management-output').textContent = JSON.stringify(data, null, 2);
  $('management-summary').textContent = `${data.providers.length} 个渠道 · ${Object.keys(data.pools).length} 个授权池 · ${data.clients.length} 个客户端`;
  $('management-warning').textContent = '仅限本地单写入者：展开后的环境密钥可能以明文保存到原生配置和版本库；环境引用不会保留。不做目标主机白名单或公共 DNS 检查，不提供 DNS 重绑定防护，也不约束原生重定向或代理目标。';
  $('stat-providers').textContent = data.providers.length;
  $('stat-pools').textContent = Object.keys(data.pools).length;
  $('stat-clients').textContent = data.clients.length;

  const selectedPool = $('client-pool').value;
  $('client-pool').replaceChildren();
  for (const pool of Object.keys(data.pools)) {
    const option = document.createElement('option');
    option.value = pool; option.textContent = pool;
    $('client-pool').append(option);
    if (pool === selectedPool) option.selected = true;
  }
  buildModelProviderMap();
  if (cachedAuditRows.length) renderRecords(cachedAuditRows);
  if (cachedUsageModels.length) renderUsageTable(cachedUsageModels);
}
$('management-refresh').onclick = action(managementRefresh);

$('provider-form').onsubmit = action(async () => {
  if ($('provider-name').readOnly && managementData) {
    const provider = $('provider-name').value;
    const pools = Object.entries(managementData.pools).filter(([, models]) => models.some(m => m.startsWith(provider + '/'))).map(([name]) => name);
    const clients = managementData.clients.filter(c => pools.includes(c.pool)).map(c => c.name);
    if (!confirm('保存渠道 ' + provider + '？\n关联池：' + (pools.join(', ') || '无') + '\n关联客户端：' + (clients.join(', ') || '无') + '\n移除已绑定模型会被阻止。')) return;
  }
  const payload = {
    action: 'provider',
    provider: $('provider-name').value,
    base_url: $('provider-url').value,
    api: $('provider-key').value,
    models: lines('provider-models').map(line => {
      const pair = line.split('=');
      return pair.length === 2 ? {[pair[0].trim()]: pair[1].trim()} : line;
    })
  };
  try {
    await api('manage', {method: 'POST', body: JSON.stringify(payload)});
    $('provider-key').value = '';
    $('provider-dialog').close();
  } finally { payload.api = ''; }
  await managementRefresh();
});

function openPool(name, models = []) {
  $('pool-form').reset();
  $('pool-name').value = name || '';
  $('pool-name').readOnly = !!name;
  $('pool-dialog-title').textContent = name ? '编辑模型池' : '创建模型池';
  $('pool-choices').querySelectorAll('input').forEach(box => { box.checked = models.includes(box.value); });
  const clients = (managementData?.clients || []).filter(c => c.pool === name).map(c => c.name);
  $('pool-impact').textContent = clients.length ? '保存将同步：' + clients.join('、') : '尚无密钥绑定此池';
  filterPoolChoices();
  $('pool-dialog').showModal();
}
function filterPoolChoices() {
  const search = $('pool-search').value.trim().toLowerCase();
  $('pool-choices').querySelectorAll('label').forEach(label => { label.hidden = !label.textContent.toLowerCase().includes(search); });
}
$('new-pool').onclick = () => openPool();
$('pool-search').oninput = filterPoolChoices;
$('pool-form').onsubmit = action(async () => {
  await api('manage', {method: 'POST', body: JSON.stringify({
    action: 'pool', name: $('pool-name').value,
    models: [...$('pool-choices').querySelectorAll('input:checked')].map(box => box.value)
  })});
  $('pool-dialog').close();
  await managementRefresh();
});
let pendingNativeRole = null;
function openClient(client) {
  $('client-form').reset();
  const isNative = !!(client && client.isNative);
  pendingNativeRole = isNative ? client.role : null;
  const nameField = $('client-name').closest('label');
  nameField.hidden = isNative;
  $('client-name').required = !isNative;
  $('client-name').readOnly = !!client && !isNative;
  $('client-name').value = client && !isNative ? client.name : '';
  if (client) $('client-pool').value = client.pool;
  $('client-dialog-title').textContent = isNative ? '绑定原生密钥到模型池' : (client ? '编辑密钥模型池' : '创建密钥');
  $('client-save').textContent = isNative ? '绑定模型池' : (client ? '保存模型池' : '签发密钥');
  $('client-dialog').showModal();
}
$('new-client').onclick = () => openClient();
$('client-form').onsubmit = action(async () => {
  if (pendingNativeRole !== null) {
    await api('manage', {method: 'POST', body: JSON.stringify({action: 'native_pool', role: pendingNativeRole, pool: $('client-pool').value})});
    pendingNativeRole = null;
    $('client-dialog').close();
    await managementRefresh();
    return;
  }
  const editing = $('client-name').readOnly;
  const result = await api('manage', {method: 'POST', body: JSON.stringify({action: editing ? 'client_update' : 'client', name: $('client-name').value, pool: $('client-pool').value})});
  $('client-dialog').close();
  if (!editing && result.client_api_key) {
    $('client-key').textContent = result.client_api_key;
    $('new-key-result').hidden = false;
  }
  await managementRefresh();
});
$('copy-client-key').onclick = action(async () => {
  const text = $('client-key').textContent;
  if (!text) return;
  if (navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText(text);
  else {
    const range = document.createRange(); range.selectNodeContents($('client-key'));
    const selection = window.getSelection(); selection.removeAllRanges(); selection.addRange(range);
    if (!document.execCommand('copy')) throw new Error('请手动复制选中的新密钥');
    selection.removeAllRanges();
  }
  $('copy-client-key').textContent = '已复制';
});
$('dismiss-client-key').onclick = () => { $('client-key').textContent = ''; $('new-key-result').hidden = true; };

$('client-revoke').onclick = action(async () => {
  await api('manage', {method: 'POST', body: JSON.stringify({action: 'revoke', name: $('client-name').value})});
  $('client-key').textContent = '';
  await managementRefresh();
});
$('rollback').onclick = action(async () => {
  if (!confirm('回滚会恢复旧 Provider、客户端 key 和池绑定，可能恢复已撤销凭据。确认回滚？')) return;
  await api('manage', {method: 'POST', body: JSON.stringify({action: 'rollback'})});
  $('client-key').textContent = '';
  await managementRefresh();
});

const parseModels = id => lines(id).map(line => {
  const pair = line.split('=');
  return pair.length === 2 ? {[pair[0].trim()]: pair[1].trim()} : line;
});

/* 上游余量：每个渠道一个进度条，余量方式可自定义。 */
const MODE_LABELS = {none: '不查询', openrouter: 'OpenRouter', openai_billing: 'OpenAI 计费', amd: 'AMD 日预算', manual: '本地统计', custom: '自定义'};
const UNIT_LABELS = {usd: '美元', cny: '元', credits: '点', count: '次', percent: '%', tokens: 'token'};
let balancesData = null;
let balanceLoaded = false;

function fmtAmount(value, unit) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '—';
  const number = Number(value);
  if (!Number.isFinite(number)) return '—';
  if (unit === 'tokens') {
    if (number >= 1e8) return (number / 1e8).toFixed(2) + ' 亿';
    if (number >= 1e4) return (number / 1e4).toFixed(1) + ' 万';
    return String(Math.round(number));
  }
  // Only money/points treat a six-figure limit as "effectively unlimited";
  // token and request counts must print their real value.
  if ((unit === 'usd' || unit === 'cny' || unit === 'credits') && number >= 1e6) return '∞';
  const text = Math.abs(number) >= 100 ? number.toFixed(0) : number.toFixed(2);
  if (unit === 'percent') return text + '%';
  if (unit === 'usd') return '$' + text;
  if (unit === 'cny') return '¥' + text;
  return text + (UNIT_LABELS[unit] ? ' ' + UNIT_LABELS[unit] : '');
}

function balanceCard(item) {
  const reading = item.reading;
  const mode = item.mode || 'none';
  const card = document.createElement('div');
  card.className = 'provider-card balance-card';
  card.dataset.provider = item.provider;
  const head = document.createElement('div');
  head.className = 'balance-head';
  const name = document.createElement('div');
  name.className = 'balance-name';
  const title = document.createElement('strong');
  title.textContent = item.provider;
  const url = document.createElement('span');
  url.className = 'balance-url';
  url.textContent = item.base_url || '';
  name.append(title, url);
  const badge = document.createElement('span');
  badge.className = 'badge';
  badge.dataset.mode = mode;
  badge.textContent = MODE_LABELS[mode] || mode;
  head.append(name, badge);
  card.append(head);

  if (mode !== 'none' && reading) {
    const percent = reading.ok ? reading.percent : null;
    const bar = document.createElement('div');
    bar.className = 'balance-bar' + (reading.ok ? '' : ' danger') + (percent !== null && percent !== undefined && percent <= 20 ? ' warn' : '');
    const fill = document.createElement('span');
    fill.style.width = (percent !== null && percent !== undefined ? Math.max(2, Math.min(100, percent)) : 100) + '%';
    if (percent !== null && percent !== undefined) fill.dataset.percent = String(Math.round(percent * 10) / 10);
    bar.append(fill);
    card.append(bar);
    const meta = document.createElement('div');
    meta.className = 'balance-meta';
    if (!reading.ok) {
      meta.dataset.error = '1';
      meta.textContent = reading.error || '查询失败';
      card.classList.add('error');
    } else {
      meta.append(Object.assign(document.createElement('span'), {textContent: (reading.label || MODE_LABELS[mode]) + '：' + fmtAmount(reading.remaining, reading.unit)}));
      if (reading.total !== null && reading.total !== undefined) meta.append(Object.assign(document.createElement('span'), {textContent: '总量 ' + fmtAmount(reading.total, reading.unit)}));
      if (reading.used !== null && reading.used !== undefined) meta.append(Object.assign(document.createElement('span'), {textContent: '已用 ' + fmtAmount(reading.used, reading.unit)}));
      if (percent !== null && percent !== undefined) meta.append(Object.assign(document.createElement('span'), {textContent: '剩余 ' + (Math.round(percent * 10) / 10) + '%'}));
    }
    card.append(meta);
    const detail = Object.entries(reading.detail || {});
    if (reading.ok && detail.length) {
      const box = document.createElement('div');
      box.className = 'balance-detail';
      detail.forEach(([key, value]) => box.append(Object.assign(document.createElement('span'), {textContent: key + ' ' + value})));
      card.append(box);
    }
    if (typeof item.age === 'number' && item.age >= 0) {
      const age = document.createElement('p');
      age.className = 'hint';
      age.textContent = item.age < 90 ? '刚刚更新' : Math.max(1, Math.round(item.age / 60)) + ' 分钟前更新';
      card.append(age);
    }
  } else if (mode === 'none') {
    const hint = document.createElement('p');
    hint.className = 'hint';
    hint.textContent = '未配置余量方式，点「设置」选择查询接口。';
    card.append(hint);
  } else {
    const hint = document.createElement('p');
    hint.className = 'hint';
    hint.textContent = '还没有余量数据，点「刷新」或「设置」开始。';
    card.append(hint);
  }

  const actions = document.createElement('div');
  actions.className = 'balance-actions';
  const refresh = document.createElement('button');
  refresh.type = 'button';
  refresh.className = 'btn ghost';
  refresh.textContent = '刷新';
  refresh.onclick = action(() => refreshBalances({provider: item.provider}));
  const config = document.createElement('button');
  config.type = 'button';
  config.className = 'btn ghost';
  config.textContent = '设置';
  config.onclick = () => openBalanceDialog(item);
  actions.append(refresh, config);
  card.append(actions);
  return card;
}

async function loadBalances(options = {}) {
  const status = $('balances-status');
  if (status) status.textContent = '正在读取余量…';
  try {
    const data = options.force
      ? await api('balances/refresh', {method: 'POST', body: JSON.stringify({provider: options.provider || '', force: true})})
      : await api('balances');
    balancesData = data.providers || [];
    renderBalances();
    balanceLoaded = true;
    if (status) status.textContent = '';
  } catch (error) {
    if (status) status.textContent = error.message || '余量读取失败';
  }
}

function renderBalances() {
  const list = $('balances-list');
  if (!list) return;
  list.replaceChildren(...(balancesData || []).map(balanceCard));
}

async function refreshBalances(options = {}) {
  await loadBalances({force: true, provider: options.provider || ''});
  return balancesData;
}

function toggleBalanceCustom() {
  const mode = $('balance-mode').value;
  const custom = $('balance-custom-fields');
  if (custom) custom.hidden = mode !== 'custom';
  const manual = $('balance-manual-fields');
  if (manual) manual.hidden = !(mode === 'manual' || mode === 'openai_billing');
}

function openBalanceDialog(item) {
  const dialog = $('balance-dialog');
  if (!dialog) return;
  const custom = item.custom || {};
  $('balance-provider').value = item.provider;
  $('balance-mode').value = item.mode || 'none';
  $('balance-url').value = custom.url || '';
  $('balance-used-path').value = custom.used_path || '';
  $('balance-total-path').value = custom.total_path || '';
  $('balance-remaining-path').value = custom.remaining_path || '';
  $('balance-unit').value = custom.unit || 'usd';
  $('balance-label').value = custom.label || '';
  $('balance-limit').value = custom.limit_value === null || custom.limit_value === undefined ? '' : custom.limit_value;
  $('balance-reset').value = custom.reset || 'day';
  $('balance-price').value = custom.price_per_million === null || custom.price_per_million === undefined ? '' : custom.price_per_million;
  $('balance-manual-remaining').value = custom.manual_remaining === null || custom.manual_remaining === undefined ? '' : custom.manual_remaining;
  $('balance-models').value = custom.models || '';
  $('balance-timezone').value = custom.timezone || 'Asia/Shanghai';
  toggleBalanceCustom();
  dialog.showModal();
}

const balanceMode = $('balance-mode');
if (balanceMode) balanceMode.onchange = toggleBalanceCustom;
const balanceRefreshAll = $('balances-refresh-all');
if (balanceRefreshAll) balanceRefreshAll.onclick = action(() => refreshBalances());
const balanceForm = $('balance-form');
if (balanceForm) balanceForm.onsubmit = action(async () => {
  const message = balanceForm.querySelector('.form-message');
  const payload = {
    provider: $('balance-provider').value,
    mode: $('balance-mode').value,
    url: $('balance-url').value.trim(),
    used_path: $('balance-used-path').value.trim(),
    total_path: $('balance-total-path').value.trim(),
    remaining_path: $('balance-remaining-path').value.trim(),
    unit: $('balance-unit').value,
    label: $('balance-label').value.trim(),
    limit_value: $('balance-limit').value.trim() === '' ? null : Number($('balance-limit').value),
    reset: $('balance-reset').value,
    price_per_million: $('balance-price').value.trim() === '' ? null : Number($('balance-price').value),
    manual_remaining: $('balance-manual-remaining').value.trim() === '' ? null : Number($('balance-manual-remaining').value),
    models: $('balance-models').value.trim(),
    timezone: $('balance-timezone').value.trim() || 'Asia/Shanghai'
  };
  if (payload.mode === 'custom' && !payload.url) {
    if (message) message.textContent = '自定义方式需要填写 URL';
    throw new Error('自定义方式需要填写 URL');
  }
  if (payload.mode === 'manual' && !(payload.limit_value > 0)) {
    if (message) message.textContent = '本地统计需要填写大于 0 的限额';
    throw new Error('本地统计需要填写大于 0 的限额');
  }
  try {
    await api('balances', {method: 'POST', body: JSON.stringify(payload)});
    $('balance-dialog').close();
    await refreshBalances({provider: payload.provider});
  } catch (error) {
    if (message) message.textContent = error.message;
    throw error;
  }
});

function showPage(page) {
  currentPage = pageNames[page] ? page : 'dashboard';
  document.querySelectorAll('[data-view]').forEach(el => { el.hidden = el.dataset.view !== currentPage; });
  document.querySelectorAll('[data-page]').forEach(el => { el.setAttribute('aria-current', el.dataset.page === currentPage ? 'page' : 'false'); });
  $('page-title').textContent = pageNames[currentPage];
  if (currentPage === 'usage' && !balanceLoaded) loadBalances().catch(() => {});
}
document.querySelectorAll('[data-page]').forEach(el => { el.onclick = () => showPage(el.dataset.page); });

async function pollNative() {
  clearTimeout(pollTimer);
  if (!token) return;
  try {
    await managementRefresh();
    $('runtime-status').textContent = 'uni-api 原生管理接口已就绪';
    $('setup-status').textContent = '已配置 · 原生服务已就绪，可管理上游与授权池。';
  } catch {
    if (!token) return;
    $('runtime-status').textContent = '已配置，原生服务启动中或暂不可用。';
    $('setup-status').textContent = '等待原生服务就绪，将自动重试；持续不可用时请检查容器日志。';
  }
  if (token) pollTimer = setTimeout(pollNative, 5000);
}

try { token = localStorage.getItem(storageKey) || ''; } catch {}
if (token) action(start)();

function getDragAfterElement(container, y) {
  const draggables = [...container.querySelectorAll('.tree-model:not(.dragging)')];
  return draggables.reduce((closest, child) => {
    const box = child.getBoundingClientRect();
    const offset = y - box.top - box.height / 2;
    if (offset < 0 && offset > closest.offset) return { offset, element: child };
    return closest;
  }, { offset: Number.NEGATIVE_INFINITY }).element;
}

function modelPicker(prefix, existing) {
  let models = [], checked = new Set(), generation = 0;
  const input = suffix => $(prefix + '-' + suffix);
  const clear = () => {
    generation++; models = []; checked.clear();
    input('model-options').replaceChildren();
    input('discovery-status').textContent = '';
  };
  const render = () => {
    const search = input('model-search').value.trim().toLowerCase();
    const visible = models.filter(model => !search || model.toLowerCase().includes(search));
    input('model-options').replaceChildren();
    for (const model of visible) {
      const label = document.createElement('label');
      const box = document.createElement('input');
      box.type = 'checkbox';
      box.value = model;
      box.checked = checked.has(model);
      box.onchange = () => { box.checked ? checked.add(model) : checked.delete(model); };
      label.append(box, document.createTextNode(model));
      input('model-options').append(label);
    }
  };
  input('discover').onclick = async () => {
    // Editing an existing channel: reuse the credential the server already
    // stores — the plaintext is never rendered back to the browser. A key typed
    // into the field still takes precedence (e.g. to validate a replacement).
    const runWith = existing && !input('key').value ? existing() : '';
    const version = ++generation;
    if (!runWith && !input('key').value) { input('discovery-status').textContent = '请输入此目标上游的 Key 以获取模型列表；也可继续手动输入。'; return; }
    input('discover').disabled = true;
    input('discovery-status').textContent = runWith ? '正在用已保存的 Key 请求模型目录…' : '正在请求模型目录…';
    const modelsUrl = input('models-url').value;
    const payload = runWith
      ? {action: 'provider_discover', provider: runWith, models_url: modelsUrl}
      : {base_url: input('url').value, models_url: modelsUrl, api: input('key').value};
    try {
      const result = runWith
        ? await api('manage', {method: 'POST', body: JSON.stringify(payload)})
        : await api('models/discover', {method: 'POST', body: JSON.stringify(payload)});
      if (version !== generation) return;
      models = result.models;
      render();
      input('discovery-status').textContent = runWith
        ? `已用保存的 Key 获取 ${models.length} 个模型；请自行勾选，目录不代表价格或调用权限。`
        : `获取 ${models.length} 个模型；请自行勾选，目录不代表价格或调用权限。`;
    } catch (error) {
      if (version === generation) {
        input('model-options').replaceChildren();
        input('discovery-status').textContent = error.message;
      }
    } finally { delete payload.api; delete payload.action; input('discover').disabled = false; }
  };
  input('model-search').oninput = render;
  input('model-add').onclick = () => {
    const text = input('models').value;
    const existing = new Set(text.split('\n').map(line => line.split('=')[0].trim()));
    const additions = [...checked].filter(model => !existing.has(model));
    if (additions.length) input('models').value = text + (text && !text.endsWith('\n') ? '\n' : '') + additions.join('\n');
  };
  for (const suffix of ['url', 'models-url', 'key']) input(suffix).oninput = clear;
  return clear;
}
const clearModelPickers = [modelPicker('setup'), modelPicker('provider', () => $('provider-name').readOnly ? $('provider-name').value : '')];

function openProvider(provider) {
  clearModelPickers[1]();
  $('provider-form').reset();
  $('provider-name').readOnly = !!provider;
  $('provider-form').hidden = !setupConfigured;
  $('setup-form').hidden = setupConfigured;
  if (provider) {
    $('provider-name').value = provider.provider;
    $('provider-url').value = provider.base_url;
    $('provider-models').value = provider.models.map(m => typeof m === 'string' ? m : Object.entries(m).map(([k, v]) => k + '=' + v).join('\n')).join('\n');
  }
  $('provider-dialog').showModal();
  if (!setupConfigured) $('provider-list').textContent = '尚未接入渠道。';
}
$('new-provider').onclick = () => openProvider();
document.querySelectorAll('[data-close]').forEach(button => button.onclick = () => $(button.dataset.close).close());
$('provider-dialog').onclose = () => {
  $('provider-key').value = '';
  $('setup-key').value = '';
  clearModelPickers.forEach(clear => clear());
};

async function providerChange(provider, operation) {
  const preview = await api('manage', {method: 'POST', body: JSON.stringify({action: operation, provider})});
  if (!confirm('将移除模型：' + (preview.removed.join(', ') || '无') + '\n受影响模型池：' + (preview.pools.join(', ') || '无') + '\n受影响客户端：' + (preview.clients.join(', ') || '无') + '\n将撤销的 key（移除最后一条授权）：' + ((preview.revoked_clients || []).join(', ') || '无') + '\n确认同步原生配置？')) return;
  await api('manage', {method: 'POST', body: JSON.stringify({action:'confirm', preview:preview.preview})});
  await managementRefresh();
}
function renderManagement(data) {
  managementData = data;
  const checked = new Set([...$('pool-choices').querySelectorAll('input:checked')].map(box => box.value));
  $('pool-choices').replaceChildren();
  const expanded = new Set([...$('provider-list').querySelectorAll('details[open]')].map(d => d.dataset.provider));

  // Build model→provider map for pool display
  const modelProviderMap = {};
  for (const p of data.providers) {
    for (const m of p.models) {
      const alias = typeof m === 'string' ? m : Object.values(m)[0];
      modelProviderMap[p.provider + '/' + alias] = p.provider;
    }
  }

  /* ---- 渠道卡片 ---- */
  $('provider-list').replaceChildren();
  if (!data.providers.length) {
    const empty = document.createElement('div');
    empty.className = 'empty';
    empty.innerHTML = '<strong>暂无接入渠道</strong>点击「新增渠道」添加第一个上游。';
    $('provider-list').append(empty);
  }
  for (const provider of data.providers) {
    const card = document.createElement('div');
    card.className = 'provider-card';
    const head = document.createElement('div');
    head.className = 'card-head';
    const info = document.createElement('div');
    const name = document.createElement('h3');
    name.textContent = provider.provider;
    const url = document.createElement('p');
    url.className = 'url';
    url.textContent = provider.base_url;
    info.append(name, url);
    const status = document.createElement('span');
    status.className = 'badge ok';
    status.textContent = '已配置';
    head.append(info, status);
    card.append(head);

    const tags = document.createElement('div');
    tags.className = 'model-tags';
    for (const m of provider.models) {
      const alias = typeof m === 'string' ? m : Object.entries(m).map(([k, v]) => k + '=' + v).join('=');
      const tag = document.createElement('span');
      tag.className = 'tag';
      tag.textContent = alias;
      tags.append(tag);
    }
    const disclosure = document.createElement('details');
    disclosure.className = 'model-disclosure'; disclosure.dataset.provider = provider.provider;
    disclosure.open = expanded.has(provider.provider);
    const summary = document.createElement('summary'); summary.textContent = provider.models.length + ' 个已配置模型';
    disclosure.append(summary);
    const list = document.createElement('ul'); list.className = 'channel-model-list';
    for (const m of provider.models) {
      const li = document.createElement('li');
      const source = typeof m === 'string' ? m : Object.keys(m)[0];
      const alias = typeof m === 'string' ? '' : Object.values(m)[0];
      li.append(makeTag(provider.provider, 'p-tag'), makeTag(source, 'm-tag'));
      if (alias) li.append(document.createTextNode(' → '), makeTag(alias, 'm-tag'));
      list.append(li);
    }
    disclosure.append(list); card.append(disclosure);

    const actions = document.createElement('div');
    actions.className = 'card-actions';
    for (const [label, fn] of [['编辑', () => openProvider(provider)], ['刷新 /models', () => providerChange(provider.provider, 'provider_refresh')], ['删除', () => providerChange(provider.provider, 'provider_delete')]]) {
      const button = document.createElement('button');
      button.textContent = label;
      button.className = 'btn ghost';
      button.onclick = action(fn);
      actions.append(button);
    }
    card.append(actions);
    $('provider-list').append(card);

    for (const m of provider.models) {
      const alias = typeof m === 'string' ? m : Object.values(m)[0];
      const label = document.createElement('label');
      const box = document.createElement('input');
      box.type = 'checkbox';
      box.value = provider.provider + '/' + alias;
      box.checked = checked.has(box.value);
      // Public model name (= the alias) is what the user recognizes; only fall back to
      // the provider-qualified form when the alias is not already upstream-namespaced.
      const shown = alias.startsWith(provider.provider + '/') ? alias : provider.provider + '/' + alias;
      label.append(box, document.createTextNode(shown));
      $('pool-choices').append(label);
    }
  }

  /* ---- 密钥卡片 ---- */
  $('key-list').replaceChildren();
  const consoleClients = data.clients || [];
  const nativeClients = (data.external_clients || []).filter(Boolean);
  const hasKeys = consoleClients.length > 0 || nativeClients.length > 0;
  if (!hasKeys) {
    const empty = document.createElement('div');
    empty.className = 'empty';
    empty.innerHTML = '<strong>暂无客户端密钥</strong>在下方签发第一个 API 密钥。';
    $('key-list').append(empty);
  }

  async function copyKey(fetchKey, button) {
    const text = await fetchKey();
    if (!text) return;
    let done = false;
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
        done = true;
      }
    } catch { done = false; }
    if (!done) {
      const area = document.createElement('textarea');
      area.value = text; area.style.position = 'fixed'; area.style.opacity = '0';
      document.body.append(area); area.select();
      const ok = document.execCommand('copy');
      area.remove();
      if (!ok) throw new Error('复制失败，请手动复制密钥');
    }
    const label = button.getAttribute('aria-label');
    button.classList.add('copied');
    button.setAttribute('aria-label', '已复制');
    setTimeout(() => { button.classList.remove('copied'); button.setAttribute('aria-label', label); }, 1400);
  }

  function buildKeyCard({title, badge, subline, masked, disabledCopy, fetchKey, actions}) {
    const card = document.createElement('div');
    card.className = 'provider-card key-card';

    const top = document.createElement('div');
    top.className = 'key-top';
    const main = document.createElement('div');
    main.className = 'key-main';
    const titleRow = document.createElement('div');
    titleRow.className = 'key-title';
    const heading = document.createElement('h3');
    heading.textContent = title;
    titleRow.append(heading);
    if (badge) titleRow.append(makeTag(badge, 'neutral'));
    main.append(titleRow);
    if (subline) main.append(subline);

    const actionBox = document.createElement('div');
    actionBox.className = 'card-actions';
    for (const [label, handler, cls] of actions) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'btn ' + (cls || 'ghost');
      button.textContent = label;
      button.onclick = handler;
      actionBox.append(button);
    }
    top.append(main, actionBox);

    const line = document.createElement('div');
    line.className = 'key-line';
    const code = document.createElement('code');
    code.className = 'masked';
    code.textContent = masked || '未记录';
    const copyBtn = document.createElement('button');
    copyBtn.type = 'button';
    copyBtn.className = 'btn icon copy-key';
    copyBtn.setAttribute('aria-label', disabledCopy ? '密钥不可复制' : '复制完整密钥');
    copyBtn.title = copyBtn.getAttribute('aria-label');
    copyBtn.disabled = !!disabledCopy;
    copyBtn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>';
    if (!disabledCopy) copyBtn.onclick = action(() => copyKey(fetchKey, copyBtn));
    line.append(code, copyBtn);

    card.append(top, line);
    return card;
  }

  for (const k of consoleClients) {
    const clientName = k.name;
    const subline = document.createElement('p');
    subline.className = 'muted-line';
    const poolModels = (data.pools || {})[k.pool];
    subline.append(document.createTextNode('模型池 '), makeTag(k.pool, 'p-tag'),
      document.createTextNode(poolModels ? ' · ' + poolModels.length + ' 个模型，自动同步' : ' · 池不存在'));
    $('key-list').append(buildKeyCard({
      title: clientName, subline, masked: k.masked,
      fetchKey: async () => (await api('manage', {method: 'POST', body: JSON.stringify({action: 'client_key', name: clientName})})).key,
      actions: [
        ['编辑模型池', () => openClient(k)],
        ['撤销', action(async () => {
          if (!confirm('撤销密钥「' + clientName + '」？使用此密钥的客户端将无法继续调用。')) return;
          await api('manage', {method: 'POST', body: JSON.stringify({action: 'revoke', name: clientName})});
          await managementRefresh();
        }), 'danger-soft'],
      ],
    }));
  }
  for (const k of nativeClients) {
    const subline = document.createElement('p');
    subline.className = 'muted-line';
    if (k.bound_pool) subline.append(document.createTextNode('已绑定模型池 '), makeTag(k.bound_pool, 'p-tag'));
    else subline.append(document.createTextNode('未绑定模型池 · 角色 ' + (k.role || '空')));
    const actions = [];
    if (Object.keys(data.pools).length) {
      actions.push(['绑定模型池', () => openClient({isNative: true, role: k.role, pool: k.bound_pool || Object.keys(data.pools)[0]})]);
    }
    if (!k.admin) {
      actions.push(['删除', action(async () => {
        if (!confirm('删除该原生密钥？使用它的客户端会立即失去访问，且无法恢复。')) return;
        await api('manage', {method: 'POST', body: JSON.stringify({action: 'native_delete', role: k.role})});
        await managementRefresh();
      }), 'danger-soft']);
    }
    $('key-list').append(buildKeyCard({
      title: k.label === '未命名' ? '原生密钥（未命名）' : '原生密钥', badge: '原生', subline, masked: k.masked,
      disabledCopy: !k.copyable,
      fetchKey: async () => (await api('manage', {method: 'POST', body: JSON.stringify({action: 'native_key', role: k.role})})).key,
      actions,
    }));
  }

  /* ---- 授权池卡片 ---- */
  $('pool-list').replaceChildren();
  const poolClientCount = {};
  for (const k of data.clients) {
    poolClientCount[k.pool] = (poolClientCount[k.pool] || 0) + 1;
  }
  if (!Object.keys(data.pools).length) {
    const empty = document.createElement('div');
    empty.className = 'empty';
    empty.innerHTML = '<strong>暂无授权池</strong>创建一个池并从已接入渠道中选择模型。';
    $('pool-list').append(empty);
  }
  for (const [name, models] of Object.entries(data.pools)) {
    const card = document.createElement('div');
    card.className = 'provider-card';
    const head = document.createElement('div');
    head.className = 'card-head';
    const info = document.createElement('div');
    const pname = document.createElement('h3');
    pname.textContent = name;
    const meta = document.createElement('p');
    meta.className = 'muted-line';
    meta.textContent = models.length + ' 个模型 · ' + (poolClientCount[name] || 0) + ' 个客户端';
    info.append(pname, meta);
    head.append(info);
    const edit = document.createElement('button');
    edit.textContent = '编辑';
    edit.className = 'btn ghost';
    edit.onclick = () => openPool(name, models);
    head.append(edit);
    card.append(head);

    // Two-level list: upstream -> models (draggable)
    const upstreams = {};
    for (const m of models) {
      const provider = modelProviderMap[m] || m.split('/')[0];
      if (!upstreams[provider]) upstreams[provider] = [];
      upstreams[provider].push(m);
    }
    const list = document.createElement('ul');
    list.className = 'pool-model-tree';
    list.dataset.pool = name;
    list.dataset.order = JSON.stringify(models);
    for (const [upstream, upstreamModels] of Object.entries(upstreams)) {
      const li = document.createElement('li');
      li.className = 'tree-node';
      const upstreamLabel = document.createElement('span');
      upstreamLabel.className = 'tree-upstream';
      upstreamLabel.textContent = upstream;
      li.append(upstreamLabel);
      const modelList = document.createElement('ul');
      modelList.className = 'tree-models';
      for (const m of upstreamModels) {
        const mli = document.createElement('li');
        mli.className = 'tree-model';
        mli.draggable = true;
        mli.dataset.model = m;
        mli.append(makeTag(upstream, 'p-tag'), makeTag(m.slice(upstream.length + 1), 'm-tag'));
        modelList.append(mli);
      }
      li.append(modelList);
      list.append(li);
    }
    card.append(list);
    $('pool-list').append(card);
  }

  // Drag-and-drop inside a pool tree: reorders and saves the pool model order.
  const treeOrder = tree => [...tree.querySelectorAll('.tree-model')].map(el => el.dataset.model);
  async function saveTreeOrder(tree) {
    const models = treeOrder(tree);
    if (!tree.dataset.pool || !models.length || JSON.stringify(models) === tree.dataset.order) return;
    await api('manage', {method: 'POST', body: JSON.stringify({action: 'pool', name: tree.dataset.pool, models})});
    tree.dataset.order = JSON.stringify(models);
  }
  document.querySelectorAll('.pool-model-tree[data-pool]').forEach(tree => {
    let dragged = null;
    tree.querySelectorAll('.tree-model').forEach(el => {
      el.addEventListener('dragstart', e => {
        dragged = el;
        el.classList.add('dragging');
        if (e.dataTransfer) { e.dataTransfer.effectAllowed = 'move'; try { e.dataTransfer.setData('text/plain', el.dataset.model); } catch {} }
      });
      el.addEventListener('dragend', () => {
        el.classList.remove('dragging');
        if (!dragged) return;
        dragged = null;
        action(async () => { await saveTreeOrder(tree); await managementRefresh(); })();
      });
      el.addEventListener('dragover', e => {
        if (!dragged) return;
        e.preventDefault();
        const after = getDragAfterElement(tree, e.clientY);
        if (after) tree.insertBefore(dragged, after); else tree.append(dragged);
      });
      el.addEventListener('drop', e => { e.preventDefault(); });
    });
  });

  filterPoolChoices();
  $('management-summary').textContent = `${data.providers.length} 个渠道 · ${Object.keys(data.pools).length} 个授权池 · ${data.clients.length} 个客户端`;
  const selectedPool = $('client-pool').value;
  $('client-pool').replaceChildren();
  for (const pool of Object.keys(data.pools)) {
    const option = document.createElement('option');
    option.value = pool; option.textContent = pool;
    $('client-pool').append(option);
    if (pool === selectedPool) option.selected = true;
  }
}
