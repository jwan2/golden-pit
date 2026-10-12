// 黄金坑筛选器前端。所有数据只通过 textContent / DOM API 写入页面，没有 innerHTML。

const $ = (id) => document.getElementById(id);

/** 极简 DOM 构造器：h('div', {class:'x', onclick}, 'text', child…) */
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (k === 'class') el.className = v;
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

/** localStorage 在隐私模式下可能抛错，所以全部包起来 */
const store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v == null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* 忽略 */ } },
};

/** replaceChildren 会把 null 渲染成文字 "null"，所以统一过滤空值 */
function fill(el, ...kids) {
  el.replaceChildren(...kids.flat().filter((k) => k != null && k !== false));
  return el;
}

const state = {
  config: null, scan: null, viewingHistory: null,
  tier: null, query: '', hideNoise: true,
  sort: { key: 'verdict', dir: 'asc' }, polling: null,
  ai: {}, onlyWatch: false,
  watch: new Set(store.get('gp_watch', [])),
  plan: store.get('gp_plan', { capital: '', cap: 5 }),
};

const ICON = { pass: '✓', fail: '✗', unknown: '?', na: '–' };
const STATUS_TEXT = { pass: '通过', fail: '未通过', unknown: '未知', na: '不适用' };

// ---------------------------------------------------------------- 工具
async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  let body = null;
  try { body = await res.json(); } catch { /* 非 JSON（如导出） */ }
  if (!res.ok) throw new Error((body && body.error) || `请求失败（${res.status}）`);
  return body;
}

const fmt = {
  price: (v) => (v == null ? '—' : v.toFixed(2)),
  pct: (v) => (v == null ? '—' : v.toFixed(1) + '%'),
  x: (v) => (v == null ? '—' : v.toFixed(2) + 'x'),
  cap: (v) => {
    if (!v) return '—';
    for (const [u, d] of [['T', 1e12], ['B', 1e9], ['M', 1e6]]) if (v >= d) return `$${(v / d).toFixed(1)}${u}`;
    return '$' + Math.round(v);
  },
  ago(iso) {
    const s = (Date.now() - new Date(iso).getTime()) / 1000;
    if (s < 90) return '刚刚';
    if (s < 5400) return `${Math.round(s / 60)} 分钟前`;
    if (s < 129600) return `${Math.round(s / 3600)} 小时前`;
    return `${Math.round(s / 86400)} 天前`;
  },
  when: (iso) => new Date(iso).toLocaleString('zh-CN', { hour12: false }),
};

const check = (r, key) => r.checks.find((c) => c.key === key);
const tierLabel = (t) => (state.config?.tiers[t]?.label ?? t);

// ---------------------------------------------------------------- 渲染：顶部
function renderFreshness() {
  const el = $('freshness');
  const s = state.scan;
  el.className = 'muted';
  if (!s) { el.textContent = '还没有扫描结果，点击“运行扫描”'; return; }
  const ageH = (Date.now() - new Date(s.finished_at).getTime()) / 3.6e6;
  el.textContent = `扫描于 ${fmt.ago(s.finished_at)}（${fmt.when(s.finished_at)}）· ${s.universe.note}`;
  if (ageH > 36 && !state.viewingHistory) {
    el.classList.add('stale');
    el.textContent += ' · 数据已超过 36 小时，请重新扫描';
  }
}

function renderBanner() {
  const b = $('banner');
  b.replaceChildren();
  if (state.viewingHistory) {
    b.hidden = false; b.className = 'banner';
    b.append(`正在查看历史扫描 ${fmt.when(state.scan.finished_at)}`,
      h('button', { onclick: () => loadLatest() }, '返回最新'));
  } else b.hidden = true;
}

function renderSummary() {
  const box = $('summary');
  box.replaceChildren();
  if (!state.scan) return;
  const { summary } = state.scan;
  for (const [tier, meta] of Object.entries(state.config.tiers)) {
    const n = summary[tier] || 0;
    if (!n && (tier === 'error' || tier === 'n_a')) continue;
    box.append(h('button', {
      class: `tile t-${tier}`, 'aria-pressed': state.tier === tier, title: meta.desc,
      onclick: () => { state.tier = state.tier === tier ? null : tier; render(); },
    }, h('div', { class: 'n num' }, n), h('div', { class: 'l' }, meta.label)));
  }
}

