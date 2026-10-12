"""AI 研判（可选）。

定位：给"已经量化判定过的股票"写一份**观点**——这个坑是周期性的还是结构性的、
空头会怎么说、催化剂是什么、还有哪些要自己核实。它**不参与判定**，只作为参考展示。

安全与可信度设计：
- 模型只能看到我们算好的快照（指标值、状态、阈值），提示词要求不得编造快照之外的数字
- 输出必须是 JSON，并逐字段校验、截断长度；校验失败 = 失败，不展示半成品
- 缓存里记录快照哈希：数据变了，旧研判会被标为"已过期"
- 不使用 shell，不拼接命令字符串；API 密钥只来自环境变量，不写进代码
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

PIT_TYPES = {"cyclical": "周期性低谷", "structural": "结构性衰退风险", "event": "事件/一次性冲击",
             "sentiment": "情绪/估值杀跌", "unclear": "原因不明"}
AGREE = {"agree": "同意量化结论", "partial": "部分同意", "disagree": "不同意量化结论"}
CONFIDENCE = {"low": "低", "medium": "中", "high": "高"}
TICKER_RE = re.compile(r"^[A-Z]{1,5}(-[A-Z])?$")
_CLI_SLOTS = threading.Semaphore(2)


class AIError(Exception):
    """AI 调用失败；消息可直接展示给用户。"""


@dataclass(frozen=True)
class AIConfig:
    provider: str = "none"          # none | cli | openai
    model: str = ""
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    timeout: int = 90
    auto_top: int = 0               # 扫描后自动研判前 N 只（0 = 关闭）

    @property
    def enabled(self) -> bool:
        return self.provider in ("cli", "openai")

    @classmethod
    def from_env(cls) -> "AIConfig":
        env = os.environ.get
        provider = env("GOLDEN_PIT_AI_PROVIDER", "none").lower()
        default_model = "deepseek/deepseek-v4-flash" if provider == "cli" else ""
        return cls(
            provider=provider if provider in ("none", "cli", "openai") else "none",
            model=env("GOLDEN_PIT_AI_MODEL", default_model),
            base_url=env("GOLDEN_PIT_AI_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
            api_key=env("GOLDEN_PIT_AI_KEY", ""),
            timeout=int(env("GOLDEN_PIT_AI_TIMEOUT", "90")),
            auto_top=max(0, min(15, int(env("GOLDEN_PIT_AI_AUTO_TOP", "0")))),
        )

    def public(self) -> dict:
        return {"enabled": self.enabled, "provider": self.provider, "model": self.model,
                "auto_top": self.auto_top}


# ------------------------------------------------------------ 快照与提示词
def _clean(s, n=120) -> str:
    """来自外部数据源的文本：去控制字符、限长，降低提示词注入面。"""
    return re.sub(r"[\x00-\x1f\x7f]", " ", str(s or ""))[:n].strip()


def build_snapshot(result: dict) -> dict:
    v = result["verdict"]
    return {
        "ticker": result["ticker"], "name": _clean(result.get("name")),
        "sector": _clean(result.get("sector")), "industry": _clean(result.get("industry")),
        "price": result.get("price"), "currency": result.get("currency"),
        "market_cap": result.get("market_cap"),
        "drop_from_52w_high_pct": None if result.get("drop_from_high_pct") is None
        else round(result["drop_from_high_pct"], 1),
        "dividend_yield_pct": result.get("dividend_yield_pct"),
        "quant_verdict": {"tier": v["tier"], "label": v["label"],
                          "financial_pass": f"{v['fin_pass']}/{v['fin_total']}",
                          "management_pass": f"{v['mgmt_pass']}/3", "unknown_checks": v["unknown_count"]},
        "checks": [{"label": c["label"], "status": c["status"], "value": c["display"], "rule": c["rule"],
                    "note": _clean(c.get("note"), 160)} for c in result["checks"]],
        "data_warnings": [_clean(w, 200) for w in result.get("warnings", [])],
    }


def snapshot_hash(snapshot: dict) -> str:
    """只对"结构性结论"取哈希（档位 + 各项通过状态 + 警告）。
    价格、市值、回撤每天都在变，如果算进去，研判会天天"过期"，失去意义。"""
    core = {"ticker": snapshot["ticker"], "tier": snapshot["quant_verdict"]["tier"],
            "checks": [(c["label"], c["status"]) for c in snapshot["checks"]],
            "warnings": snapshot["data_warnings"]}
    return hashlib.sha256(json.dumps(core, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def build_prompt(snapshot: dict) -> str:
    return f"""你是一位严谨的价值投资研究员，正在审阅一只被量化筛选器标记的美股。你的任务是写一份**研究观点**，帮助使用者判断这个"坑"是真黄金坑还是价值陷阱。

