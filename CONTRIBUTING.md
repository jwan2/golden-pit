# Contributing · 参与贡献

Thanks for helping! 欢迎参与。

## Setup

```bash
git clone https://github.com/jwan2/golden-pit.git && cd golden-pit
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest                                             # all tests are offline — no network needed
golden-pit serve
```

## The one rule that matters

**Never turn missing or unreliable data into a pass.** If a metric can't be computed reliably, return `UNKNOWN` with a note that explains why. Every rule change needs a test in `tests/` that pins the behaviour, especially for edge cases such as negative EBIT, missing rows, currency mismatches and splits.

**缺失或不可靠的数据，永远不能变成"通过"。** 算不准就返回 `UNKNOWN`，并写清原因。任何规则改动都要在 `tests/` 里有对应测试。

## Where things live

| File | Responsibility |
|---|---|
| `golden_pit/providers/` | Fetching and caching. The only code that touches the network, along with `universe.live_screen` and `ai`. |
| `golden_pit/metrics.py` | The 11 checks. Pure functions. |
| `golden_pit/scoring.py` | Quality gate × pit condition → verdict and reasons. |
| `golden_pit/timing.py` | Price-timing state. Display only. |
| `golden_pit/track.py` | Forward track record. |
| `golden_pit/web/` | Vanilla JS UI. **No `innerHTML`** — build DOM with `h()` / `fill()`. |

## Pull requests

- Keep PRs focused, and describe *why* as well as what.
- UI text is currently Chinese. English i18n is very welcome.
- Don't commit market data, scan results or API keys.