function renderChanges() {
  const box = $('changes');
  const c = state.scan?.changes;
  box.replaceChildren();
  if (!c || !c.has_previous) { box.hidden = true; return; }
  const parts = [];
  if (c.entered_golden_pit.length) parts.push(h('span', null, h('b', null, '新晋黄金坑 '), c.entered_golden_pit.join('、')));
  if (c.left_golden_pit.length) parts.push(h('span', null, h('b', null, '掉出黄金坑 '),
    c.left_golden_pit.map((x) => `${x.ticker}→${tierLabel(x.now)}`).join('、')));
  box.hidden = !parts.length;
  box.append(`较上次（${fmt.ago(c.previous_finished_at)}）：`, ...parts.flatMap((p, i) => (i ? ['　', p] : [p])));
}

// ---------------------------------------------------------------- 渲染：表格
const COLUMNS = [
  { key: 'watch', label: '★' },
  { key: 'verdict', label: '结论' },
  { key: 'ticker', label: '公司' },
  { key: 'price', label: '现价', cls: 'r' },
  { key: 'drop', label: '回撤', cls: 'r' },
  { key: 'pb', label: 'P/B', cls: 'r hide-sm' },
  { key: 'fpe', label: '前瞻 PE', cls: 'r hide-sm' },
  { key: 'fin', label: '财务', cls: 'hide-sm' },
  { key: 'mgmt', label: '管理层', cls: 'hide-sm' },
  { key: 'timing', label: '走势', cls: '' },
  { key: 'unknown', label: '数据', cls: '' },
];

const SORTERS = {
  watch: (r) => (state.watch.has(r.ticker) ? 0 : 1),
  verdict: (r) => r.verdict.sort_key,
  ticker: (r) => r.ticker,
  price: (r) => r.price,
  drop: (r) => r.drop_from_high_pct,
  pb: (r) => check(r, 'pb')?.value,
  fpe: (r) => check(r, 'forward_pe')?.value,
  fin: (r) => r.verdict.fin_pass,
  mgmt: (r) => r.verdict.mgmt_pass,
  unknown: (r) => r.verdict.unknown_count,
  timing: (r) => ['rebounding', 'basing', 'unclear', 'falling', 'unknown'].indexOf(r.timing?.state ?? 'unknown'),
};

function cmp(a, b) {
  if (Array.isArray(a)) { for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return a[i] < b[i] ? -1 : 1; return 0; }
  return a < b ? -1 : a > b ? 1 : 0;
}

function visibleRows() {
  const q = state.query.trim().toLowerCase();
  let rows = state.scan.results.filter((r) => {
    if (state.onlyWatch && !state.watch.has(r.ticker)) return false;
    if (state.tier && r.verdict.tier !== state.tier) return false;
    if (!state.tier && !state.onlyWatch && state.hideNoise && ['n_a', 'error'].includes(r.verdict.tier)) return false;
    if (q && !`${r.ticker} ${r.name} ${r.industry} ${r.sector}`.toLowerCase().includes(q)) return false;
    return true;
  });
  const { key, dir } = state.sort;
  const get = SORTERS[key];
  rows = rows.slice().sort((a, b) => {
    const [va, vb] = [get(a), get(b)];
    if (va == null && vb == null) return 0;
    if (va == null) return 1;              // 缺失值永远排最后
    if (vb == null) return -1;
    return cmp(va, vb) * (dir === 'asc' ? 1 : -1);
  });
  return rows;
}

function pips(checks) {
  return h('span', { class: 'pips', 'aria-hidden': 'true' }, checks.map((c) => h('i', { class: `pip ${c.status}` })));
}

function renderHead() {
  const tr = h('tr');
  for (const col of COLUMNS) {
    const active = state.sort.key === col.key;
    tr.append(h('th', { class: col.cls, scope: 'col', 'aria-sort': active ? (state.sort.dir === 'asc' ? 'ascending' : 'descending') : 'none' },
      h('button', {
        onclick: () => {
          state.sort = { key: col.key, dir: active && state.sort.dir === 'asc' ? 'desc' : 'asc' };
          renderTable();
        },
      }, col.label)));
  }
  $('table').tHead.replaceChildren(tr);
}

