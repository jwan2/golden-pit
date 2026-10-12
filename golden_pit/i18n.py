"""国际化：存储与语言无关，输出时再渲染。

- 代码里用 M("key", **params) 产生一条"消息"，形如 {"$m": "key", "p": {...}}，可以安全地存进 JSON
- 固定的双语文本（如知识库）用 T(zh, en) → {"$t": {"zh": ..., "en": ...}}
- render(obj, lang) 递归地把所有消息渲染成目标语言的字符串；普通字符串原样保留
- 参数本身也可以是消息或消息列表（列表会按语言用合适的分隔符连接）

新增文字时只需要在 CATALOG 里加一条，测试会检查中英文是否齐全、占位符是否一致。
"""
from __future__ import annotations

import locale
import os
import string
from typing import Any

LANGS = ("zh", "en")
DEFAULT_LANG = "en"


def M(key: str, **params: Any) -> dict:
    return {"$m": key, "p": params} if params else {"$m": key}


def T(zh: str, en: str) -> dict:
    return {"$t": {"zh": zh, "en": en}}


def normalize_lang(lang: str | None) -> str:
    if not lang:
        return DEFAULT_LANG
    lang = lang.lower().replace("_", "-")
    return "zh" if lang.startswith("zh") else "en" if lang.startswith("en") else DEFAULT_LANG


def env_lang() -> str:
    """命令行 / MCP 的默认语言：GOLDEN_PIT_LANG > 系统区域设置 > 英文。"""
    explicit = os.environ.get("GOLDEN_PIT_LANG")
    if explicit:
        return normalize_lang(explicit)
    try:
        loc = locale.getlocale()[0] or ""
    except ValueError:
        loc = ""
    return "zh" if loc.lower().startswith(("zh", "chinese")) else "en"


class _Keep(dict):
    def __missing__(self, k):  # 缺参数时保留占位符，绝不抛异常
        return "{" + k + "}"


SEP = {"zh": "、", "en": ", "}


