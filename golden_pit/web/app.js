// 黄金坑筛选器前端。所有数据只通过 textContent / DOM API 写入页面，没有 innerHTML。
// 界面文字来自 i18n.js；结论、理由、指标等由后端按 ?lang= 渲染好再返回。
import { DICT } from './i18n.js';

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

// ---------------------------------------------------------------- 语言
const detectLang = () => store.get('gp_lang', null) || ((navigator.language || '').toLowerCase().startsWith('zh') ? 'zh' : 'en');
let LANG = detectLang();

/** t('key', {x: 1}) → 当前语言的文字，{x} 被替换；缺词时回退英文、再回退 key 本身 */
function t(key, params) {
  const s = DICT[LANG][key] ?? DICT.en[key] ?? key;
  return params ? s.replace(/\{(\w+)\}/g, (m, k) => (params[k] ?? m)) : s;
}

function applyStaticText() {
  document.documentElement.lang = LANG === 'zh' ? 'zh-CN' : 'en';
  document.title = t('title');
  for (const el of document.querySelectorAll('[data-i18n]')) el.textContent = t(el.dataset.i18n);
  for (const el of document.querySelectorAll('[data-i18n-ph]')) el.placeholder = t(el.dataset.i18nPh);
  for (const el of document.querySelectorAll('[data-i18n-aria]')) el.setAttribute('aria-label', t(el.dataset.i18nAria));
  $('btn-lang').textContent = t('lang.switch');
  $('btn-lang').title = t('lang.switch.title');
  $('btn-csv').href = `/api/export.csv?lang=${LANG}`;
  $('btn-md').href = `/api/export.md?lang=${LANG}`;
}

const state = {
  config: null, scan: null, viewingHistory: null,
  tier: null, query: '', hideNoise: true,
  sort: { key: 'verdict', dir: 'asc' }, polling: null,
  ai: {}, onlyWatch: false,
  watch: new Set(store.get('gp_watch', [])),
  plan: store.get('gp_plan', { capital: '', cap: 5 }),
  details: {},
};

const ICON = { pass: '✓', fail: '✗', unknown: '?', na: '–' };

// ---------------------------------------------------------------- 工具
async function api(path, opts = {}) {
  const url = path + (path.includes('?') ? '&' : '?') + 'lang=' + LANG;
  const res = await fetch(url, opts);
  let body = null;
  try { body = await res.json(); } catch { /* 非 JSON（如导出） */ }
  if (!res.ok) throw new Error((body && body.error) || t('req.failed', { s: res.status }));
  return body;
}

const fmt = {
  price: (v) => (v == null ? '—' : v.toFixed(2)),
  pct: (v) => (v == null ? '—' : v.toFixed(1) + '%'),
  cap: (v) => {
    if (!v) return '—';
    for (const [u, d] of [['T', 1e12], ['B', 1e9], ['M', 1e6]]) if (v >= d) return `$${(v / d).toFixed(1)}${u}`;
    return '$' + Math.round(v);
  },
  ago(iso) {
    const s = (Date.now() - new Date(iso).getTime()) / 1000;
    if (s < 90) return t('ago.now');
    if (s < 5400) return t('ago.min', { n: Math.round(s / 60) });
    if (s < 129600) return t('ago.h', { n: Math.round(s / 3600) });
    return t('ago.d', { n: Math.round(s / 86400) });
  },
  when: (iso) => new Date(iso).toLocaleString(LANG === 'zh' ? 'zh-CN' : 'en-US', { hour12: false }),
  num: (v) => Number(v).toLocaleString(LANG === 'zh' ? 'zh-CN' : 'en-US'),
};

const check = (r, key) => r.checks.find((c) => c.key === key);
const tierLabel = (tr) => (state.config?.tiers[tr]?.label ?? tr);

// ---------------------------------------------------------------- 渲染：顶部
function renderFreshness() {
  const el = $('freshness');
  const s = state.scan;
  el.className = 'muted';
  if (!s) { el.textContent = t('fresh.none'); return; }
  const ageH = (Date.now() - new Date(s.finished_at).getTime()) / 3.6e6;
  el.textContent = t('fresh.at', { ago: fmt.ago(s.finished_at), when: fmt.when(s.finished_at), note: s.universe.note });
  if (ageH > 36 && !state.viewingHistory) {
    el.classList.add('stale');
    el.textContent += t('fresh.stale');
  }
}

function renderBanner() {
  const b = $('banner');
  b.replaceChildren();
  if (state.viewingHistory) {
    b.hidden = false; b.className = 'banner';
    b.append(t('hist.viewing', { when: fmt.when(state.scan.finished_at) }),
      h('button', { onclick: () => loadLatest() }, t('hist.back')));
  } else b.hidden = true;
}

function renderSummary() {
  const box = $('summary');
  box.replaceChildren();
  if (!state.scan) return;
  const { summary } = state.scan;
  for (const tier of state.config.tier_order) {
    const meta = state.config.tiers[tier];
    const n = summary[tier] || 0;
    if (!n && (tier === 'error' || tier === 'n_a')) continue;
    box.append(h('button', {
      class: `tile t-${tier}`, 'aria-pressed': state.tier === tier, title: meta.desc,
      onclick: () => { state.tier = state.tier === tier ? null : tier; render(); },
    }, h('div', { class: 'n num' }, n), h('div', { class: 'l' }, meta.label),
    tier === 'golden_pit' ? gradeSplit() : null));
  }
}

function renderChanges() {
  const box = $('changes');
  const c = state.scan?.changes;
  box.replaceChildren();
  if (!c || !c.has_previous) { box.hidden = true; return; }
  const sep = t('list.sep');
  const parts = [];
  if (c.entered_golden_pit.length) parts.push(h('span', null, h('b', null, t('chg.entered')), c.entered_golden_pit.join(sep)));
  if (c.left_golden_pit.length) parts.push(h('span', null, h('b', null, t('chg.left')),
    c.left_golden_pit.map((x) => `${x.ticker}→${tierLabel(x.now)}`).join(sep)));
  box.hidden = !parts.length;
  box.append(t('chg.prefix', { ago: fmt.ago(c.previous_finished_at) }), ...parts.flatMap((p, i) => (i ? ['　', p] : [p])));
}

/** 黄金坑里 A/B/C 各几只（老的历史扫描没有分档，就不显示） */
function gradeSplit() {
  const n = { a: 0, b: 0, c: 0 };
  for (const r of state.scan.results) if (r.verdict.rank) n[r.verdict.rank.grade]++;
  if (!n.a && !n.b && !n.c) return null;
  return h('div', { class: 'split num' }, `A ${n.a} · B ${n.b} · C ${n.c}`);
}

function gradeChip(g, title) {
  return g ? h('span', { class: `grade g-${g}`, title: title || t(`grade.${g}`) }, g.toUpperCase()) : null;
}

// ---------------------------------------------------------------- 渲染：表格
const COLUMNS = [
  { key: 'watch', label: () => '★' },
  { key: 'verdict', label: () => t('col.verdict') },
  { key: 'ticker', label: () => t('col.company') },
  { key: 'price', label: () => t('col.price'), cls: 'r' },
  { key: 'drop', label: () => t('col.drop'), cls: 'r' },
  { key: 'pb', label: () => t('col.pb'), cls: 'r hide-sm' },
  { key: 'fpe', label: () => t('col.fpe'), cls: 'r hide-sm' },
  { key: 'fin', label: () => t('col.fin'), cls: 'hide-sm' },
  { key: 'mgmt', label: () => t('col.mgmt'), cls: 'hide-sm' },
  { key: 'timing', label: () => t('col.timing') },
  { key: 'unknown', label: () => t('col.data') },
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
  const row = h('tr');
  for (const col of COLUMNS) {
    const active = state.sort.key === col.key;
    row.append(h('th', { class: col.cls, scope: 'col', 'aria-sort': active ? (state.sort.dir === 'asc' ? 'ascending' : 'descending') : 'none' },
      h('button', {
        onclick: () => {
          state.sort = { key: col.key, dir: active && state.sort.dir === 'asc' ? 'desc' : 'asc' };
          renderTable();
        },
      }, col.label())));
  }
  $('table').tHead.replaceChildren(row);
}