function renderTable() {
  renderHead();
  const body = $('table').tBodies[0];
  body.replaceChildren();
  const empty = $('empty');
  if (!state.scan) {
    empty.hidden = false; empty.textContent = '还没有扫描结果。点击右上角“运行扫描”开始。';
    $('count').textContent = ''; return;
  }
  const rows = visibleRows();
  empty.hidden = rows.length > 0;
  empty.textContent = '没有符合条件的标的。';
  $('count').textContent = `${rows.length} / ${state.scan.results.length} 只`;
  for (const r of rows) {
    const v = r.verdict;
    const open = () => openDrawer(r.ticker);
    const fin = r.checks.filter((c) => c.group === 'financial');
    const mgmt = r.checks.filter((c) => c.group === 'management');
    body.append(h('tr', {
      tabindex: 0, onclick: open, onkeydown: (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); } },
    },
      h('td', { class: 'star-cell' }, starButton(r.ticker)),
      h('td', null, h('span', { class: `badge t-${v.tier}` }, v.label), aiMark(r)),
      h('td', { class: 'co' }, h('b', null, r.ticker), ' ', r.name !== r.ticker ? r.name : '',
        h('small', null, [r.industry, fmt.cap(r.market_cap)].filter((x) => x && x !== '—').join(' · '))),
      h('td', { class: 'r num' }, fmt.price(r.price)),
      h('td', { class: 'r num' }, fmt.pct(r.drop_from_high_pct)),
      h('td', { class: 'r num hide-sm' }, valueOf(r, 'pb')),
      h('td', { class: 'r num hide-sm' }, valueOf(r, 'forward_pe')),
      h('td', { class: 'hide-sm num' }, v.tier === 'error' ? '—' : [pips(fin), `${v.fin_pass}/${v.fin_total}`]),
      h('td', { class: 'hide-sm num' }, v.tier === 'error' ? '—' : [pips(mgmt), `${v.mgmt_pass}/3`]),
      h('td', null, timingBadge(r.timing)),
      h('td', null, dataCell(r)),
    ));
  }
}

function starButton(ticker) {
  const on = state.watch.has(ticker);
  return h('button', {
    class: 'star' + (on ? ' on' : ''), 'aria-pressed': on, 'aria-label': on ? `取消自选 ${ticker}` : `加入自选 ${ticker}`,
    onclick: (e) => { e.stopPropagation(); toggleWatch(ticker); },
  }, on ? '★' : '☆');
}

function toggleWatch(ticker) {
  state.watch.has(ticker) ? state.watch.delete(ticker) : state.watch.add(ticker);
  store.set('gp_watch', [...state.watch]);
  renderTable();
  if (!$('drawer').hidden && $('drawer').dataset.ticker) openDrawer($('drawer').dataset.ticker);
}

function aiMark(r) {
  const rec = state.ai[r.ticker];
  return rec ? h('span', { class: 'ai-dot', title: `已有 AI 研判${rec.stale ? '（已过期）' : ''}` }, rec.stale ? ' ◌' : ' ◉') : null;
}

function timingBadge(t) {
  if (!t || t.state === 'unknown') return h('span', { class: 'muted' }, '—');
  return h('span', { class: `tm tm-${t.state}`, title: t.reason }, t.label);
}

const SVG_NS = 'http://www.w3.org/2000/svg';
function svg(tag, attrs, ...kids) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs || {})) if (v != null) el.setAttribute(k, v);
  for (const kid of kids) if (kid != null) el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  return el;
}