def render(obj: Any, lang: str) -> Any:
    if isinstance(obj, dict):
        if "$m" in obj:
            entry = CATALOG.get(obj["$m"])
            if entry is None:
                return obj["$m"]
            tpl = entry[0] if lang == "zh" else entry[1]
            params = {k: _param(v, lang) for k, v in (obj.get("p") or {}).items()}
            return string.Formatter().vformat(tpl, (), _Keep(params))
        if "$t" in obj:
            t = obj["$t"]
            return t.get(lang) or t.get("en") or t.get("zh") or ""
        return {k: render(v, lang) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [render(x, lang) for x in obj]
    return obj


def _param(v: Any, lang: str) -> Any:
    v = render(v, lang)
    if isinstance(v, list):
        return SEP[lang].join(str(x) for x in v)
    return v


class MsgError(Exception):
    """携带 i18n 消息的异常。str(e) 是英文（用于日志），.msg 用于按语言展示。"""

    def __init__(self, msg: Any):
        self.msg = msg
        super().__init__(render(msg, "en") if isinstance(msg, dict) else str(msg))


def msg_of(e: BaseException) -> Any:
    return getattr(e, "msg", None) or str(e)


def tr(key: str, lang: str, **params: Any) -> str:
    return render(M(key, **params), lang)


# key: (中文, English)
CATALOG: dict[str, tuple[str, str]] = {
    # ---------------------------------------------------------- 指标名
    "chk.interest_coverage": ("利息保障倍数", "Interest coverage"),
    "chk.current_ratio": ("流动比率", "Current ratio"),
    "chk.net_debt_ebitda": ("净负债 / EBITDA", "Net debt / EBITDA"),
    "chk.fcf": ("自由现金流", "Free cash flow"),
    "chk.altman_z": ("Altman Z-Score", "Altman Z-Score"),
    "chk.share_change": ("总股本变化", "Share count change"),
    "chk.buyback_yield": ("净回购收益率", "Net buyback yield"),
    "chk.capex_revenue": ("资本开支 / 营收", "Capex / revenue"),
    "chk.drop": ("距 52 周高点回撤", "Drawdown from 52-week high"),
    "chk.pb": ("市净率 P/B", "Price / book"),
    "chk.forward_pe": ("前瞻市盈率", "Forward P/E"),
    # ---------------------------------------------------------- 规则 / 显示值
    "rule.share_reduction": ("净减少 ≥ {v}", "net decrease ≥ {v}"),
    "rule.buyback": ("净回购 ≥ {v} 市值", "net buybacks ≥ {v} of market cap"),
    "rule.capex": ("≤ {v} 营收", "≤ {v} of revenue"),
    "disp.no_debt": ("无债务", "no debt"),
    "disp.no_interest": ("无利息", "no interest"),
    "disp.net_cash": ("净现金", "net cash"),
    "disp.na": ("不适用", "n/a"),
    "disp.share_change": ("{chg}（{years}年）", "{chg} ({years} yrs)"),
    # ---------------------------------------------------------- 指标备注
    "n.no_ebit": ("缺少 EBIT", "EBIT missing"),
    "n.no_debt": ("无有息债务", "No interest-bearing debt"),
    "n.interest_undisclosed": ("数据源未披露利息支出，不能当作无债务",
                               "Interest expense not reported — not treated as debt-free"),
    "n.zero_interest": ("利息支出为 0", "Interest expense is 0"),
    "n.ebit_negative": ("EBIT 为负", "EBIT is negative"),
    "n.from_balance": ("由资产负债表计算", "Computed from the balance sheet"),
    "n.no_current": ("缺少流动资产/负债", "Current assets / liabilities missing"),
    "n.no_debt_cash": ("缺少总债务或现金数据", "Total debt or cash missing"),
    "n.net_cash": ("现金 {cash} ≥ 债务 {debt}", "Cash {cash} ≥ debt {debt}"),
    "n.no_ebitda": ("净负债 {net}，但缺少 EBITDA", "Net debt {net}, but EBITDA missing"),
    "n.ebitda_nonpos": ("净负债 {net} 而 EBITDA 为 {ebitda}", "Net debt {net} while EBITDA is {ebitda}"),
    "n.fcf_annual": ("取自最新年报", "From the latest annual report"),
    "n.fcf_derived": ("经营现金流 + 资本开支（年报）", "Operating cash flow + capex (annual report)"),
    "n.no_cashflow": ("缺少现金流数据", "Cash-flow data missing"),
    "n.missing_fields": ("缺少：{fields}", "Missing: {fields}"),
    "n.bad_assets": ("总资产/总负债无效", "Invalid total assets / liabilities"),
    "n.altman_classic": ("经典制造业版本；轻资产公司留存收益偏低会被低估",
                         "Classic manufacturing model; understates asset-light firms with low retained earnings"),
    "n.currency_mismatch": ("财报币种 {fin} ≠ 报价币种 {quote}，市值与财报金额不可直接相比",
                            "Statements in {fin} but quoted in {quote} — market cap and statement figures aren't comparable"),
    "n.shares_history": ("历史股本数据不足（需要至少 2 个年报期）",
                         "Not enough share-count history (needs ≥2 annual periods)"),
    "n.shares_window": ("可比期间不足 1.5 年", "Comparable window shorter than 1.5 years"),
    "n.split": ("窗口内发生拆/并股（{date}，×{ratio}），股本未必复权，无法可靠判断（未复权变化 {chg}）",
                "Split in window ({date}, ×{ratio}); share counts may not be adjusted, so this can't be judged reliably (unadjusted change {chg})"),
    "n.no_cf_statement": ("缺少现金流量表", "Cash-flow statement missing"),
    "n.no_mcap": ("缺少市值", "Market cap missing"),
    "n.buyback_net": ("回购减去发行（含股权激励发行）", "Repurchases minus issuance (incl. stock-based compensation)"),
    "n.no_buyback": ("现金流表未披露回购", "No repurchases reported"),
    "n.no_capex": ("缺少资本开支或营收", "Capex or revenue missing"),
    "n.no_price_high": ("缺少价格或 52 周高点", "Price or 52-week high missing"),
    "n.required": ("必需条件", "Required"),
    "n.no_data": ("缺少数据", "No data"),
    "n.neg_book": ("净资产为负，P/B 无意义（常见于大额回购）",
                   "Negative book equity — P/B is meaningless (common after heavy buybacks)"),
    "n.expected_loss": ("预期亏损", "Expected loss"),
    "n.financial_na": ("金融业的杠杆/流动性指标没有可比意义", "Leverage / liquidity metrics aren't comparable for financials"),
    "f.total_assets": ("总资产", "total assets"),
    "f.current_assets": ("流动资产", "current assets"),
    "f.current_liabilities": ("流动负债", "current liabilities"),
    "f.retained_earnings": ("留存收益", "retained earnings"),
    "f.total_liabilities": ("总负债", "total liabilities"),
    "f.ebit": ("EBIT", "EBIT"),
    "f.revenue": ("营收", "revenue"),
    "f.market_cap": ("市值", "market cap"),
    # ---------------------------------------------------------- 警告
    "w.stale": ("最新年报是 {date}，已过期 {months} 个月，财务指标可能不反映现状",
                "Latest annual report is from {date} ({months} months old); financials may be outdated"),
    "w.no_balance": ("没有取到任何资产负债表", "No balance sheet available"),
    "w.currency_mismatch": ("{msg}；Altman Z、回购收益率已标为未知，其余金额以财报币种显示",
                            "{msg}. Altman Z and buyback yield are marked unknown; other amounts are in the statement currency"),
    "w.non_usd": ("报价币种为 {ccy}，非美元标的", "Quoted in {ccy}, not USD"),
    # ---------------------------------------------------------- 档位
    "tier.golden_pit": ("黄金坑", "Golden pit"),
    "tier.golden_pit.desc": ("质量过关，且价格确实在坑里", "Quality passes and the price is genuinely in a pit"),
    "tier.quality_wait": ("优质 · 待价", "Quality · wait"),
    "tier.quality_wait.desc": ("公司质量过关，但价格还没到坑位", "Quality passes, but the price isn't in a pit yet"),
    "tier.value_trap": ("坑里 · 质量不足", "Pit · weak"),
    "tier.value_trap.desc": ("价格够便宜，但财务或管理层没过关（价值陷阱风险）",
                             "Cheap enough, but financials or management fail (value-trap risk)"),
    "tier.fail": ("未通过", "Fail"),
    "tier.fail.desc": ("质量和坑位都不满足", "Neither the quality gate nor the pit condition is met"),
    "tier.insufficient": ("数据不足", "Insufficient data"),
    "tier.insufficient.desc": ("关键数据缺失过多，不下结论", "Too much key data missing to conclude"),
    "tier.n_a": ("不适用", "Not applicable"),
    "tier.n_a.desc": ("金融业等不适用本框架的公司", "Financials and others this framework doesn't fit"),
    "tier.error": ("抓取失败", "Fetch error"),
    "tier.error.desc": ("数据源没有返回有效数据", "The data source returned nothing usable"),
    # ---------------------------------------------------------- 结论理由
    "r.fail": ("✗ {label} {display}（要求 {rule}）", "✗ {label} {display} (needs {rule})"),
    "r.fail_note": ("✗ {label} {display}（要求 {rule}）— {note}", "✗ {label} {display} (needs {rule}) — {note}"),
    "r.unknown": ("? {label} 无法判断 — {note}", "? {label} can't be judged — {note}"),
    "r.na_financial": ("金融业公司的利息保障、流动比率、净负债等指标没有可比意义，不纳入评判。",
                       "Interest coverage, current ratio and net debt aren't comparable for financial companies, so they aren't judged."),
    "r.insufficient": ("财务+管理层 8 项中有 {n} 项数据缺失（上限 {max}），不下结论。",
                       "{n} of the 8 financial + management checks lack data (limit {max}), so there's no verdict."),
    "r.no_drop": ("? 缺少价格或 52 周高点，无法判断是否在坑里", "? Price or 52-week high missing — can't tell whether it's in a pit"),
    "r.quality": ("质量：财务 {fp}/{ft}（要求 ≥{fmin}），管理层 {mp}/3（要求 ≥{mmin}） → {res}",
                  "Quality: financial {fp}/{ft} (needs ≥{fmin}), management {mp}/3 (needs ≥{mmin}) → {res}"),
    "r.quality_ok": ("过关", "passes"),
    "r.quality_no": ("未过关", "fails"),
    "r.pit": ("坑位：回撤 {drop}（要求 ≥{min}%），估值锚点 {a}/2 合格（要求 ≥{amin}） → {res}",
              "Pit: drawdown {drop} (needs ≥{min}%), valuation anchors {a}/2 (needs ≥{amin}) → {res}"),
    "r.pit_in": ("在坑里", "in a pit"),
    "r.pit_out": ("不在坑里", "not in a pit"),
    # ---------------------------------------------------------- 走势
    "tm.falling": ("下跌中", "Falling"),
    "tm.basing": ("筑底中", "Basing"),
    "tm.rebounding": ("企稳反弹", "Rebounding"),
    "tm.unclear": ("方向不明", "Unclear"),
    "tm.unknown": ("数据不足", "No data"),
    "tm.r.history": ("价格历史不足 {n} 个交易日", "Less than {n} trading days of price history"),
    "tm.r.falling": ("{d} 个交易日前刚创一年新低，价格在 MA20 下方且均线仍在下行",
                     "Hit a 1-year low {d} trading days ago; price is below a falling MA20"),
    "tm.r.rebounding": ("低点（{date}）后 {d} 个交易日未再创新低，已站上拐头向上的 MA20，距低点 +{above}%",
                        "No new low for {d} trading days since {date}; above a rising MA20, +{above}% off the low"),
    "tm.r.basing": ("价格贴近一年低点（+{above}%），MA20 5 日变化 {slope}%，在底部横盘",
                    "Near the 1-year low (+{above}%), MA20 5-day change {slope}%, moving sideways at the bottom"),
    "tm.r.basing_held": ("价格贴近一年低点（+{above}%），MA20 5 日变化 {slope}%，在底部横盘，且 {d} 个交易日未破底",
                         "Near the 1-year low (+{above}%), MA20 5-day change {slope}%, moving sideways with no new low for {d} trading days"),
    "tm.r.unclear": ("距一年低点 +{above}%，MA20 5 日变化 {slope}%，没有清晰的下跌或企稳特征",
                     "+{above}% off the 1-year low, MA20 5-day change {slope}%; no clear falling or basing pattern"),
    # ---------------------------------------------------------- 买入计划
    "plan.inv.quality": ("下一次扫描质量门槛不再过关（财务或管理层项目转为未通过）→ 暂停加仓，重新评估",
                         "The quality gate fails on a later scan (financial or management checks turn to fail) → stop adding and re-evaluate"),
    "plan.inv.data": ("出现无法解释的新增“未知”项或数据警告 → 先核实数据再行动",
                      "New unexplained 'unknown' checks or data warnings appear → verify the data before acting"),
    "plan.inv.structural": ("坑的成因从周期性/情绪性变为结构性衰退 → 这不再是黄金坑",
                            "The cause of the pit turns from cyclical / sentiment to structural decline → it's no longer a golden pit"),
    # ---------------------------------------------------------- 战绩
    "track.caveat": ("前向追踪，价格收益不含股息；追踪满 {days} 天才计入统计；样本少、时间短时结论没有统计意义（建议至少 30 个样本、持有 90 天以上再看）。",
                     "Forward tracking of price returns, dividends excluded. Entries count toward stats after {days} days. Small or young samples aren't statistically meaningful — wait for 30+ samples held 90+ days."),
    # ---------------------------------------------------------- 候选池 / 扫描状态
    "u.custom": ("自定义代码", "Custom tickers"),
    "u.curated": ("核心池：知识库中的 {n} 只标的", "Curated pool: {n} tickers from the knowledge base"),
    "u.live": ("Yahoo 实时筛选：{n} 只", "Yahoo live screener: {n} tickers"),
    "u.live_partial": ("Yahoo 实时筛选：{n} 只（部分查询失败：{errors}）", "Yahoo live screener: {n} tickers (some queries failed: {errors})"),
    "u.live_empty": ("Yahoo 实时筛选器没有返回任何标的", "The Yahoo live screener returned no tickers"),
    "u.query_failed": ("{label}查询失败：{error}", "{label} query failed: {error}"),
    "u.q.base": ("大中盘回撤", "large/mid-cap drawdown"),
    "u.q.deep": ("深度回撤+低P/B", "deep drawdown + low P/B"),
    "u.no_tickers": ("没有有效的股票代码", "No valid tickers"),
    "u.sp500": ("S&P 500：{n} 只（名单日期 {date}）", "S&P 500: {n} tickers (list as of {date})"),
    "u.combined": ("S&P 500 + 实时筛选：{n} 只（其中 {extra} 只不在 S&P 500）",
                   "S&P 500 + live screener: {n} tickers ({extra} outside the S&P 500)"),
    "u.combined_partial": ("S&P 500：{n} 只（实时筛选失败，本次未合并）",
                           "S&P 500: {n} tickers (live screener failed, not merged this time)"),
    "u.sp500_bad": ("成分股名单异常（只解析到 {n} 只），未更新", "Constituent list looks wrong (only {n} parsed); not updated"),
    "s.universe": ("正在确定候选池…", "Building the candidate list…"),
    "s.analyzing": ("正在分析 {n} 只标的…", "Analyzing {n} tickers…"),
    "s.retrying": ("正在补扫 {n} 只抓取失败的标的（避开限流）…", "Retrying {n} tickers that failed to fetch (backing off from rate limits)…"),
    "s.tracking": ("正在更新战绩追踪…", "Updating the track record…"),
    "s.done": ("完成：{n} 只，其中黄金坑 {g} 只", "Done: {n} tickers, {g} golden pits"),
    "s.done_failed": ("完成：{n} 只，其中黄金坑 {g} 只，{f} 只抓取失败", "Done: {n} tickers, {g} golden pits, {f} fetch errors"),
    "s.failed": ("扫描失败", "Scan failed"),
    "s.mostly_failed": ("只有 {ok}/{n} 只抓取成功，整次扫描作废，上一次结果保持不变。首个错误：{first}",
                        "Only {ok}/{n} tickers fetched; this scan was discarded and the previous results were kept. First error: {first}"),
    "s.track_failed": ("战绩追踪更新失败：{e}", "Track-record update failed: {e}"),
    # ---------------------------------------------------------- 错误
    "err.no_quote": ("{t}：没有行情数据（代码无效、已退市或停牌）", "{t}: no quote data (invalid, delisted or halted)"),
    "err.fetch": ("{t}：抓取失败（{e}）", "{t}: fetch failed ({e})"),
    "err.analyze": ("{t}：分析出错（{e}）", "{t}: analysis error ({e})"),
    "err.timeout": ("{t}：扫描超时（>{m} 分钟）", "{t}: scan timed out (>{m} min)"),
    "err.bad_ticker": ("无效的股票代码：{t}", "Invalid ticker: {t}"),
    "e.admin_token": ("需要管理员令牌（X-Admin-Token）", "Admin token required (X-Admin-Token)"),
    "e.local_only": ("服务器未配置 GOLDEN_PIT_ADMIN_TOKEN，仅允许本机操作",
                     "GOLDEN_PIT_ADMIN_TOKEN isn't set on the server; only localhost may do this"),
    "e.mode": ("mode 必须是 live / curated / sp500 / combined / custom", "mode must be live, curated, sp500, combined or custom"),
    "e.no_custom": ("没有识别到有效的美股代码（如 AAPL, BRK-B）", "No valid US tickers found (e.g. AAPL, BRK-B)"),
    "e.busy": ("已有扫描正在进行", "A scan is already running"),
    "e.bad_ticker": ("无效的股票代码", "Invalid ticker"),
    "e.not_in_scan": ("最新扫描里没有这只股票，请先扫描", "This stock isn't in the latest scan — scan it first"),
    # ---------------------------------------------------------- AI
    "ai.pit.cyclical": ("周期性低谷", "Cyclical trough"),
    "ai.pit.structural": ("结构性衰退风险", "Structural decline risk"),
    "ai.pit.event": ("事件/一次性冲击", "One-off event"),
    "ai.pit.sentiment": ("情绪/估值杀跌", "Sentiment / de-rating"),
    "ai.pit.unclear": ("原因不明", "Unclear cause"),
    "ai.agree.agree": ("同意量化结论", "Agrees with the quant verdict"),
    "ai.agree.partial": ("部分同意", "Partly agrees"),
    "ai.agree.disagree": ("不同意量化结论", "Disagrees with the quant verdict"),
    "ai.conf.low": ("低", "low"),
    "ai.conf.medium": ("中", "medium"),
    "ai.conf.high": ("高", "high"),
    "ai.e.disabled": ("未启用 AI（设置 GOLDEN_PIT_AI_PROVIDER=cli 或 openai）",
                      "AI is not enabled (set GOLDEN_PIT_AI_PROVIDER=cli or openai)"),
    "ai.e.no_data": ("该标的没有可用的量化数据，无法研判", "No usable quantitative data for this stock"),
    "ai.e.no_json": ("模型没有返回 JSON", "The model didn't return JSON"),
    "ai.e.bad_json": ("模型返回的 JSON 无法解析：{e}", "The model's JSON couldn't be parsed: {e}"),
    "ai.e.not_object": ("模型返回的不是 JSON 对象", "The model didn't return a JSON object"),
    "ai.e.bad_enum": ("模型返回的枚举字段不合法", "The model returned invalid enum values"),
    "ai.e.incomplete": ("模型返回的内容不完整（缺少多头逻辑或空头论点）", "The model's answer is incomplete (missing thesis or bear case)"),
    "ai.e.no_cli": ("没有找到 Command Code CLI（commandcode）。请先安装并登录，或改用 openai 提供商。",
                    "Command Code CLI (commandcode) not found. Install and log in, or use the openai provider."),
    "ai.e.timeout": ("模型响应超时（>{s}s）", "The model timed out (>{s}s)"),
    "ai.e.cli_start": ("无法启动 Command Code CLI：{e}", "Couldn't start Command Code CLI: {e}"),
    "ai.e.region": ("该模型受地域限制，请换一个模型（GOLDEN_PIT_AI_MODEL）", "This model is region-restricted; pick another (GOLDEN_PIT_AI_MODEL)"),
    "ai.e.cli": ("CLI 返回错误：{e}", "CLI error: {e}"),
    "ai.e.no_key": ("未配置 GOLDEN_PIT_AI_KEY", "GOLDEN_PIT_AI_KEY is not set"),
    "ai.e.https": ("GOLDEN_PIT_AI_BASE_URL 必须是 https:// 地址", "GOLDEN_PIT_AI_BASE_URL must be an https:// URL"),
    "ai.e.http": ("接口返回 HTTP {code}", "The API returned HTTP {code}"),
    "ai.e.call": ("调用失败：{e}", "Call failed: {e}"),
    "ai.e.ticker": ("无效的股票代码", "Invalid ticker"),
    "moat.source": ("人工知识库（行业级观点，非公司级结论，不计入判定）",
                    "Human-written knowledge base: an industry-level view, not a company verdict; not scored"),
    # ---------------------------------------------------------- 命令行
    "cli.desc": ("美股黄金坑助手：质量门槛 × 坑位条件，缺失数据绝不当作通过。",
                 "US golden-pit assistant: quality gate x pit condition. Missing data never counts as a pass."),
    "cli.h.lang": ("输出语言", "Output language"),
    "cli.h.serve": ("启动网页（默认 http://127.0.0.1:5000）", "Start the web app (default http://127.0.0.1:5000)"),
    "cli.h.scan": ("运行一次扫描并保存结果", "Run a scan and save the results"),
    "cli.h.tickers": ("自定义代码（给了代码就是 custom 模式）", "Custom tickers (implies custom mode)"),
    "cli.h.json": ("输出 JSON", "Output JSON"),
    "cli.h.analyze": ("即时分析单只股票（不保存）", "Analyze one stock now (not saved)"),
    "cli.h.track": ("查看战绩追踪（前向验证）", "Show the forward track record"),
    "cli.h.universe": ("管理候选池名单", "Manage universe lists"),
    "cli.h.universe_update": ("从维基百科更新 S&P 500 成分股名单", "Refresh the S&P 500 constituent list from Wikipedia"),
    "cli.universe_updated": ("已更新 S&P 500 名单：{n} 只，日期 {date}，保存在 {path}",
                             "S&P 500 list updated: {n} tickers as of {date}, saved to {path}"),
    "cli.h.mcp": ("以 MCP server 方式运行（stdio），供 Claude Code 等 AI 客户端调用",
                  "Run as an MCP server (stdio) for Claude Code and other AI clients"),
    "cli.col.ticker": ("代码", "Ticker"), "cli.col.verdict": ("结论", "Verdict"), "cli.col.price": ("现价", "Price"),
    "cli.col.drop": ("回撤", "Drop"), "cli.col.fin": ("财务", "Financial"), "cli.col.mgmt": ("管理层", "Management"),
    "cli.col.unknown": ("未知", "Unknown"), "cli.col.timing": ("走势", "Timing"), "cli.col.company": ("公司", "Company"),
    "cli.scanning": ("扫描中（{mode}）… 首次运行需要抓取数据，之后有缓存会快很多",
                     "Scanning ({mode})… the first run fetches data; later runs use the cache and are much faster"),
    "cli.done": ("{note} · 完成于 {time}", "{note} · finished {time}"),
    "cli.golden": ("黄金坑 {n} 只。详情：golden-pit analyze <代码>，或 golden-pit serve 打开网页。",
                   "{n} golden pits. Details: golden-pit analyze <TICKER>, or golden-pit serve for the web app."),
    "cli.price_line": ("现价 {price} {ccy} · 距 52 周高点回撤 {drop}% · 走势：{timing}",
                       "Price {price} {ccy} · {drop}% below 52-week high · timing: {timing}"),
    "cli.why": ("为什么：", "Why:"),
    "cli.timing": ("走势：{r}", "Timing: {r}"),
    "cli.checks": ("指标：", "Checks:"),
    "cli.needs": ("要求", "needs"),
    "cli.plan": ("分批价位（参考模板，非建议）：", "Staged entry levels (a template, not advice):"),
    "cli.weight": ("占比", "weight"),
    "cli.reached": ("✓ 已到达", "✓ reached"),
    "cli.moat": ("行业观点（人工知识库，不计分）：{v}", "Industry view (human knowledge base, not scored): {v}"),
    "cli.track_head": ("追踪 {total} 条（仍在坑里 {open}），满 90 天的 {mature} 条。基准 {bench}。",
                       "{total} tracked ({open} still in a pit), {mature} older than 90 days. Benchmark {bench}."),
    "cli.stat": ("  {label}：均值 {mean}  中位数 {median}  胜率 {win}%  (n={n})",
                 "  {label}: mean {mean}  median {median}  win rate {win}%  (n={n})"),
    "cli.stat.return_since": ("入选至今收益", "Return since flagged"),
    "cli.stat.excess_since": ("入选至今超额", "Excess since flagged"),
    "cli.stat.excess_during": ("入选期间超额", "Excess while flagged"),
    "cli.serve": ("Golden Pit {v} → http://{host}:{port}  数据目录 {dir}（管理员令牌：{tok}）",
                  "Golden Pit {v} → http://{host}:{port}  data dir {dir} (admin token: {tok})"),
    "cli.tok_set": ("已配置", "set"),
    "cli.tok_unset": ("未配置，仅本机可操作", "not set — localhost only"),
    # ---------------------------------------------------------- 导出
    "x.csv.ticker": ("代码", "Ticker"), "x.csv.name": ("公司", "Company"), "x.csv.verdict": ("结论", "Verdict"),
    "x.csv.price": ("现价", "Price"), "x.csv.drop": ("回撤%", "Drawdown %"), "x.csv.pb": ("P/B", "P/B"),
    "x.csv.fpe": ("前瞻PE", "Forward P/E"), "x.csv.fin": ("财务通过", "Financial pass"),
    "x.csv.mgmt": ("管理层通过", "Management pass"), "x.csv.unknown": ("未知项", "Unknown checks"),
    "x.csv.timing": ("走势", "Timing"), "x.csv.industry": ("行业", "Industry"), "x.csv.time": ("扫描时间", "Scanned at"),
    "x.md.title": ("黄金坑扫描 {time}", "Golden Pit scan {time}"),
    "x.md.universe": ("候选池：{note}　|　标的 {n} 只", "Universe: {note} | {n} tickers"),
    "x.md.header": ("| 代码 | 公司 | 现价 | 回撤 | 财务 | 管理层 | 未知项 | 走势 |",
                    "| Ticker | Company | Price | Drawdown | Financial | Management | Unknown | Timing |"),
    "x.md.disclaimer": ("> 本工具仅为筛选辅助，不构成投资建议。", "> A research aid only — not investment advice."),
}
