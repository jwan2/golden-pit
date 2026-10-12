// CI check for the web UI dictionary: both languages complete, placeholders match,
// every key used in app.js / index.html exists, and no Chinese in English strings.
import { readFileSync } from 'node:fs';
import { DICT } from '../golden_pit/web/i18n.js';

const fail = (msg) => { console.error('✗ ' + msg); process.exitCode = 1; };
const zh = Object.keys(DICT.zh), en = Object.keys(DICT.en);
const ph = (s) => (s.match(/\{(\w+)\}/g) || []).sort().join();
const CJK = /[一-鿿]/;

for (const k of zh) if (!(k in DICT.en)) fail(`missing en: ${k}`);
for (const k of en) if (!(k in DICT.zh)) fail(`missing zh: ${k}`);
for (const k of zh) if (k in DICT.en && ph(DICT.zh[k]) !== ph(DICT.en[k])) fail(`placeholder mismatch: ${k}`);
for (const k of en) if (!k.startsWith('lang.switch') && CJK.test(DICT.en[k])) fail(`CJK in en: ${k}`);

const src = readFileSync(new URL('../golden_pit/web/app.js', import.meta.url), 'utf8')
  + readFileSync(new URL('../golden_pit/web/index.html', import.meta.url), 'utf8');
const used = new Set([
  ...[...src.matchAll(/\bt\('([a-z0-9_]+\.[a-z0-9_.]+|[a-z_]+)'/g)].map((m) => m[1]),
  ...[...src.matchAll(/data-i18n(?:-ph|-aria)?="([a-z0-9_.]+)"/g)].map((m) => m[1]),
]);
used.delete('key');   // JSDoc example
for (const k of used) if (!(k in DICT.en)) fail(`used but undefined: ${k}`);

if (!process.exitCode) console.log(`✓ web i18n OK (${zh.length} keys, ${used.size} used)`);