function renderTable() {
  renderHead();
  const body = $('table').tBodies[0];
  body.replaceChildren();
  const empty = $('empty');
  if (!state.scan) {
    empty.hidden = false; empty.textContent = t('empty.none');
    $('count').textContent = ''; return;
  }
  const rows = visibleRows();
  empty.hidden = rows.length > 0;
  empty.textContent = t('empty.filter');
  $('count').textContent = t('count', { n: rows.length, total: state.scan.results.length });
  // 按结论排序时分组显示：黄金坑按 A/B/C 分三段，其余每个档位一段
  const grouped = state.sort.key === 'verdict';
  const groupOf = (r) => (r.verdict.rank ? `${r.verdict.tier}:${r.verdict.rank.grade}` : r.verdict.tier);
  const sizes = {};
  for (const r of rows) sizes[groupOf(r)] = (sizes[groupOf(r)] || 0) + 1;
  let lastGroup = null;
  for (const r of rows) {
    const v = r.verdict;
    if (grouped && groupOf(r) !== lastGroup) {
      lastGroup = groupOf(r);
      body.append(groupRow(r, sizes[lastGroup]));
    }
    const open = () => openDrawer(r.ticker);
    const fin = r.checks.filter((c) => c.group === 'financial');
    const mgmt = r.checks.filter((c) => c.group === 'management');
    body.append(h('tr', {
      tabindex: 0, onclick: open, onkeydown: (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); } },
    },
      h('td', { class: 'star-cell' }, starButton(r.ticker)),
      h('td', { class: 'verdict-cell' }, h('span', { class: `badge t-${v.tier}` }, v.label), gradeChip(v.rank?.grade, v.rank?.label), aiMark(r),
        v.rank ? h('small', { class: 'gaps' }, v.rank.gap_labels.length ? t('rk.gaps', { list: v.rank.gap_labels.join(t('list.sep')) }) : t('rk.nogaps')) : null),
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

function groupRow(r, n) {
  const v = r.verdict, meta = state.config.tiers[v.tier];
  const title = v.rank ? `${meta.label} · ${v.rank.label}` : meta.label;
  const hint = v.rank ? t(`grp.hint.${v.rank.grade}`) : meta.desc;
  return h('tr', { class: `grp t-${v.tier}` }, h('td', { colspan: COLUMNS.length },
    h('b', null, title), h('span', { class: 'num' }, ` · ${t('grp.count', { n })}`), h('span', { class: 'grp-hint' }, hint)));
}

function starButton(ticker) {
  const on = state.watch.has(ticker);
  return h('button', {
    class: 'star' + (on ? ' on' : ''), 'aria-pressed': on, 'aria-label': t(on ? 'star.remove' : 'star.add', { t: ticker }),
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
  return rec ? h('span', { class: 'ai-dot', title: t(rec.stale ? 'ai.has_stale' : 'ai.has') }, rec.stale ? ' ◌' : ' ◉') : null;
}

function timingBadge(tm) {
  if (!tm || tm.state === 'unknown') return h('span', { class: 'muted' }, '—');
  return h('span', { class: `tm tm-${tm.state}`, title: tm.reason }, tm.label);
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
  return svg('svg', { viewBox: `0 0 ${W} ${H}`, class: 'chart', role: 'img', 'aria-label': t('chart.aria', { t: r.ticker, p: last }) },
    ...(high ? hline(high, 'ch-high', t('chart.high', { v: high.toFixed(0) })) : []),
    ...levels.flatMap((lv) => hline(lv.price, lv.reached ? 'ch-lv reached' : 'ch-lv', `-${lv.drop_pct}%`)),
    svg('path', { d: path, class: 'ch-line' }),
    svg('circle', { cx: x(c.closes.length - 1), cy: y(last), r: 3.5, class: 'ch-dot' }),
    svg('text', { x: L, y: H - 6, class: 'ch-axis' }, c.dates[0]),
    svg('text', { x: W - R, y: H - 6, class: 'ch-axis', 'text-anchor': 'end' }, c.dates[c.dates.length - 1]));
}

function timingCard(r) {
  const tm = r.timing;
  if (!tm || r.error) return null;
  const m = tm.metrics || {};
  return h('section', { class: 'card' },
    h('h3', null, t('timing.title'), h('small', null, t('timing.sub'))),
    priceChart(r),
    h('p', { class: 'tm-line' }, timingBadge(tm), ' ', tm.reason),
    m.ma20 ? h('div', { class: 'muted small' },
      t('timing.metrics', { ma20: m.ma20, ma50: m.ma50 ?? '—', low: m.low_1y, date: m.low_date, rsi: m.rsi14 ?? '—' })) : null);
}

/** 同为黄金坑，这只排在哪一档、差在哪 */
function rankCard(r) {
  const rk = r.verdict.rank;
  if (!rk) return null;
  return h('section', { class: 'card rank' },
    h('h3', null, t('rk.title'), h('small', null, gradeChip(rk.grade, rk.label), ' ', rk.label)),
    h('p', { class: 'muted small' }, t('rk.explain')),
    rk.conditions.map((c) => h('div', { class: `chk ${c.ok ? 'pass' : 'unknown'}` },
      h('span', { class: `ic ${c.ok ? 'pass' : 'unknown'}`, 'aria-label': t(c.ok ? 'rk.met' : 'rk.missed') }, c.ok ? '✓' : '–'),
      h('span', { class: 'lab' }, c.label),
      h('span', { class: 'val' }, c.ok ? '' : c.gap))));
}

function valueOf(r, key) {
  const c = check(r, key);
  if (!c || c.status === 'unknown') return '—';
  return c.display;
}

function dataCell(r) {
  if (r.error) return h('span', { class: 'warn' }, t('data.error'));
  const bits = [];
  if (r.verdict.unknown_count) bits.push(h('span', { class: 'warn', title: t('data.unknown_tip') }, t('data.unknown', { n: r.verdict.unknown_count })));
  if (r.warnings.length) bits.push(h('span', { class: 'warn', title: r.warnings.join('\n') }, '⚠'));
  return bits.length ? bits.flatMap((b, i) => (i ? [' ', b] : [b])) : h('span', { class: 'muted' }, t('data.ok'));
}

// ---------------------------------------------------------------- 详情抽屉
function checkRow(c) {
  const status = t(`status.${c.status}`);
  return h('div', { class: `chk ${c.status}` },
    h('span', { class: `ic ${c.status}`, title: status, 'aria-label': status }, ICON[c.status]),
    h('span', { class: 'lab' }, c.label),
    h('span', { class: 'val num' }, c.display),
    c.rule ? h('span', { class: 'rule' }, t('rule.needs', { r: c.rule })) : null,
    c.note ? h('span', { class: 'note' }, c.note) : null);
}

function group(title, sub, checks) {
  if (!checks.length) return null;
  return h('section', { class: 'card' }, h('h3', null, title, h('small', null, sub)), checks.map(checkRow));
}

/** 列表数据不含走势图；打开详情时按需取完整结果（历史扫描本来就没有图表，不取） */
async function openDrawer(ticker) {
  let r = (!state.viewingHistory && state.details[ticker]) || state.scan.results.find((x) => x.ticker === ticker);
  if (!r) return;
  if (!state.viewingHistory && r.slim && !r.error) {
    try {
      r = (await api('/api/scan/latest/' + encodeURIComponent(ticker))).result;
      state.details[ticker] = r;
    } catch { /* 取不到就用列表里的数据，只是没有图 */ }
  }
  const v = r.verdict, g = (name) => r.checks.filter((c) => c.group === name);
  const body = $('drawer-body');
  fill(body,
    h('div', { class: 'd-head' },
      h('div', null, h('h2', null, `${r.ticker} `, h('span', { class: `badge t-${v.tier}` }, v.label)),
        h('div', { class: 'sub' }, [r.name, r.sector, r.industry].filter(Boolean).join(' · '))),
      h('div', { class: 'd-tools' }, starButton(r.ticker), h('button', { class: 'd-close', onclick: closeDrawer, 'aria-label': t('close') }, '✕'))),
    r.error ? null : h('div', { class: 'd-price' },
      h('span', null, t('d.price'), h('b', { class: 'num' }, fmt.price(r.price)), ` ${r.currency}`),
      h('span', null, t('d.drop'), h('b', { class: 'num' }, fmt.pct(r.drop_from_high_pct))),
      h('span', null, t('d.mcap'), h('b', { class: 'num' }, fmt.cap(r.market_cap))),
      r.dividend_yield_pct != null ? h('span', null, t('d.div'), h('b', { class: 'num' }, r.dividend_yield_pct + '%')) : null),
    r.warnings.length ? h('div', { class: 'warnbox' }, r.warnings.map((w) => h('div', null, '⚠ ' + w))) : null,
    h('section', { class: 'card' }, h('h3', null, t('d.why')),
      h('ul', { class: 'reasons' }, (v.reasons || []).map((x) => h('li', null, x)))),
    rankCard(r),
    timingCard(r),
    planCard(r),
    aiCard(r),
    group(t('g.fin'), t('g.pass', { p: v.fin_pass, t: v.fin_total }), g('financial')),
    group(t('g.mgmt'), t('g.pass', { p: v.mgmt_pass, t: 3 }), g('management')),
    group(t('g.val'), t('g.anchors', { a: v.anchors_pass }), g('valuation')),
    r.moat ? h('section', { class: 'card moat' }, h('h3', null, t('moat.title'), h('small', null, t('moat.sub'))),
      h('p', null, r.moat.summary),
      h('dl', null,
        h('dt', null, t('moat.disruption', { v: r.moat.disruption_risk })), h('dd', null, r.moat.disruption_view),
        h('dt', null, t('moat.capacity', { v: r.moat.capacity_barrier })), h('dd', null, r.moat.capacity_view)),
      h('div', { class: 'src' }, r.moat.source)) : null,
    h('div', { class: 'foot' },
      h('span', null, t('foot.fetched', { when: fmt.when(r.fetched_at) })),
      r.statements_as_of ? h('span', null, t('foot.annual', { d: r.statements_as_of })) : null,
      r.fundamentals_fetched_at ? h('span', null, t('foot.fund', { when: fmt.when(r.fundamentals_fetched_at) })) : null,
      h('a', { href: `https://finance.yahoo.com/quote/${encodeURIComponent(r.ticker)}`, target: '_blank', rel: 'noopener noreferrer' }, t('foot.yahoo'))),
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
    if (v.tier === 'value_trap') return h('section', { class: 'card' }, h('h3', null, t('plan.title')),
      h('p', { class: 'muted' }, t('plan.trap')));
    return null;
  }
  const body = h('div');
  const draw = () => {
    const cap = parseFloat(state.plan.capital), pct = parseFloat(state.plan.cap);
    const budget = cap > 0 && pct > 0 ? cap * pct / 100 : null;
    fill(body,
      h('table', { class: 'mini plan' },
        h('thead', null, h('tr', null, ['plan.col.level', 'plan.col.price', 'plan.col.weight', budget ? 'plan.col.amount' : null, 'plan.col.status']
          .map((k) => h('th', null, k ? t(k) : '')))),
        h('tbody', null, r.plan.levels.map((lv) => {
          const amt = budget ? budget * lv.weight_pct / 100 : null;
          return h('tr', null,
            h('td', { class: 'num' }, `-${lv.drop_pct}%`),
            h('td', { class: 'num' }, `$${lv.price.toFixed(2)}`),
            h('td', { class: 'num' }, `${lv.weight_pct}%`),
            h('td', { class: 'num' }, amt ? t('plan.shares', { amt: '$' + fmt.num(Math.round(amt)), n: Math.floor(amt / lv.price) }) : ''),
            h('td', null, lv.reached ? h('b', { class: 'reached' }, t('plan.reached')) : t('plan.away', { p: lv.away_pct })));
        }))),
      budget ? h('p', { class: 'muted small' }, t('plan.budget', { cap: fmt.num(cap), pct, b: fmt.num(Math.round(budget)) })) : null);
  };
  const input = (key, label, ph) => h('label', { class: 'mini-field' }, label,
    h('input', {
      type: 'number', min: '0', step: 'any', value: state.plan[key], placeholder: ph, inputmode: 'decimal',
      oninput: (e) => { state.plan[key] = e.target.value; store.set('gp_plan', state.plan); draw(); },
    }));
  draw();
  return h('section', { class: 'card' },
    h('h3', null, t('plan.title'), h('small', null, t('plan.anchor', { v: r.plan.anchor_high }))),
    h('div', { class: 'planner-inputs' }, input('capital', t('plan.capital'), t('plan.capital_ph')), input('cap', t('plan.cap'), '5')),
    body,
    h('div', { class: 'invalid' }, h('b', null, t('plan.invalid')),
      h('ul', null, r.plan.invalidation.map((x) => h('li', null, x)))),
    h('p', { class: 'muted small' }, t('plan.disclaimer')));
}

// ---------------------------------------------------------------- AI 研判
function adminHeaders() {
  const headers = { 'Content-Type': 'application/json' };
  if (state.config.auth_required) {
    let tok = sessionStorage.getItem('gp_token') || '';
    if (!tok) { tok = window.prompt(t('token.prompt')) || ''; if (tok) sessionStorage.setItem('gp_token', tok); }
    headers['X-Admin-Token'] = tok;
  }
  return headers;
}

function aiCard(r) {
  if (r.error) return null;
  const cfg = state.config.ai;
  const box = h('section', { class: 'card ai' });
  const draw = () => {
    const rec = state.ai[r.ticker];
    const head = h('h3', null, t('ai.title'), h('small', null, t('ai.sub')));
    if (!cfg.enabled) { fill(box, head, h('p', { class: 'muted' }, t('ai.disabled'))); return; }
    const btn = h('button', {
      class: rec ? '' : 'primary',
      onclick: async (e) => {
        e.target.disabled = true; e.target.textContent = t('ai.running');
        try {
          const res = await api('/api/ai/' + encodeURIComponent(r.ticker), { method: 'POST', headers: adminHeaders() });
          state.ai[r.ticker] = res.item; renderTable();
        } catch (ex) { box.append(h('p', { class: 'error' }, ex.message)); }
        draw();
      },
    }, t(rec ? 'ai.regen' : 'ai.gen'));
    if (!rec) { fill(box, head, h('p', { class: 'muted' }, t('ai.intro')), btn); return; }
    const a = rec.analysis;
    const list = (title, items) => (items.length ? h('div', null, h('h4', null, title), h('ul', null, items.map((x) => h('li', null, x)))) : null);
    fill(box, head,
      rec.stale ? h('div', { class: 'warnbox' }, t('ai.stale')) : null,
      h('div', { class: 'ai-tags' },
        h('span', { class: 'badge t-quality_wait' }, a.pit_type_label),
        h('span', { class: 'badge t-fail' }, a.agree_label),
        h('span', { class: 'badge t-fail' }, t('ai.conf', { v: a.confidence_label }))),
      h('p', null, a.pit_reason),
      h('h4', null, t('ai.thesis')), h('p', null, a.thesis),
      list(t('ai.bear'), a.bear_case), list(t('ai.catalysts'), a.catalysts),
      list(t('ai.verify'), a.verify), list(t('ai.gaps'), a.data_gaps),
      h('p', { class: 'muted small' }, t('ai.footer', { model: rec.model, when: fmt.when(rec.generated_at) })),
      btn);
  };
  draw();
  return box;
}

function closeDrawer() { $('drawer').hidden = true; $('scrim').hidden = true; }

// ---------------------------------------------------------------- 对话框
function renderMethod() {
  const th = state.config.thresholds;
  const li = (...x) => h('li', null, ...x);
  fill($('method-body'),
    h('h2', null, t('method.title')),
    h('p', null, t('method.intro')),
    h('h3', null, t('method.q1')),
    h('ul', null,
      li(t('method.fin', { n: th.financial_min_pass, ic: th.interest_coverage_min, cr: th.current_ratio_min, nd: th.net_debt_to_ebitda_max, z: th.altman_z_min })),
      li(t('method.mgmt', { n: th.management_min_pass, sh: th.shares_reduction_min_pct, bb: th.buyback_yield_min_pct, cx: th.capex_to_revenue_max_pct }))),
    h('h3', null, t('method.q2')),
    h('ul', null,
      li(t('method.drop', { d: th.drop_from_high_min_pct })),
      li(t('method.anchor', { pb: th.pb_max, pe: th.forward_pe_max, n: th.pit_min_anchors }))),
    h('h3', null, t('method.rank')),
    h('p', null, t('method.rank.intro')),
    h('ul', null, li(t('method.rank.list')), li(t('method.rank.grades')), li(t('method.rank.check'))),
    h('h3', null, t('method.integrity')),
    h('ul', null,
      li(t('method.i1', { n: th.max_unknown_checks })), li(t('method.i2')), li(t('method.i3')), li(t('method.i4')), li(t('method.i5'))),
    h('p', { class: 'muted' }, t('method.foot')));
}

async function openTrack() {
  const box = $('track-body');
  fill(box, h('h2', null, t('track.title')), h('p', { class: 'muted' }, t('loading')));
  $('dlg-track').showModal();
  try { state.track = await api('/api/track'); } catch (e) { box.append(h('p', { class: 'error' }, e.message)); return; }
  drawTrack();
}

const pctS = (v) => (v == null ? '—' : `${v > 0 ? '+' : ''}${v.toFixed(1)}%`);
const signCls = (v) => (v > 0 ? 'up' : v < 0 ? 'down' : '');
const ripe = (e, rep) => (e.days ?? 0) >= rep.summary.min_days_for_stats;

/** 一根以 0 为中心的横条：负数往左、正数往右。宽度用 CSSOM 设置（CSP 不允许内联 style 属性）。 */
function centerBar(v, max, cls) {
  const fill = h('span', { class: `cfill ${v < 0 ? 'neg' : 'pos'} ${cls || ''}` });
  fill.style.width = `${Math.min(50, (Math.abs(v) / max) * 50)}%`;
  return h('span', { class: 'ctrack' }, fill);
}

function trackVerdict(rep) {
  const s = rep.summary, o = s.overall, b = rep.benchmark;
  if (!o || !o.n) return h('section', { class: 'card tv' }, h('p', null, t('track.v.none', { d: s.min_days_for_stats })));
  const max = Math.max(Math.abs(o.ret_mean), Math.abs(o.bench_mean), 1);
  const line = (label, v, cls) => h('div', { class: 'vrow' }, h('span', { class: 'vl' }, label), centerBar(v, max, cls),
    h('b', { class: `num ${signCls(v)}` }, pctS(v)));
  return h('section', { class: 'card tv' },
    h('p', { class: 'tv-main' }, t(o.excess_mean >= 0 ? 'track.v.won' : 'track.v.lost',
      { n: o.n, ret: pctS(o.ret_mean), b, bench: pctS(o.bench_mean), ex: Math.abs(o.excess_mean).toFixed(1), beat: Math.round(o.beat_rate) })),
    line(t('track.v.ours'), o.ret_mean, 'ours'), line(b, o.bench_mean, 'bench'),
    s.max_days < 90 ? h('p', { class: 'warn-line' }, t('track.v.young', { d: s.max_days })) : null,
    h('p', { class: 'muted small' }, t('track.v.how', { b })));
}

function gradeTable(rep) {
  const by = rep.summary.by_grade || {};
  const keys = ['a', 'b', 'c', 'none'].filter((g) => by[g]?.n);
  if (!keys.length) return null;
  return h('section', { class: 'card' },
    h('h3', null, t('track.g.title')),
    h('p', { class: 'muted small' }, t('track.g.intro')),
    h('div', { class: 'table-wrap flat' }, h('table', { class: 'mini' },
      h('thead', null, h('tr', null, ['grade', 'n', 'ret', 'bench', 'excess', 'beat']
        .map((k) => h('th', { class: k === 'grade' ? '' : k === 'bench' ? 'r hide-sm' : 'r' }, t(`track.g.c.${k}`, { b: rep.benchmark }))))),
      h('tbody', null, keys.map((g) => {
        const x = by[g];
        return h('tr', { class: x.n < 10 ? 'thin' : '' },
          h('td', { class: 'nowrap' }, g === 'none' ? t('grade.none') : [gradeChip(g), h('span', { class: 'hide-sm' }, ' ' + t(`grade.${g}`))]),
          h('td', { class: 'r num' }, x.n),
          h('td', { class: `r num ${signCls(x.ret_mean)}` }, pctS(x.ret_mean)),
          h('td', { class: 'r num muted hide-sm' }, pctS(x.bench_mean)),
          h('td', { class: `r num ${signCls(x.excess_mean)}` }, h('b', null, pctS(x.excess_mean))),
          h('td', { class: 'r num' }, `${Math.round(x.beat_rate)}%`));
      })))),
    h('p', { class: 'muted small' }, t('track.g.note')));
}

function exitTable(rep) {
  const ex = rep.summary.exits || {};
  const keys = Object.keys(ex).sort((a, b) => ex[b].n - ex[a].n);
  if (!keys.length) return null;
  return h('section', { class: 'card' },
    h('h3', null, t('track.x.title')),
    h('p', { class: 'muted small' }, t('track.x.intro', { b: rep.benchmark })),
    keys.map((k) => h('div', { class: 'xrow' },
      h('span', { class: `badge t-${k}` }, tierLabel(k)),
      h('span', { class: 'xmean' }, t(`track.x.${k}`) === `track.x.${k}` ? '' : t(`track.x.${k}`)),
      h('span', { class: 'num muted' }, t('track.x.n', { n: ex[k].n })),
      h('b', { class: `num ${signCls(ex[k].excess_during_mean)}` }, pctS(ex[k].excess_during_mean)))));
}

function bestWorst(rep) {
  const rs = rep.entries.filter((e) => ripe(e, rep) && e.excess_since_pct != null)
    .sort((a, b) => b.excess_since_pct - a.excess_since_pct);
  if (rs.length < 4) return null;
  const n = Math.min(5, Math.floor(rs.length / 2));
  const list = (title, items) => h('div', null, h('h4', null, title), items.map((e) => h('div', { class: 'bw' },
    h('b', null, e.ticker), gradeChip(e.grade), h('span', { class: 'muted small' }, t('track.days', { d: e.days })),
    h('span', { class: `num ${signCls(e.excess_since_pct)}` }, pctS(e.excess_since_pct)))));
  return h('section', { class: 'card' }, h('h3', null, t('track.bw.title'), h('small', null, t('track.bw.sub', { b: rep.benchmark }))),
    h('div', { class: 'bw-cols' }, list(t('track.bw.best'), rs.slice(0, n)), list(t('track.bw.worst'), rs.slice(-n).reverse())));
}

const TRACK_TABS = {
  ripe: (e, rep) => ripe(e, rep),
  open: (e) => e.status === 'open',
  exited: (e) => e.status === 'exited',
  young: (e, rep) => !ripe(e, rep),
};
const TRACK_COLS = [
  { key: 'ticker', get: (e) => e.ticker },
  { key: 'grade', get: (e) => e.grade || 'z' },
  { key: 'flagged', get: (e) => e.flagged_at, cls: 'hide-sm' },
  { key: 'days', get: (e) => e.days, cls: 'r' },
  { key: 'ret', get: (e) => e.return_since_pct, cls: 'r' },
  { key: 'bench', get: (e) => e.bench_since_pct, cls: 'r hide-sm' },
  { key: 'excess', get: (e) => e.excess_since_pct, cls: 'r' },
  { key: 'status', get: (e) => (e.status === 'open' ? '' : e.exit_tier || 'z') },
];

function trackList(rep) {
  const v = state.trackView || (state.trackView = { tab: 'ripe', sort: 'excess', dir: 'desc' });
  const box = h('section', { class: 'card' });
  const draw = () => {
    const col = TRACK_COLS.find((c) => c.key === v.sort);
    const rows = rep.entries.filter((e) => TRACK_TABS[v.tab](e, rep)).sort((a, b) => {
      const [x, y] = [col.get(a), col.get(b)];
      if (x == null) return 1; if (y == null) return -1;
      return cmp(x, y) * (v.dir === 'asc' ? 1 : -1);
    });
    fill(box,
      h('h3', null, t('track.list.title')),
      h('div', { class: 'tabs', role: 'tablist' }, Object.keys(TRACK_TABS).map((k) => h('button', {
        role: 'tab', 'aria-selected': v.tab === k, class: v.tab === k ? 'on' : '',
        onclick: () => { v.tab = k; draw(); },
      }, t(`track.tab.${k}`, { n: rep.entries.filter((e) => TRACK_TABS[k](e, rep)).length })))),
      v.tab === 'young' ? h('p', { class: 'muted small' }, t('track.tab.young_note', { d: rep.summary.min_days_for_stats })) : null,
      rows.length ? h('div', { class: 'table-wrap flat' }, h('table', { class: 'mini track' },
        h('thead', null, h('tr', null, TRACK_COLS.map((c) => {
          const active = v.sort === c.key;
          return h('th', { class: c.cls, 'aria-sort': active ? (v.dir === 'asc' ? 'ascending' : 'descending') : 'none' },
            h('button', { onclick: () => { v.dir = active && v.dir === 'desc' ? 'asc' : 'desc'; v.sort = c.key; draw(); } },
              t(`track.c.${c.key}`, { b: rep.benchmark })));
        }))),
        h('tbody', null, rows.map((e) => h('tr', null,
          h('td', null, h('b', null, e.ticker), e.nth > 1 ? h('small', { class: 'muted' }, ' ' + t('track.nth', { n: e.nth })) : null),
          h('td', null, gradeChip(e.grade) || h('span', { class: 'muted' }, '—')),
          h('td', { class: 'num hide-sm' }, e.flagged_at.slice(0, 10)),
          h('td', { class: 'r num' }, e.days ?? '—'),
          h('td', { class: `r num ${signCls(e.return_since_pct)}` }, pctS(e.return_since_pct)),
          h('td', { class: 'r num muted hide-sm' }, pctS(e.bench_since_pct)),
          h('td', { class: `r num ${signCls(e.excess_since_pct)}` }, h('b', null, pctS(e.excess_since_pct))),
          h('td', null, e.status === 'open' ? t('track.open') : t('track.exited', { tier: tierLabel(e.exit_tier) })))))))
        : h('p', { class: 'muted' }, t('track.list.empty')));
  };
  draw();
  return box;
}

function drawTrack() {
  const rep = state.track, s = rep.summary;
  fill($('track-body'),
    h('h2', null, t('track.title')),
    h('p', { class: 'muted small' }, t('track.intro', { b: rep.benchmark })),
    s.total ? [trackVerdict(rep), gradeTable(rep), bestWorst(rep), exitTable(rep), trackList(rep)]
      : h('p', { class: 'muted' }, t('track.empty')),
    h('div', { class: 'warnbox' }, `⚠ ${rep.caveat} ${t('track.count', { t: s.total, m: s.mature_90d })}`));
}

async function openHistory() {
  const box = $('history-body');
  fill(box, h('h2', null, t('hist.title')), h('p', { class: 'muted' }, t('loading')));
  $('dlg-history').showModal();
  try {
    const { items } = await api('/api/history');
    fill(box, h('h2', null, t('hist.title')),
      items.length ? items.map((it) => h('button', {
        class: 'hist', onclick: async () => { $('dlg-history').close(); await loadHistory(it.name); },
      }, h('span', null, fmt.when(it.finished_at), ' · ', t(it.trigger === 'scheduled' ? 'hist.scheduled' : 'hist.manual')),
        h('span', { class: 'muted' }, t('hist.row', { n: it.count, g: it.summary.golden_pit ?? 0 })))) : h('p', { class: 'muted' }, t('hist.empty')));
  } catch (e) { box.append(h('p', { class: 'error' }, e.message)); }
}

function setupScanDialog() {
  const dlg = $('dlg-scan'), form = $('scan-form');
  const sync = () => {
    const m = form.mode.value;
    $('custom-tickers').hidden = m !== 'custom';
    $('watch-note').textContent = state.watch.size ? t('watch.note', { n: state.watch.size, list: [...state.watch].join(', ') }) : t('watch.none');
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
      if (!state.watch.size) { err.textContent = t('watch.empty_err'); err.hidden = false; return; }
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
      $('progress-msg').textContent = s.message || t('progress.scanning');
      $('progress-count').textContent = s.total ? `${s.done}/${s.total}${s.current ? ' · ' + s.current : ''} · ${s.elapsed ?? 0}s` : '';
      $('progress-fill').style.width = s.total ? `${Math.round((s.done / s.total) * 100)}%` : '5%';
      $('btn-scan').disabled = true;
    } else {
      clearInterval(state.polling); state.polling = null;
      $('btn-scan').disabled = false;
      if (s.stage === 'error') {
        box.hidden = true;
        const b = $('banner'); b.hidden = false; b.className = 'banner error';
        b.textContent = t('scan.failed', { e: s.error || t('unknown_error') });
      } else if (s.stage === 'done') { box.hidden = true; await loadLatest(); }
      else box.hidden = true;
    }
  };
  tick(); state.polling = setInterval(tick, 1500);
}

// ---------------------------------------------------------------- 大盘
// 三大指数 / 七巨头 / A 股：走势、回撤、"钟摆"指标、历史分档。全部是历史统计，不合成分数。
const MK_TABS = ['us', 'm7', 'cn'];
const MK_RANGES = { '1y': 365, '3y': 1095, '10y': 3652, all: null };
const mk = { tab: 'us', data: {}, sel: { us: 'spx', m7: 'nvda', cn: 'cyb' }, range: '10y', series: {}, showAllEp: false };

const mkName = (key) => t(`mk.n.${key}`);
const ddCls = (v) => (v == null ? '' : v <= -20 ? 'down strong' : v < 0 ? 'down' : '');
const numL = (v, d = 2) => (v == null ? '—' : Number(v).toLocaleString(LANG === 'zh' ? 'zh-CN' : 'en-US', { maximumFractionDigits: d }));
const pct0 = pctS;

async function openMarket() {
  $('dlg-market').showModal();
  drawMarket();
  loadMarket(mk.tab);
}

async function loadMarket(g, tries = 0) {
  try {
    const d = await api(`/api/market/${g}`);
    if (d.pending) {
      mk.data[g] = { pending: true };
      if (tries < 45 && $('dlg-market').open) setTimeout(() => loadMarket(g, tries + 1), 4000);
    } else {
      mk.data[g] = d;
    }
  } catch (e) { mk.data[g] = { error: e.message }; }
  if (mk.tab === g && $('dlg-market').open) drawMarket();
}

function drawMarket() {
  const g = mk.tab, d = mk.data[g];
  const head = [
    h('h2', null, t('mk.title')),
    h('p', { class: 'muted small' }, t('mk.intro')),
    h('div', { class: 'tabs', role: 'tablist' }, MK_TABS.map((k) => h('button', {
      role: 'tab', 'aria-selected': k === g, class: k === g ? 'on' : '',
      onclick: () => { mk.tab = k; mk.showAllEp = false; drawMarket(); if (!mk.data[k] || mk.data[k].error) loadMarket(k); },
    }, t(`mk.tab.${k}`))))];
  const box = $('market-body');
  if (!d || d.pending) return fill(box, head, h('p', { class: 'muted' }, t(d?.pending ? 'mk.pending' : 'loading')));
  if (d.error) return fill(box, head, h('p', { class: 'error' }, d.error));
  const ok = d.items.filter((it) => it.last != null);
  const sel = ok.find((it) => it.key === mk.sel[g]) || ok[0];
  fill(box, head,
    h('p', { class: 'muted small' }, t('mk.updated', { when: fmt.when(d.updated_at) }), d.refreshing ? ` · ${t('mk.refreshing')}` : ''),
    g === 'm7' ? m7Table(d.items, sel) : mkCards(d.items, sel),
    sel ? mkDetail(sel, g === 'us' ? d.vix : null) : null,
    h('div', { class: 'warnbox' }, '⚠ ', t('mk.caveat')),
    h('p', { class: 'muted small' }, t(g === 'cn' ? 'mk.src.cn' : 'mk.src.us')));
  if (sel) requestAnimationFrame(drawMkChart);
}

function pick(key) {
  mk.sel[mk.tab] = key; mk.showAllEp = false;
  drawMarket();
  $('mk-detail')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function mkCards(items, sel) {
  return h('div', { class: 'mk-cards' }, items.map((it) => h('button', {
    class: `mk-card${it === sel ? ' on' : ''}`, 'aria-pressed': it === sel, disabled: it.last == null,
    onclick: () => pick(it.key),
  },
  h('div', { class: 'mk-name' }, mkName(it.key)),
  it.last == null ? h('div', { class: 'error small' }, it.error) : [
    h('div', { class: 'mk-last num' }, numL(it.last)),
    h('div', { class: `mk-dd num ${ddCls(it.dd_ath)}` }, pct0(it.dd_ath), h('small', null, ` ${t('mk.from_high')}`)),
    it.cur && it.cur.dd !== it.dd_ath ? h('div', { class: 'small' }, t('mk.cur_swing', { v: pct0(it.cur.dd) })) : null,
    h('div', { class: 'small muted' }, t('mk.mdd_short', { v: pct0(it.mdd?.depth), y: it.mdd?.trough_date.slice(0, 4) ?? '—' })),
    it.stale ? h('div', { class: 'small warn' }, t('mk.stale')) : null])));
}

function m7Table(items, sel) {
  return h('div', { class: 'table-wrap flat' }, h('table', { class: 'mini mk-m7' },
    h('thead', null, h('tr', null,
      h('th', null, t('mk.c.name')), h('th', { class: 'r' }, t('mk.c.last')), h('th', { class: 'r' }, t('mk.c.dd')),
      h('th', { class: 'r' }, t('mk.c.mdd')), h('th', { class: 'r hide-sm' }, t('mk.c.mdd10')), h('th', { class: 'r hide-sm' }, t('mk.c.days')))),
    h('tbody', null, items.map((it) => (it.last == null
      ? h('tr', null, h('td', null, mkName(it.key)), h('td', { class: 'error small', colspan: 5 }, it.error))
      : h('tr', {
        class: it === sel ? 'on' : '', tabindex: 0, onclick: () => pick(it.key),
        onkeydown: (e) => { if (e.key === 'Enter') pick(it.key); },
      },
      h('td', null, h('b', null, it.symbol), h('span', { class: 'muted hide-sm' }, ` ${mkName(it.key)}`)),
      h('td', { class: 'r num' }, numL(it.last)),
      h('td', { class: `r num ${ddCls(it.dd_ath)}` }, h('b', null, pct0(it.dd_ath))),
      h('td', { class: 'r num' }, pct0(it.mdd?.depth), h('small', { class: 'muted' }, ` ${it.mdd?.trough_date.slice(0, 4) ?? ''}`)),
      h('td', { class: 'r num hide-sm' }, pct0(it.mdd_10y?.depth), h('small', { class: 'muted' }, ` ${it.mdd_10y?.trough_date.slice(0, 4) ?? ''}`)),
      h('td', { class: 'r num hide-sm' }, it.cur.dd < 0 ? t('mk.ndays', { n: it.cur.days }) : t('mk.at_high'))))))));
}

function mkDetail(it, vix) {
  return h('section', { class: 'card mk-detail', id: 'mk-detail' },
    h('h3', null, h('span', null, mkName(it.key)), h('small', null, `${it.symbol} · ${t('mk.since', { d: it.first_date })}`)),
    mkFacts(it),
    it.key === 'dji' ? h('p', { class: 'muted small' }, t('mk.note.dji')) : null,
    it.group === 'm7' ? h('p', { class: 'muted small' }, t('mk.note.m7')) : null,
    h('div', { class: 'tabs mk-ranges' }, Object.keys(MK_RANGES).map((r) => h('button', {
      class: mk.range === r ? 'on' : '', 'aria-pressed': mk.range === r,
      onclick: (e) => {
        mk.range = r;
        for (const b of e.currentTarget.parentNode.children) { b.classList.toggle('on', b === e.currentTarget); b.setAttribute('aria-pressed', b === e.currentTarget); }
        drawMkChart();
      },
    }, t(`mk.r.${r}`)))),
    h('div', { id: 'mk-chart', class: 'mk-chart', 'data-key': it.key }),
    pendulum(it, vix),
    halfway(it),
    ladderTable(it),
    episodesTable(it),
    h('p', { class: 'muted small' }, t(it.u ? 'mk.def.cn' : 'mk.def', { t: it.t, u: it.u })));
}

function mkFacts(it) {
  const stat = (label, big, sub, cls) => h('div', { class: 'stat' }, h('div', { class: 'l' }, label),
    h('div', { class: `n num ${cls || ''}` }, big), sub ? h('div', { class: 'small muted' }, sub) : null);
  const m = it.mdd, m10 = it.mdd_10y, c = it.cur;
  const rec = (x) => (x.recover_date ? t('mk.recovered', { d: x.recover_date }) : t('mk.not_recovered'));
  return h('div', { class: 'stats' },
    stat(t('mk.s.dd'), pct0(it.dd_ath), t('mk.s.dd_sub', { v: numL(it.ath), d: it.ath_date }), ddCls(it.dd_ath)),
    c.dd !== it.dd_ath ? stat(t('mk.s.cur'), pct0(c.dd), t('mk.s.cur_sub', { v: numL(c.peak), d: c.peak_date }), ddCls(c.dd)) : null,
    m ? stat(t('mk.s.mdd'), pct0(m.depth), `${m.peak_date} → ${m.trough_date} · ${rec(m)}`, 'down') : null,
    m10 ? stat(t('mk.s.mdd10'), pct0(m10.depth), `${m10.peak_date} → ${m10.trough_date} · ${rec(m10)}`, 'down') : null);
}

// ---------- 走势图 + 水下图（回撤）：两张图共用横轴，一根十字线
function niceStep(span, n) {
  const raw = span / n, p = 10 ** Math.floor(Math.log10(raw));
  return [1, 2, 2.5, 5, 10].map((m) => m * p).find((s) => s >= raw) || p * 10;
}
function linTicks(lo, hi, n) {
  const s = niceStep(hi - lo || 1, n), out = [];
  for (let v = Math.ceil(lo / s) * s; v <= hi + 1e-9; v += s) out.push(+v.toFixed(10));
  return out;
}
function logTicks(lo, hi) {
  const out = [];
  for (let e = Math.floor(Math.log10(lo)); e <= Math.ceil(Math.log10(hi)); e++) {
    for (const m of [1, 2, 5]) { const v = m * 10 ** e; if (v >= lo && v <= hi) out.push(v); }
  }
  return out.length > 7 ? out.filter((v) => /^1/.test(String(v))) : out;
}

async function drawMkChart() {
  const box = $('mk-chart');
  if (!box) return;
  const key = box.dataset.key;
  let s = mk.series[key];
  if (!s) {
    fill(box, h('p', { class: 'muted small' }, t('loading')));
    try { s = mk.series[key] = await api(`/api/market/series/${key}`); } catch (e) { fill(box, h('p', { class: 'error' }, e.message)); return; }
    if (!box.isConnected || box.dataset.key !== key) return;
  }
  s.tm = s.tm || s.d.map((d) => Date.parse(d));
  const n = s.tm.length, days = MK_RANGES[mk.range];
  const from = days ? s.tm[n - 1] - days * 864e5 : -Infinity;
  const i0 = Math.max(0, s.tm.findIndex((x) => x >= from));
  const tm = s.tm.slice(i0), cs = s.c.slice(i0), dds = s.dd.slice(i0);
  const W = Math.max(300, box.clientWidth || 680), L = 8, R = 58, T = 10, B = 22, H1 = 230, H2 = 120;
  const log = !days;
  const x = (v) => L + ((v - tm[0]) / (tm[tm.length - 1] - tm[0] || 1)) * (W - L - R);
  let lo = Math.min(...cs), hi = Math.max(...cs);
  const f = log ? Math.log : (v) => v;
  const pad = (f(hi) - f(lo)) * 0.05 || 1;
  const ylo = f(lo) - pad, yhi = f(hi) + pad;
  const y1 = (v) => T + (1 - (f(v) - ylo) / (yhi - ylo)) * (H1 - T - B);
  const ddLo = Math.min(-5, ...dds) * 1.06;
  const y2 = (v) => 6 + (v / ddLo) * (H2 - 6 - B);
  const line = (ys) => ys.map((yy, i) => `${i ? 'L' : 'M'}${x(tm[i]).toFixed(1)},${yy.toFixed(1)}`).join('');

  // 横轴：按年份
  const y0 = new Date(tm[0]).getUTCFullYear(), yN = new Date(tm[tm.length - 1]).getUTCFullYear();
  const span = yN - y0 + 1;
  const ystep = span <= 2 ? 0 : [1, 2, 5, 10, 20].find((st) => span / st <= 7) || 25;
  const xt = [];
  if (ystep) for (let yy = Math.ceil(y0 / ystep) * ystep; yy <= yN; yy += ystep) { const v = Date.UTC(yy, 0, 1); if (v >= tm[0]) xt.push([v, String(yy)]); }
  else for (let k = 0; k <= 4; k++) { const v = tm[0] + (k / 4) * (tm[tm.length - 1] - tm[0]); xt.push([v, new Date(v).toISOString().slice(0, 7)]); }
  const xAxis = (H) => xt.map(([v, lab]) => [
    svg('line', { x1: x(v), x2: x(v), y1: 4, y2: H - B, class: 'mk-grid' }),
    svg('text', { x: x(v), y: H - 6, class: 'ch-axis', 'text-anchor': 'middle' }, lab)]).flat();

  const pTicks = log ? logTicks(lo, hi) : linTicks(lo, hi, 4);
  const cross1 = svg('line', { y1: T, y2: H1 - B, class: 'mk-cross', visibility: 'hidden' });
  const cross2 = svg('line', { y1: 4, y2: H2 - B, class: 'mk-cross', visibility: 'hidden' });
  const dot = svg('circle', { r: 3.5, class: 'ch-dot', visibility: 'hidden' });
  const last = cs[cs.length - 1], lastDd = dds[dds.length - 1];

  const price = svg('svg', { viewBox: `0 0 ${W} ${H1}`, width: W, height: H1, class: 'mk-svg', role: 'img',
    'aria-label': t('mk.aria.price', { n: mkName(key) }) },
  ...xAxis(H1),
  ...pTicks.flatMap((v) => [svg('line', { x1: L, x2: W - R, y1: y1(v), y2: y1(v), class: 'mk-grid' }),
    svg('text', { x: W - R + 6, y: y1(v) + 4, class: 'ch-axis' }, numL(v, v < 10 ? 2 : 0))]),
  svg('path', { d: line(cs.map(y1)), class: 'ch-line' }),
  svg('circle', { cx: x(tm[tm.length - 1]), cy: y1(last), r: 3.5, class: 'ch-dot' }),
  log ? svg('text', { x: L + 2, y: T + 10, class: 'ch-axis' }, t('mk.logscale')) : null,
  cross1, dot);

  const ddTicks = linTicks(ddLo, 0, 3).filter((v) => v < 0);
  const under = svg('svg', { viewBox: `0 0 ${W} ${H2}`, width: W, height: H2, class: 'mk-svg', role: 'img',
    'aria-label': t('mk.aria.dd', { n: mkName(key) }) },
  ...xAxis(H2),
  ...ddTicks.flatMap((v) => [svg('line', { x1: L, x2: W - R, y1: y2(v), y2: y2(v), class: 'mk-grid' }),
    svg('text', { x: W - R + 6, y: y2(v) + 4, class: 'ch-axis' }, `${Math.round(v)}%`)]),
  svg('path', { d: `${line(dds.map(y2))}L${x(tm[tm.length - 1]).toFixed(1)},${y2(0)}L${x(tm[0]).toFixed(1)},${y2(0)}Z`, class: 'mk-dd-area' }),
  svg('path', { d: line(dds.map(y2)), class: 'mk-dd-line' }),
  svg('line', { x1: L, x2: W - R, y1: y2(0), y2: y2(0), class: 'mk-zero' }),
  lastDd < -1 ? svg('line', { x1: L, x2: W - R, y1: y2(lastDd), y2: y2(lastDd), class: 'mk-now' }) : null,
  lastDd < -1 ? svg('text', { x: W - R + 6, y: y2(lastDd) + 4, class: 'ch-lbl mk-now-lbl' }, t('mk.now', { v: lastDd.toFixed(1) })) : null,
  cross2);

  const tip = h('div', { class: 'mk-tip', hidden: true });
  const wrap = h('div', { class: 'mk-plot' }, h('div', { class: 'mk-cap small muted' }, t('mk.cap.price')), price,
    h('div', { class: 'mk-cap small muted' }, t('mk.cap.dd')), under, tip);
  const xs = tm.map(x);
  const move = (e) => {
    const r = price.getBoundingClientRect();
    const px = (e.clientX - r.left) * (W / r.width);
    let a = 0, b = xs.length - 1;
    while (b - a > 1) { const m = (a + b) >> 1; if (xs[m] < px) a = m; else b = m; }
    const i = Math.abs(xs[a] - px) < Math.abs(xs[b] - px) ? a : b;
    for (const c of [cross1, cross2]) { c.setAttribute('x1', xs[i]); c.setAttribute('x2', xs[i]); c.setAttribute('visibility', 'visible'); }
    dot.setAttribute('cx', xs[i]); dot.setAttribute('cy', y1(cs[i])); dot.setAttribute('visibility', 'visible');
    fill(tip, h('b', null, s.d[i0 + i]), h('span', null, numL(cs[i])), h('span', { class: dds[i] < 0 ? 'down' : '' }, t('mk.tip.dd', { v: pct0(dds[i]) })));
    tip.hidden = false;
    const left = (xs[i] / W) * r.width;
    tip.style.left = `${Math.min(Math.max(left, 70), r.width - 70)}px`;
  };
  const leave = () => { tip.hidden = true; for (const c of [cross1, cross2, dot]) c.setAttribute('visibility', 'hidden'); };
  wrap.addEventListener('pointermove', move);
  wrap.addEventListener('pointerdown', move);
  wrap.addEventListener('pointerleave', leave);
  fill(box, wrap);
}

let mkResize = null;
window.addEventListener('resize', () => {
  clearTimeout(mkResize);
  mkResize = setTimeout(() => { if ($('dlg-market').open) drawMkChart(); }, 200);
});

// ---------- 钟摆：每个指标单独看它在历史里排第几，不合成分数
function gauge(label, value, rank, text) {
  const mark = h('span', { class: `g-mark ${rank == null ? '' : rank < 5 ? 'ext' : rank < 20 ? 'warm' : ''}` });
  if (rank != null) mark.style.left = `${Math.min(100, Math.max(0, rank))}%`;
  return h('div', { class: 'gauge' },
    h('div', { class: 'g-top' }, h('span', null, label), h('b', { class: 'num' }, value)),
    h('div', { class: 'g-track' }, h('span', { class: 'g-zone' }), rank == null ? null : mark),
    h('div', { class: 'g-ends small muted' }, h('span', null, t('mk.g.rare')), h('span', null, t('mk.g.common'))),
    h('p', { class: 'small' }, text));
}

function pendulum(it, vix) {
  const p = it.pend || {};
  const rows = [
    p.dd_rank != null
      ? gauge(t('mk.g.dd'), pct0(it.cur.dd), p.dd_rank, t('mk.g.dd_t', { r: p.dd_rank }))
      : gauge(t('mk.g.dd'), pct0(it.cur.dd), null, t('mk.g.dd_none')),
    p.ma200_gap != null ? gauge(t('mk.g.ma'), pct0(p.ma200_gap), p.ma200_rank, t('mk.g.ma_t', { r: p.ma200_rank })) : null,
    vix ? gauge(t('mk.g.vix'), vix.last.toFixed(1), vix.above_rank,
      t('mk.g.vix_t', { r: vix.above_rank, m: vix.median, x: vix.max, d: vix.max_date })) : null,
  ];
  return h('div', { class: 'mk-sec' }, h('h4', null, t('mk.g.title')), h('p', { class: 'muted small' }, t('mk.g.intro')),
    h('div', { class: 'gauges' }, rows));
}

// ---------- 现在是不是半山腰：历史上跌到同样深度的那些轮次，最后跌到了哪儿
function halfway(it) {
  const x = it.here;
  const sec = [h('h4', null, t('mk.h.title'))];
  if (!x) return h('div', { class: 'mk-sec' }, sec, h('p', { class: 'small' }, t('mk.h.shallow', { v: pct0(it.cur.dd), t: it.t / 2 })));
  if (!x.done) return h('div', { class: 'mk-sec' }, sec, h('p', { class: 'small' }, t('mk.h.none', { v: pct0(x.level) })));
  const finals = x.finals || [];
  const minV = Math.min(x.level, ...finals.map((q) => q.depth));
  const lo = Math.floor(minV / 10) * 10 - 5;
  const pos = (v) => `${(v / lo) * 100}%`;
  const strip = h('div', { class: 'strip' });
  for (const q of finals) {
    const d = h('span', { class: 'sdot', title: t('mk.h.dot', { y: q.year, v: pct0(q.depth) }) });
    d.style.left = pos(q.depth);
    strip.append(d);
  }
  const now = h('span', { class: 'snow' }, h('i', null, t('mk.h.now')));
  now.style.left = pos(x.level);
  const med = h('span', { class: 'smed' }, h('i', null, t('mk.h.med')));
  med.style.left = pos(x.final_median);
  strip.append(now, med);
  const ticks = h('div', { class: 'strip-ticks small muted' });
  for (let v = 0; v >= lo; v -= lo < -60 ? 20 : 10) { const s = h('span', null, `${v}%`); s.style.left = pos(v); ticks.append(s); }
  return h('div', { class: 'mk-sec' }, sec,
    h('p', { class: 'small' }, t('mk.h.text', { v: pct0(x.level), n: x.n, done: x.done, fm: pct0(x.final_median), fw: pct0(x.final_worst),
      fur: pct0(x.further_median), furw: pct0(x.further_worst) })),
    h('div', { class: 'strip-wrap' }, strip, ticks),
    x.fwd1y.n ? h('p', { class: 'small' }, t('mk.h.fwd', { n1: x.fwd1y.n, p1: x.fwd1y.pos, m1: pct0(x.fwd1y.median),
      n3: x.fwd3y.n, p3: x.fwd3y.pos ?? '—', m3: pct0(x.fwd3y.median) })) : null);
}

// ---------- 历史分档：每一档第一次跌到时，之后发生了什么
function ladderTable(it) {
  const fwd = (f) => (f.n ? [h('span', { class: signCls(f.median) }, pct0(f.median)), h('small', { class: 'muted' }, ` ${t('mk.l.up', { p: f.pos, n: f.n })}`)] : '—');
  return h('div', { class: 'mk-sec' }, h('h4', null, t('mk.l.title')), h('p', { class: 'muted small' }, t('mk.l.intro')),
    h('div', { class: 'table-wrap flat' }, h('table', { class: 'mini mk-ladder' },
      h('thead', null, h('tr', null, ['lv', 'n', 'stop', 'further', 'final', 'f1', 'f3'].map((k) => h('th', {
        class: k === 'lv' ? '' : (k === 'stop' || k === 'final' ? 'r hide-sm' : 'r'),
      }, t(`mk.l.c.${k}`))))),
      h('tbody', null, it.ladder.map((r) => h('tr', { class: it.cur.dd <= r.level ? 'hit' : (r.n ? '' : 'thin') },
        h('td', { class: 'num nowrap' }, h('b', null, pct0(r.level)), it.cur.dd <= r.level ? h('small', null, ` ${t('mk.l.reached')}`) : null),
        h('td', { class: 'r num' }, r.n, r.ongoing ? h('small', { class: 'muted' }, ` ${t('mk.l.incl_now')}`) : null),
        h('td', { class: 'r num hide-sm' }, r.stopped_before_next == null || !r.done ? '—' : `${r.stopped_before_next}/${r.done}`),
        h('td', { class: 'r num' }, r.further_median == null ? '—' : [pct0(r.further_median), h('small', { class: 'muted' }, ` / ${pct0(r.further_worst)}`)]),
        h('td', { class: 'r num hide-sm' }, pct0(r.final_median)),
        h('td', { class: 'r num nowrap' }, fwd(r.fwd1y)),
        h('td', { class: 'r num nowrap' }, fwd(r.fwd3y))))))),
    h('p', { class: 'muted small' }, t('mk.l.howto')));
}

function episodesTable(it) {
  const eps = it.episodes || [];
  if (!eps.length) return null;
  const shown = mk.showAllEp ? eps : eps.slice(0, 10);
  return h('div', { class: 'mk-sec' }, h('h4', null, t('mk.e.title', { t: it.t, n: eps.length })),
    h('div', { class: 'table-wrap flat' }, h('table', { class: 'mini mk-eps' },
      h('thead', null, h('tr', null, ['peak', 'trough', 'depth', 'days', 'rec'].map((k) => h('th', { class: k === 'depth' ? 'r' : k === 'days' ? 'r hide-sm' : '' }, t(`mk.e.c.${k}`))))),
      h('tbody', null, shown.map((e) => h('tr', { class: e.ongoing ? 'hit' : '' },
        h('td', { class: 'num nowrap' }, e.peak_date),
        h('td', { class: 'num nowrap' }, e.trough_date, e.ongoing ? h('small', { class: 'muted' }, ` ${t('mk.e.sofar')}`) : null),
        h('td', { class: `r num ${ddCls(e.depth)}` }, h('b', null, pct0(e.depth))),
        h('td', { class: 'r num hide-sm' }, t('mk.ndays', { n: e.days_down })),
        h('td', { class: 'num nowrap' }, e.recover_date || (e.ongoing ? t('mk.e.ongoing') : t('mk.not_recovered')),
          e.how === 'rebound' ? h('small', { class: 'muted' }, ` ${t('mk.e.rebound', { u: it.u })}`) : null)))))),
    eps.length > 10 ? h('button', { class: 'plain small', onclick: () => { mk.showAllEp = !mk.showAllEp; drawMarket(); } },
      t(mk.showAllEp ? 'mk.e.less' : 'mk.e.all', { n: eps.length })) : null);
}

// ---------------------------------------------------------------- 数据加载 / 总渲染
function render() {
  renderFreshness(); renderBanner(); renderSummary(); renderChanges(); renderTable();
}

async function loadLatest() {
  state.viewingHistory = null;
  state.scan = (await api('/api/scan/latest?slim=1')).scan;
  state.details = {};
  state.ai = state.scan && state.config.ai.enabled ? (await api('/api/ai').catch(() => ({ items: {} }))).items : {};
  render();
}

async function loadHistory(name) {
  state.scan = (await api('/api/history/' + encodeURIComponent(name))).scan;
  state.viewingHistory = name;
  state.details = {};
  render();
}

/** 切换语言：界面文字立即切换，结论等后端文字重新按新语言请求 */
async function switchLang() {
  LANG = LANG === 'zh' ? 'en' : 'zh';
  store.set('gp_lang', LANG);
  applyStaticText();
  state.config = await api('/api/config');
  renderMethod();
  const reopen = !$('drawer').hidden && $('drawer').dataset.ticker;
  if (state.viewingHistory) await loadHistory(state.viewingHistory); else await loadLatest();
  if (reopen) openDrawer(reopen);
}

async function init() {
  applyStaticText();
  state.config = await api('/api/config');
  renderMethod(); setupScanDialog();
  $('btn-lang').addEventListener('click', switchLang);
  $('btn-history').addEventListener('click', openHistory);
  $('btn-track').addEventListener('click', openTrack);
  $('btn-market').addEventListener('click', openMarket);
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
  $('freshness').textContent = t('conn.error', { e: e.message });
  $('freshness').classList.add('stale');
});