【硬性规则】
1. 只能使用下面"数据快照"里的数字；不得编造任何股价、财务数字、日期或新闻。快照里没有的，写"需自行核实"。
2. 不给买卖指令，不给目标价，不预测股价。只分析：坑的成因、空头逻辑、催化剂、该核实什么。
3. 如果快照里有 unknown（数据缺失），必须在 data_gaps 里指出，并相应降低 confidence。
4. 只返回一个 JSON 对象，不要任何其他文字或 markdown。

【数据快照】
{json.dumps(snapshot, ensure_ascii=False, indent=1)}

【返回格式】（所有文本用中文，每条不超过 60 字）
{{
  "pit_type": "cyclical|structural|event|sentiment|unclear 之一",
  "pit_reason": "为什么判断是这种类型的坑（≤80字）",
  "thesis": "多头逻辑：为什么这可能是真黄金坑（≤100字）",
  "bear_case": ["空头最有力的 2-3 个论点"],
  "catalysts": ["可能让价值回归的 1-3 个催化剂"],
  "verify": ["使用者应当自行核实的 2-4 件事"],
  "data_gaps": ["快照中缺失、影响判断的数据；没有则空数组"],
  "agree_with_quant": "agree|partial|disagree 之一",
  "confidence": "low|medium|high 之一"
}}"""


# ------------------------------------------------------------ 解析与校验
def _strs(v, max_items: int, max_len: int = 100) -> list[str]:
    if not isinstance(v, list):
        return []
    return [_clean(x, max_len) for x in v if isinstance(x, (str, int, float)) and _clean(x, max_len)][:max_items]


def parse_and_validate(text: str) -> dict:
    m = re.search(r"\{[\s\S]*\}", text or "")
    if not m:
        raise AIError("模型没有返回 JSON")
    try:
        d = json.loads(m.group(0))
    except ValueError as e:
        raise AIError(f"模型返回的 JSON 无法解析：{e}") from e
    if not isinstance(d, dict):
        raise AIError("模型返回的不是 JSON 对象")
    pit = d.get("pit_type")
    agree = d.get("agree_with_quant")
    conf = d.get("confidence")
    if pit not in PIT_TYPES or agree not in AGREE or conf not in CONFIDENCE:
        raise AIError("模型返回的枚举字段不合法")
    out = {
        "pit_type": pit, "pit_type_label": PIT_TYPES[pit],
        "pit_reason": _clean(d.get("pit_reason"), 160),
        "thesis": _clean(d.get("thesis"), 200),
        "bear_case": _strs(d.get("bear_case"), 3),
        "catalysts": _strs(d.get("catalysts"), 3),
        "verify": _strs(d.get("verify"), 4),
        "data_gaps": _strs(d.get("data_gaps"), 4),
        "agree_with_quant": agree, "agree_label": AGREE[agree],
        "confidence": conf, "confidence_label": CONFIDENCE[conf],
    }
    if not out["thesis"] or not out["bear_case"]:
        raise AIError("模型返回的内容不完整（缺少多头逻辑或空头论点）")
    return out


# ------------------------------------------------------------ 调用后端
def _cli_command() -> list[str]:
    if sys.platform == "win32":
        cli_mjs = Path(os.environ.get("APPDATA", "")) / "npm" / "node_modules" / "command-code" / "dist" / "cli.mjs"
        node = shutil.which("node")
        if node and cli_mjs.exists():
            return [node, str(cli_mjs)]
    exe = shutil.which("commandcode") or shutil.which("commandcode.cmd")
    if not exe:
        raise AIError("没有找到 Command Code CLI（commandcode）。请先安装并登录，或改用 openai 提供商。")
    return [exe]


def call_cli(prompt: str, cfg: AIConfig) -> str:
    """Command Code CLI 直连（Go Plan 可用，不需要 API Key）。"""
    cmd = _cli_command() + ["-m", cfg.model, "-p", prompt, "--non-interactive"]
    with _CLI_SLOTS:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                  timeout=cfg.timeout, shell=False)
        except subprocess.TimeoutExpired as e:
            raise AIError(f"模型响应超时（>{cfg.timeout}s）") from e
        except OSError as e:
            raise AIError(f"无法启动 Command Code CLI：{e}") from e
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout).strip()[:200]
        if "region" in err.lower() or "Geographic" in err:
            raise AIError("该模型受地域限制，请换一个模型（GOLDEN_PIT_AI_MODEL）")
        raise AIError(f"CLI 返回错误：{err}")
    return proc.stdout


def call_openai(prompt: str, cfg: AIConfig) -> str:
    """任意 OpenAI 兼容接口（OpenAI、DeepSeek、Command Code provider 等）。仅用标准库。"""
    if not cfg.api_key:
        raise AIError("未配置 GOLDEN_PIT_AI_KEY")
    if not cfg.base_url.startswith("https://"):
        raise AIError("GOLDEN_PIT_AI_BASE_URL 必须是 https:// 地址")
    body = json.dumps({"model": cfg.model, "temperature": 0.2, "max_tokens": 900,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request(cfg.base_url + "/chat/completions", data=body, method="POST", headers={
        "Content-Type": "application/json", "Authorization": f"Bearer {cfg.api_key}"})
    try:
        with urllib.request.urlopen(req, timeout=cfg.timeout) as r:  # noqa: S310 — 已限定 https
            data = json.loads(r.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"]
    except urllib.error.HTTPError as e:
        raise AIError(f"接口返回 HTTP {e.code}") from e   # 不回显响应体，避免泄漏
    except (urllib.error.URLError, TimeoutError, KeyError, IndexError, ValueError) as e:
        raise AIError(f"调用失败：{type(e).__name__}") from e


# ------------------------------------------------------------ 服务（含缓存）
class AIService:
    def __init__(self, cfg: AIConfig, cache_dir: Path, runner: Optional[Callable[[str, AIConfig], str]] = None):
        self.cfg = cfg
        self.dir = Path(cache_dir) / "ai"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.runner = runner or (call_cli if cfg.provider == "cli" else call_openai)
        self._lock = threading.Lock()

    def _path(self, ticker: str) -> Path:
        if not TICKER_RE.match(ticker):
            raise AIError("无效的股票代码")
        return self.dir / f"{ticker}.json"

    def cached(self, ticker: str) -> Optional[dict]:
        try:
            return json.loads(self._path(ticker).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def cached_many(self, tickers: list[str]) -> dict[str, dict]:
        return {t: c for t in tickers if (c := self.cached(t))}

    def generate(self, result: dict) -> dict:
        if not self.cfg.enabled:
            raise AIError("未启用 AI（设置 GOLDEN_PIT_AI_PROVIDER=cli 或 openai）")
        if result.get("error") or not result.get("checks"):
            raise AIError("该标的没有可用的量化数据，无法研判")
        snap = build_snapshot(result)
        analysis = parse_and_validate(self.runner(build_prompt(snap), self.cfg))
        record = {"ticker": result["ticker"], "snapshot_hash": snapshot_hash(snap), "model": self.cfg.model,
                  "provider": self.cfg.provider, "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  "analysis": analysis}
        with self._lock:
            tmp = self._path(result["ticker"]).with_suffix(".tmp")
            tmp.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self._path(result["ticker"]))
        return record

    @staticmethod
    def is_stale(record: dict, result: dict) -> bool:
        return record.get("snapshot_hash") != snapshot_hash(build_snapshot(result))