/** 一年收盘价走势 + 52 周高点 + 分批价位。纯 SVG，颜色走 CSS 变量，深浅色都适配。 */
function priceChart(r) {
  const c = r.chart;
  if (!c || c.closes.length < 2) return null;
  const W = 520, H = 190, L = 6, R = 64, T = 10, B = 22;
  const levels = r.plan ? r.plan.levels : [];
  const high = r.high_52w;
  const all = [...c.closes, ...(high ? [high] : []), ...levels.map((l) => l.price)];
  let lo = Math.min(...all), hi = Math.max(...all);
  const pad = (hi - lo) * 0.06 || 1; lo -= pad; hi += pad;
  const x = (i) => L + (i / (c.closes.length - 1)) * (W - L - R);
  const y = (v) => T + (1 - (v - lo) / (hi - lo)) * (H - T - B);
  const path = c.closes.map((v, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join('');
  const hline = (v, cls, label) => [
    svg('line', { x1: L, x2: W - R, y1: y(v), y2: y(v), class: cls }),
    svg('text', { x: W - R + 4, y: y(v) + 4, class: 'ch-lbl' }, label)];
  const last = c.closes[c.closes.length - 1];
  const g = svg('svg', { viewBox: `0 0 ${W} ${H}`, class: 'chart', role: 'img',
    'aria-label': `${r.ticker} 近一年收盘价走势，现价 ${last}` },
    ...(high ? hline(high, 'ch-high', `高 ${high.toFixed(0)}`) : []),
    ...levels.flatMap((lv) => hline(lv.price, lv.reached ? 'ch-lv reached' : 'ch-lv', `-${lv.drop_pct}%`)),
    svg('path', { d: path, class: 'ch-line' }),
    svg('circle', { cx: x(c.closes.length - 1), cy: y(last), r: 3.5, class: 'ch-dot' }),
    svg('text', { x: L, y: H - 6, class: 'ch-axis' }, c.dates[0]),
    svg('text', { x: W - R, y: H - 6, class: 'ch-axis', 'text-anchor': 'end' }, c.dates[c.dates.length - 1]));
  return g;
}

function timingCard(r) {
  const t = r.timing;
  if (!t || r.error) return null;
  const m = t.metrics || {};
  return h('section', { class: 'card' },
    h('h3', null, '走势与时机', h('small', null, '参考 · 不计入判定')),
    priceChart(r),
    h('p', { class: 'tm-line' }, timingBadge(t), ' ', t.reason),
    m.ma20 ? h('div', { class: 'muted small' },
      `MA20 ${m.ma20} · MA50 ${m.ma50 ?? '—'} · 一年低点 ${m.low_1y}（${m.low_date}）· RSI14 ${m.rsi14 ?? '—'}`) : null);
}

function valueOf(r, key) {
  const c = check(r, key);
  if (!c || c.status === 'unknown') return '—';
  return c.display;
}

function dataCell(r) {
  if (r.error) return h('span', { class: 'warn' }, '抓取失败');
  const bits = [];
  if (r.verdict.unknown_count) bits.push(h('span', { class: 'warn', title: '这些指标没有可靠数据，未计入通过' }, `? ${r.verdict.unknown_count} 项未知`));
  if (r.warnings.length) bits.push(h('span', { class: 'warn', title: r.warnings.join('\n') }, '⚠'));
  return bits.length ? bits.flatMap((b, i) => (i ? [' ', b] : [b])) : h('span', { class: 'muted' }, '完整');
}

// ---------------------------------------------------------------- 详情抽屉
function checkRow(c) {
  return h('div', { class: `chk ${c.status}` },
    h('span', { class: `ic ${c.status}`, title: STATUS_TEXT[c.status], 'aria-label': STATUS_TEXT[c.status] }, ICON[c.status]),
    h('span', { class: 'lab' }, c.label),
    h('span', { class: 'val num' }, c.display),
    c.rule ? h('span', { class: 'rule' }, `要求 ${c.rule}`) : null,
    c.note ? h('span', { class: 'note' }, c.note) : null);
}

function group(title, sub, checks) {
  if (!checks.length) return null;
  return h('section', { class: 'card' }, h('h3', null, title, h('small', null, sub)), checks.map(checkRow));
}

function openDrawer(ticker) {
  const r = state.scan.results.find((x) => x.ticker === ticker);
  if (!r) return;
  const v = r.verdict, g = (name) => r.checks.filter((c) => c.group === name);
  const body = $('drawer-body');
  fill(body,
    h('div', { class: 'd-head' },
      h('div', null, h('h2', null, `${r.ticker} `, h('span', { class: `badge t-${v.tier}` }, v.label)),
        h('div', { class: 'sub' }, [r.name, r.sector, r.industry].filter(Boolean).join(' · '))),
      h('div', { class: 'd-tools' }, starButton(r.ticker), h('button', { class: 'd-close', onclick: closeDrawer, 'aria-label': '关闭' }, '✕'))),
    r.error ? null : h('div', { class: 'd-price' },
      h('span', null, '现价 ', h('b', { class: 'num' }, fmt.price(r.price)), ` ${r.currency}`),
      h('span', null, '回撤 ', h('b', { class: 'num' }, fmt.pct(r.drop_from_high_pct))),
      h('span', null, '市值 ', h('b', { class: 'num' }, fmt.cap(r.market_cap))),
      r.dividend_yield_pct != null ? h('span', null, '股息率 ', h('b', { class: 'num' }, r.dividend_yield_pct + '%')) : null),
    r.warnings.length ? h('div', { class: 'warnbox' }, r.warnings.map((w) => h('div', null, '⚠ ' + w))) : null,
    h('section', { class: 'card' }, h('h3', null, '为什么是这个结论'),
      h('ul', { class: 'reasons' }, v.reasons.map((t) => h('li', null, t)))),
    timingCard(r),
    planCard(r),
    aiCard(r),
    group('财务抗压', `通过 ${v.fin_pass}/${v.fin_total}`, g('financial')),
    group('管理层资本配置', `通过 ${v.mgmt_pass}/3`, g('management')),
    group('坑位（估值）', `估值锚点 ${v.anchors_pass}/2`, g('valuation')),
    r.moat ? h('section', { class: 'card moat' }, h('h3', null, '行业护城河观点', h('small', null, '人工知识库 · 不计入判定')),
      h('p', null, r.moat.summary),
      h('dl', null,
        h('dt', null, `终极颠覆风险：${r.moat.disruption_risk}`), h('dd', null, r.moat.disruption_view),
        h('dt', null, `产能复制壁垒：${r.moat.capacity_barrier}`), h('dd', null, r.moat.capacity_view)),
      h('div', { class: 'src' }, r.moat.source)) : null,
    h('div', { class: 'foot' },
      h('span', null, `数据抓取于 ${fmt.when(r.fetched_at)}`),
      r.statements_as_of ? h('span', null, `最新年报 ${r.statements_as_of}`) : null,
      r.fundamentals_fetched_at ? h('span', null, `财报数据抓取于 ${fmt.when(r.fundamentals_fetched_at)}`) : null,
      h('a', { href: `https://finance.yahoo.com/quote/${encodeURIComponent(r.ticker)}`, target: '_blank', rel: 'noopener noreferrer' }, '在 Yahoo 查看 ↗')),
  );
  $('drawer').dataset.ticker = r.ticker;
  $('drawer').hidden = false; $('scrim').hidden = false;
  $('drawer').scrollTop = 0;
  body.querySelector('.d-close')?.focus();
}

// ---------------------------------------------------------------- 买入计划器
function planCard(r) {
  const v = r.verdict;
  if (!['golden_pit', 'quality_wait'].includes(v.tier) || !r.plan) {
    if (v.tier === 'value_trap') return h('section', { class: 'card' }, h('h3', null, '买入计划'),
      h('p', { class: 'muted' }, '质量门槛没有过关（价值陷阱风险），不生成分批计划。请先看“为什么是这个结论”。'));
    return null;
  }
  const body = h('div');
  const draw = () => {
    const cap = parseFloat(state.plan.capital), pct = parseFloat(state.plan.cap);
    const budget = cap > 0 && pct > 0 ? cap * pct / 100 : null;
    fill(body,
      h('table', { class: 'mini plan' },
        h('thead', null, h('tr', null, ['回撤位', '价位', '占比', budget ? '金额 / 股数' : '', '状态'].map((t) => h('th', null, t)))),
        h('tbody', null, r.plan.levels.map((lv) => {
          const amt = budget ? budget * lv.weight_pct / 100 : null;
          return h('tr', null,
            h('td', { class: 'num' }, `-${lv.drop_pct}%`),
            h('td', { class: 'num' }, `$${lv.price.toFixed(2)}`),
            h('td', { class: 'num' }, `${lv.weight_pct}%`),
            h('td', { class: 'num' }, amt ? `$${Math.round(amt).toLocaleString()} / ${Math.floor(amt / lv.price)} 股` : ''),
            h('td', null, lv.reached ? h('b', { class: 'reached' }, '✓ 已到达') : `还差 ${lv.away_pct}%`));
        }))),
      budget ? h('p', { class: 'muted small' }, `单只仓位上限 = ${cap.toLocaleString()} × ${pct}% = $${Math.round(budget).toLocaleString()}（三档全部买满）`) : null);
  };
  const input = (key, label, ph) => h('label', { class: 'mini-field' }, label,
    h('input', {
      type: 'number', min: '0', step: 'any', value: state.plan[key], placeholder: ph, inputmode: 'decimal',
      oninput: (e) => { state.plan[key] = e.target.value; store.set('gp_plan', state.plan); draw(); },
    }));
  draw();
  return h('section', { class: 'card' },
    h('h3', null, '买入计划', h('small', null, `锚点：52 周高点 $${r.plan.anchor_high}`)),
    h('div', { class: 'planner-inputs' }, input('capital', '总资金 $', '例如 50000'), input('cap', '单只上限 %', '5')),
    body,
    h('div', { class: 'invalid' }, h('b', null, '停止加仓 / 重新评估的条件'),
      h('ul', null, r.plan.invalidation.map((t) => h('li', null, t)))),
    h('p', { class: 'muted small' }, '这是分批纪律的参考模板，不是买入建议。分档位置和仓位上限请按自己的风险承受力调整。'));
}

// ---------------------------------------------------------------- AI 研判
function adminHeaders() {
  const headers = { 'Content-Type': 'application/json' };
  if (state.config.auth_required) {
    let t = sessionStorage.getItem('gp_token') || '';
    if (!t) { t = window.prompt('请输入管理员令牌') || ''; if (t) sessionStorage.setItem('gp_token', t); }
    headers['X-Admin-Token'] = t;
  }
  return headers;
}

function aiCard(r) {
  if (r.error) return null;
  const cfg = state.config.ai;
  const box = h('section', { class: 'card ai' });
  const draw = () => {
    const rec = state.ai[r.ticker];
    const head = h('h3', null, 'AI 研判', h('small', null, '观点 · 不计入判定'));
    if (!cfg.enabled) {
      fill(box, head, h('p', { class: 'muted' }, '未启用。服务端设置 GOLDEN_PIT_AI_PROVIDER=cli（Command Code）或 openai 后可用。'));
      return;
    }
    const btn = h('button', {
      class: rec ? '' : 'primary',
      onclick: async (e) => {
        e.target.disabled = true; e.target.textContent = '分析中…（约 10-60 秒）';
        try {
          const res = await api('/api/ai/' + encodeURIComponent(r.ticker), { method: 'POST', headers: adminHeaders() });
          state.ai[r.ticker] = res.item; renderTable();
        } catch (ex) { box.append(h('p', { class: 'error' }, ex.message)); }
        draw();
      },
    }, rec ? '重新生成' : '生成 AI 研判');
    if (!rec) {
      fill(box, head, h('p', { class: 'muted' }, '让模型基于上面的量化数据，分析这个坑的成因、空头逻辑和需要核实的事项。'), btn);
      return;
    }
    const a = rec.analysis;
    const list = (title, items) => (items.length ? h('div', null, h('h4', null, title), h('ul', null, items.map((t) => h('li', null, t)))) : null);
    fill(box, head,
      rec.stale ? h('div', { class: 'warnbox' }, '⚠ 这份研判生成后，量化结论或数据状态已变化，请重新生成。') : null,
      h('div', { class: 'ai-tags' },
        h('span', { class: 'badge t-quality_wait' }, a.pit_type_label),
        h('span', { class: 'badge t-fail' }, a.agree_label),
        h('span', { class: 'badge t-fail' }, `置信度 ${a.confidence_label}`)),
      h('p', null, a.pit_reason),
      h('h4', null, '多头逻辑'), h('p', null, a.thesis),
      list('空头论点', a.bear_case), list('可能的催化剂', a.catalysts),
      list('你应该自己核实', a.verify), list('数据缺口', a.data_gaps),
      h('p', { class: 'muted small' }, `${rec.model} · ${fmt.when(rec.generated_at)} · 模型可能出错，数字以上方量化数据为准`),
      btn);
  };
  draw();
  return box;
}

function closeDrawer() { $('drawer').hidden = true; $('scrim').hidden = true; }

// ---------------------------------------------------------------- 对话框
function renderMethod() {
  const t = state.config.thresholds;
  const li = (...x) => h('li', null, ...x);
  fill($('method-body'),
    h('h2', null, '判定方法'),
    h('p', null, '没有“总分”。只回答两个独立的问题，两个都过关才是黄金坑：'),
    h('h3', null, '① 质量门槛：公司本身健康吗？'),
    h('ul', null,
      li(`财务 5 项至少通过 ${t.financial_min_pass} 项：利息保障 ≥ ${t.interest_coverage_min}x、流动比率 ≥ ${t.current_ratio_min}、净负债/EBITDA ≤ ${t.net_debt_to_ebitda_max}x、自由现金流 > 0、Altman Z ≥ ${t.altman_z_min}`),
      li(`管理层 3 项至少通过 ${t.management_min_pass} 项：总股本净减少 ≥ ${t.shares_reduction_min_pct}%、净回购收益率 ≥ ${t.buyback_yield_min_pct}%、资本开支/营收 ≤ ${t.capex_to_revenue_max_pct}%`)),
    h('h3', null, '② 坑位条件：价格真的在坑里吗？'),
    h('ul', null,
      li(`距 52 周高点回撤 ≥ ${t.drop_from_high_min_pct}%（必需）`),
      li(`且 P/B ∈ (0, ${t.pb_max}] 或 前瞻 PE ∈ (0, ${t.forward_pe_max}] 至少 ${t.pit_min_anchors} 项合格`)),
    h('h3', null, '数据诚信规则'),
    h('ul', null,
      li('缺失的数据是“未知”（?），不计为通过，也不计为 0。未知项超过 ' + t.max_unknown_checks + ' 个 → “数据不足”，不下结论。'),
      li('财报币种与报价币种不同（如 SONY）→ 依赖市值的指标标为未知。'),
      li('窗口内发生拆股/并股 → 股本变化标为未知（历史股本未必复权）。'),
      li('金融业公司 → “不适用”。'),
      li('行业护城河观点来自人工知识库，只展示，不计入判定。')),
    h('p', { class: 'muted' }, '阈值在 data/settings.json 里可改，改完重启服务生效。'));
}

async function openTrack() {
  const box = $('track-body');
  fill(box, h('h2', null, '战绩追踪'), h('p', { class: 'muted' }, '加载中…'));
  $('dlg-track').showModal();
  let rep;
  try { rep = await api('/api/track'); } catch (e) { box.append(h('p', { class: 'error' }, e.message)); return; }
  const s = rep.summary;
  const pct = (v) => (v == null ? '—' : `${v > 0 ? '+' : ''}${v.toFixed(1)}%`);
  const stat = (label, st) => h('div', { class: 'stat' }, h('div', { class: 'l' }, label),
    st.n ? [h('div', { class: 'n num' }, pct(st.mean)), h('div', { class: 'muted small' }, `中位 ${pct(st.median)} · 胜率 ${st.win_rate}% · n=${st.n}`)]
      : h('div', { class: 'muted' }, '暂无数据'));
  fill(box,
    h('h2', null, '战绩追踪'),
    h('p', { class: 'muted small' }, '每只股票第一次被判为“黄金坑”时记下价格，之后每次扫描更新，与同期 ', rep.benchmark, ' 对比。这是前向记录，不是回测。'),
    s.total ? h('div', { class: 'stats' },
      stat('入选至今 · 平均收益', s.return_since),
      stat(`入选至今 · 超额（vs ${rep.benchmark}）`, s.excess_since),
      stat('入选期间 · 超额', s.excess_during)) : null,
    h('div', { class: 'warnbox' }, `⚠ ${rep.caveat}　当前共 ${s.total} 条，满 90 天的 ${s.mature_90d} 条。`),
    s.total ? h('div', { class: 'table-wrap' }, h('table', { class: 'mini' },
      h('thead', null, h('tr', null, ['代码', '入选日', '入选价', '现价', '收益', '超额', '状态'].map((t) => h('th', null, t)))),
      h('tbody', null, rep.entries.map((e) => h('tr', null,
        h('td', null, h('b', null, e.ticker)),
        h('td', { class: 'num' }, e.flagged_at.slice(0, 10)),
        h('td', { class: 'num' }, e.flag_price?.toFixed(2)),
        h('td', { class: 'num' }, e.last_price?.toFixed(2) ?? '—'),
        h('td', { class: `num ${e.return_since_pct > 0 ? 'up' : e.return_since_pct < 0 ? 'down' : ''}` }, pct(e.return_since_pct)),
        h('td', { class: `num ${e.excess_since_pct > 0 ? 'up' : e.excess_since_pct < 0 ? 'down' : ''}` }, pct(e.excess_since_pct)),
        h('td', null, e.status === 'open' ? '仍在坑里' : `已离开 → ${tierLabel(e.exit_tier)}`))))))
      : h('p', { class: 'muted' }, '还没有记录。扫描出第一只黄金坑后就会开始追踪。'));
}

async function openHistory() {
  const box = $('history-body');
  fill(box, h('h2', null, '历史扫描'), h('p', { class: 'muted' }, '加载中…'));
  $('dlg-history').showModal();
  try {
    const { items } = await api('/api/history');
    fill(box, h('h2', null, '历史扫描'),
      items.length ? items.map((it) => h('button', {
        class: 'hist', onclick: async () => { $('dlg-history').close(); await loadHistory(it.name); },
      }, h('span', null, fmt.when(it.finished_at), ' · ', it.trigger === 'scheduled' ? '定时' : '手动'),
        h('span', { class: 'muted' }, `${it.count} 只 · 黄金坑 ${it.summary.golden_pit ?? 0}`))) : h('p', { class: 'muted' }, '还没有历史记录'));
  } catch (e) { box.append(h('p', { class: 'error' }, e.message)); }
}

function setupScanDialog() {
  const dlg = $('dlg-scan'), form = $('scan-form');
  const sync = () => {
    const m = form.mode.value;
    $('custom-tickers').hidden = m !== 'custom';
    $('watch-note').textContent = state.watch.size ? `将扫描 ${state.watch.size} 只：${[...state.watch].join(', ')}` : '还没有自选，先在表格里点 ☆';
    $('watch-note').hidden = m !== 'watch';
  };
  form.addEventListener('change', sync);
  $('btn-scan').addEventListener('click', () => {
    $('scan-error').hidden = true;
    $('token-row').hidden = !state.config.auth_required;
    $('token').value = sessionStorage.getItem('gp_token') || '';
    sync(); dlg.showModal();
  });
  form.addEventListener('submit', async (e) => {
    if (e.submitter?.value !== 'go') return;          // “取消”走默认关闭
    e.preventDefault();
    const err = $('scan-error'); err.hidden = true;
    let mode = form.mode.value;
    let tickers = $('custom-tickers').value;
    if (mode === 'watch') {
      if (!state.watch.size) { err.textContent = '还没有自选'; err.hidden = false; return; }
      mode = 'custom'; tickers = [...state.watch];
    }
    const headers = { 'Content-Type': 'application/json' };
    const token = $('token').value.trim();
    if (state.config.auth_required) { headers['X-Admin-Token'] = token; sessionStorage.setItem('gp_token', token); }
    try {
      await api('/api/scan', { method: 'POST', headers, body: JSON.stringify({ mode, tickers }) });
      dlg.close(); pollStatus();
    } catch (ex) { err.textContent = ex.message; err.hidden = false; }
  });
}

// ---------------------------------------------------------------- 扫描进度
function pollStatus() {
  clearInterval(state.polling);
  const tick = async () => {
    let s;
    try { s = await api('/api/scan/status'); } catch { return; }
    const box = $('progress');
    if (s.running) {
      box.hidden = false;
      $('progress-msg').textContent = s.message || '扫描中…';
      $('progress-count').textContent = s.total ? `${s.done}/${s.total}${s.current ? ' · ' + s.current : ''} · ${s.elapsed ?? 0}s` : '';
      $('progress-fill').style.width = s.total ? `${Math.round((s.done / s.total) * 100)}%` : '5%';
      $('btn-scan').disabled = true;
    } else {
      clearInterval(state.polling); state.polling = null;
      $('btn-scan').disabled = false;
      if (s.stage === 'error') {
        box.hidden = true;
        const b = $('banner'); b.hidden = false; b.className = 'banner error'; b.textContent = '扫描失败：' + (s.error || '未知错误');
      } else if (s.stage === 'done') { box.hidden = true; await loadLatest(); }
      else box.hidden = true;
    }
  };
  tick(); state.polling = setInterval(tick, 1500);
}

// ---------------------------------------------------------------- 数据加载 / 总渲染
function render() {
  renderFreshness(); renderBanner(); renderSummary(); renderChanges(); renderTable();
}

async function loadLatest() {
  state.viewingHistory = null;
  state.scan = (await api('/api/scan/latest')).scan;
  state.ai = state.scan && state.config.ai.enabled ? (await api('/api/ai').catch(() => ({ items: {} }))).items : {};
  render();
}

async function loadHistory(name) {
  state.scan = (await api('/api/history/' + encodeURIComponent(name))).scan;
  state.viewingHistory = name;
  render();
}

async function init() {
  state.config = await api('/api/config');
  renderMethod(); setupScanDialog();
  $('btn-history').addEventListener('click', openHistory);
  $('btn-track').addEventListener('click', openTrack);
  $('btn-method').addEventListener('click', () => $('dlg-method').showModal());
  $('scrim').addEventListener('click', closeDrawer);
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !$('drawer').hidden) closeDrawer(); });
  $('search').addEventListener('input', (e) => { state.query = e.target.value; renderTable(); });
  $('only-watch').addEventListener('change', (e) => { state.onlyWatch = e.target.checked; renderTable(); });
  $('hide-noise').addEventListener('change', (e) => { state.hideNoise = e.target.checked; renderTable(); });
  await loadLatest();
  const s = await api('/api/scan/status');
  if (s.running) pollStatus();
  setInterval(renderFreshness, 60000);
}

init().catch((e) => {
  $('freshness').textContent = '无法连接服务：' + e.message;
  $('freshness').classList.add('stale');
});
