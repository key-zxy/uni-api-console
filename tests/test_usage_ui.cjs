const assert = require('node:assert/strict');
const fs = require('node:fs'), vm = require('node:vm');

/* 余量页契约：每个上游一个进度条；方式可改；错误与「不查询」各有状态。 */
function makeNode() {
  const node = {
    children: [], dataset: {}, style: {}, value: '', textContent: '', hidden: false,
    className: '', tagName: 'DIV', disabled: false, opened: false, closed: false,
    classList: { items: [], add(name) { if (!this.items.includes(name)) this.items.push(name); }, remove() {} },
    append(...children) { node.children.push(...children); },
    replaceChildren(...children) { node.children = children; },
    setAttribute() {}, querySelector() { return null; }, showModal() { node.opened = true; },
    close() { node.closed = true; }
  };
  return node;
}

const elements = new Map();
const el = id => { if (!elements.has(id)) elements.set(id, makeNode()); return elements.get(id); };
let calls = [], responses = {};

const context = {
  document: {
    getElementById: el, querySelectorAll: () => [], createElement: () => makeNode(),
    createTextNode: text => ({ textContent: text }), body: { classList: { add() {}, remove() {} } }
  },
  localStorage: { getItem: () => null, removeItem() {} },
  clearTimeout() {}, setTimeout() {},
  fetch: async (url, opts) => {
    const body = opts && opts.body ? JSON.parse(opts.body) : null;
    calls.push([url, body]);
    const payload = responses[url.replace('/_audit/api/', '')] || { providers: [] };
    return { ok: true, status: 200, json: async () => payload, text: async () => '' };
  }
};
vm.runInNewContext(fs.readFileSync('app.js', 'utf8'), context);

(async () => {
  responses['balances/refresh'] = { providers: [
    { provider: 'openrouter', base_url: 'https://openrouter.ai/api/v1', mode: 'openrouter', age: 12,
      reading: { ok: true, used: 3.82, total: 10, remaining: 6.18, percent: 61.8, unit: 'usd', label: '账户余额',
                 detail: { '免费模型今日': '113/1000（剩 887）' }, error: '' } },
    { provider: 'amd', base_url: 'https://developer.amd.com.cn/radeon/api/v1', mode: 'amd', age: 40,
      reading: { ok: false, percent: null, detail: {}, error: '查询失败：HTTP 404' } },
    { provider: 'minekuai', base_url: 'https://minekuai.cn/v1', mode: 'none', age: 0, reading: null }
  ] };

  await el('balances-refresh-all').onclick();
  assert.ok(calls.at(-1)[0].endsWith('/_audit/api/balances/refresh'), 'refresh-all hits the refresh endpoint');
  assert.deepEqual(calls.at(-1)[1], { provider: '', force: true });

  const cards = el('balances-list').children;
  assert.equal(cards.length, 3, 'one card per provider');

  const [orCard, amdCard, mkCard] = cards;
  const bar = orCard.children[1];
  assert.equal(bar.className.includes('balance-bar'), true);
  assert.equal(bar.children[0].dataset.percent, '61.8', 'progress bar carries the remaining percentage');
  assert.equal(bar.children[0].style.width, '61.8%');
  const meta = orCard.children[2];
  assert.ok(meta.children[0].textContent.includes('$6.18'), 'remaining amount rendered');
  assert.ok(meta.children.some(child => child.textContent.includes('剩余 61.8%')));
  const detail = orCard.children[3];
  assert.ok(detail.children[0].textContent.includes('887'), 'detail line keeps the free-model quota');

  const errorMeta = amdCard.children[2];
  assert.equal(errorMeta.dataset.error, '1', 'failed reading marks the meta line as an error');
  assert.ok(errorMeta.textContent.includes('HTTP 404'));
  assert.ok(amdCard.classList.items.includes('error'));

  const noneHint = mkCard.children[1];
  assert.ok(noneHint.textContent.includes('未配置余量方式'), 'none mode explains what to do');

  // 单个渠道刷新
  const actions = orCard.children.at(-1);
  await actions.children[0].onclick();
  assert.deepEqual(calls.at(-1)[1], { provider: 'openrouter', force: true });

  // 设置对话框带入当前配置，自定义字段随方式显示
  responses['balances/refresh'] = responses['balances/refresh'];
  actions.children[1].onclick();
  assert.equal(el('balance-dialog').opened, true);
  assert.equal(el('balance-provider').value, 'openrouter');
  assert.equal(el('balance-mode').value, 'openrouter');
  assert.equal(el('balance-custom-fields').hidden, true);
  el('balance-mode').value = 'custom';
  el('balance-mode').onchange();
  assert.equal(el('balance-custom-fields').hidden, false, 'custom fields appear for custom mode');

  el('balance-url').value = '/v1/custom/quota';
  el('balance-used-path').value = 'data[0].used';
  el('balance-total-path').value = 'data[0].total';
  el('balance-remaining-path').value = '';
  el('balance-unit').value = 'usd';
  el('balance-label').value = '本月额度';
  await el('balance-form').onsubmit();
  const saveCall = calls.find(([url]) => url.endsWith('/_audit/api/balances'));
  assert.ok(saveCall, 'save posts the settings');
  assert.equal(saveCall[1].mode, 'custom');
  assert.equal(saveCall[1].url, '/v1/custom/quota');
  assert.equal(saveCall[1].used_path, 'data[0].used');
  assert.equal(saveCall[1].unit, 'usd');
  // 本地统计：限额/周期/单价/锚点/模型过滤都随表单提交
  el('balance-mode').value = 'manual';
  el('balance-mode').onchange();
  assert.equal(el('balance-manual-fields').hidden, false, 'manual fields appear for local-ledger mode');
  el('balance-limit').value = '80';
  el('balance-reset').value = 'day';
  el('balance-price').value = '';
  el('balance-manual-remaining').value = '55.92';
  el('balance-models').value = 'deepseek-v4.1-flash';
  el('balance-timezone').value = 'Asia/Shanghai';
  await el('balance-form').onsubmit();
  const manualCall = calls.filter(([url]) => url.endsWith('/_audit/api/balances')).at(-1);
  assert.deepEqual(manualCall[1], { provider: 'openrouter', mode: 'manual', url: '/v1/custom/quota',
    used_path: 'data[0].used', total_path: 'data[0].total', remaining_path: '', unit: 'usd', label: '本月额度',
    limit_value: 80, reset: 'day', price_per_million: null, manual_remaining: 55.92,
    models: 'deepseek-v4.1-flash', timezone: 'Asia/Shanghai' });
  assert.equal(el('balance-dialog').closed, true);
  assert.deepEqual(calls.at(-1)[1], { provider: 'openrouter', force: true }, 'saving refreshes that provider');

  // 自定义方式缺 URL 时不提交
  calls = [];
  el('balance-mode').value = 'custom';
  el('balance-url').value = '';
  await el('balance-form').onsubmit();
  assert.equal(calls.some(([url]) => url.endsWith('/_audit/api/balances')), false, 'missing URL blocks the save');

  console.log('Balances page: progress bars, mode switching and settings save pass');
})().catch(error => { console.error(error); process.exit(1); });