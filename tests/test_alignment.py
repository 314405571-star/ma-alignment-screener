"""Regression tests for the five-line screen.

Every test that is named after a defect corresponds to an entry in
``docs/算法总结.md`` §4 and to a row in the comparison table in the README.
"""

from __future__ import annotations

import math
import sys
import traceback
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.alignment import (  # noqa: E402
    Bar,
    CHECK_KEYS,
    Config,
    evaluate,
    rank,
    select,
    simple_ma,
    to_row,
)

CONFIG = Config()
START = date(2026, 1, 5)


# ----------------------------------------------------------------- helpers
def series(closes, volumes=None):
    volumes = volumes if volumes is not None else [1000.0] * len(closes)
    return [
        Bar((START + timedelta(days=i)).isoformat(), close, vol)
        for i, (close, vol) in enumerate(zip(closes, volumes))
    ]


def flat(n, price=100.0):
    return [price] * n


def ramp(n, start, rate):
    return [start * (1.0 + rate) ** i for i in range(n)]


def spike(volumes, multiple=3.0):
    out = list(volumes)
    out[-1] = multiple * (sum(volumes[-11:-1]) / 10.0)
    return out


def mid_rally(n=160, daily=0.0035, volume_multiple=3.0):
    closes = flat(n - 40) + ramp(40, 100.0, daily)
    volumes = spike([1000.0 + 4 * i for i in range(n)], volume_multiple)
    return series(closes, volumes)


def legacy_strict_selected(closes, volumes):
    """The v1 notebook predicate, verbatim in behaviour (strict slopes,
    inclusive volume baselines, 70-bar guard). Used only to document what
    this repository changed."""

    if len(closes) < 70:
        return False
    ma = {w: simple_ma(closes, w)[-1] for w in (5, 10, 20, 30, 60)}
    prev = {w: simple_ma(closes, w)[-2] for w in (5, 10, 20, 30, 60)}
    if any(v is None for v in ma.values()) or any(v is None for v in prev.values()):
        return False
    vals = [ma[w] for w in (5, 10, 20, 30, 60)]
    if not all(vals[i] > vals[i + 1] for i in range(4)):
        return False
    if not closes[-1] >= ma[10]:
        return False
    slopes = [ma[w] - prev[w] for w in (5, 10, 20, 30, 60)]
    if not all(s > 0 for s in slopes):
        return False
    if not all(slopes[i] > slopes[i + 1] for i in range(4)):
        return False
    if closes[-1] / closes[-11] - 1 >= 0.15:
        return False
    if not (volumes[-1] > sum(volumes[-5:]) / 5 and volumes[-1] > sum(volumes[-10:]) / 10):
        return False
    return True


# ------------------------------------------------------------------- tests
def test_simple_ma_matches_the_manual_mean():
    values = [1.0, 2.0, 3.0, 4.0, 5.0]
    out = simple_ma(values, 3)
    assert out == [None, None, 2.0, 3.0, 4.0], out
    assert simple_ma([1.0, 2.0], 5) == [None, None]
    try:
        simple_ma(values, 0)
    except ValueError:
        pass
    else:
        raise AssertionError("window=0 must raise")


def test_textbook_mid_rally_is_selected():
    verdict = evaluate(mid_rally(), CONFIG, code="mid")
    assert verdict.selected, verdict.reasons
    assert verdict.score == 50, verdict.score
    assert verdict.failed == []


def test_late_stage_fails_only_the_rise_cap():
    bars = mid_rally(daily=0.017)
    verdict = evaluate(bars, CONFIG, code="late")
    assert not verdict.selected
    assert verdict.score == 40, verdict.score
    assert verdict.failed == ["not_extended"], verdict.failed
    assert verdict.rise > CONFIG.max_rise


def test_linear_ramp_is_selected_here():
    """A straight +0.5/day ramp has *identical* slopes on all five windows.

    The notebook's strict ``>`` chain rejects it. Here equality is accepted
    (within a price-scaled tolerance), because a linear trend is the cleanest
    bullish structure there is.
    """

    closes = flat(60) + [100.0 + 0.5 * i for i in range(100)]
    bars = series(closes, spike([1000.0 + 4 * i for i in range(160)]))
    verdict = evaluate(bars, CONFIG, code="linear")
    assert verdict.selected, verdict.reasons
    slopes = [verdict.slope[w] for w in CONFIG.ma_windows]
    assert all(abs(s - 0.5) < 1e-12 for s in slopes), slopes
    assert len({round(s, 12) for s in slopes}) == 1, "slopes are exactly equal"


def test_legacy_strict_order_rejects_the_linear_ramp():
    """Regression for defect #1: strict slope ordering + equal slopes = reject.

    This is the bug, reproduced. It is why the fix above is not cosmetic.
    """

    closes = flat(60) + [100.0 + 0.5 * i for i in range(100)]
    volumes = spike([1000.0 + 4 * i for i in range(160)])
    assert legacy_strict_selected(closes, volumes) is False


def test_flat_market_is_not_rising():
    bars = series(flat(160), spike([1000.0] * 160))
    verdict = evaluate(bars, CONFIG)
    assert verdict.checks["aligned"] is False
    assert verdict.checks["rising"] is False
    assert verdict.selected is False


def test_volume_baseline_excludes_today():
    """Defect #2: the notebook's 5/10-day mean includes today, so the test
    collapses to "above the previous 4-day mean" — a ~1% move clears it.

    Here the baseline is the previous five days and the default requires 1.5x.
    """

    closes = flat(120) + ramp(40, 100.0, 0.0035)
    volumes = [1000.0] * 160
    volumes[-1] = 1500.0  # exactly 1.5x: strict, so this must fail
    assert evaluate(series(closes, volumes), CONFIG).checks["volume_expansion"] is False

    volumes[-1] = 1500.1
    assert evaluate(series(closes, volumes), CONFIG).checks["volume_expansion"] is True


def test_suspension_zero_volume_blocks_expansion():
    closes = flat(120) + ramp(40, 100.0, 0.0035)
    volumes = [1000.0] * 160
    volumes[-5] = 0.0  # a suspended day inside the 5-day baseline
    verdict = evaluate(series(closes, volumes), CONFIG)
    assert verdict.checks["volume_expansion"] is False
    assert any("volume_baseline_unusable" in r for r in verdict.reasons), verdict.reasons


def test_scoring_alone_admits_a_bearish_stock():
    """Defect #3: ranking by "score > 0" puts non-trends on the buy list.

    A sideways market with a volume spike scores 30/50 while failing both
    structural conditions.
    """

    closes = [100.0 + 2.5 * math.sin(2 * math.pi * i / 20.0) for i in range(160)]
    verdict = evaluate(series(closes, spike([1000.0 + 4 * i for i in range(160)])), CONFIG)
    assert verdict.score > 0
    assert verdict.selected is False
    assert "aligned" in verdict.failed and "rising" in verdict.failed
    assert verdict in rank([verdict])


def test_insufficient_history_is_rejected_not_crashed():
    verdict = evaluate(series(flat(60)), CONFIG)
    assert verdict.selected is False
    assert verdict.score == 0
    assert any("insufficient_history" in r for r in verdict.reasons)


def test_unsorted_bars_raise():
    bars = series(flat(160))
    bars[10], bars[12] = bars[12], bars[10]
    try:
        evaluate(bars, CONFIG)
    except ValueError as exc:
        assert "ascending" in str(exc)
    else:
        raise AssertionError("unsorted bars must raise, not silently average")


def test_rise_cap_rejects_sixteen_percent():
    closes = flat(150) + [100.0 + 1.6 * (i + 1) for i in range(10)]
    assert abs(closes[-1] / closes[-11] - 1 - 0.16) < 1e-12
    verdict = evaluate(series(closes, spike([1000.0] * 160)), CONFIG)
    assert verdict.checks["not_extended"] is False
    assert verdict.rise > 0.15


def test_ma_windows_are_configurable():
    config = Config(ma_windows=(3, 6), volume_windows=(3,), volume_ratio=1.1)
    # 11 = the 10-day return needs 11 bars; the 6-day MA needs 7.
    assert config.min_history == 11
    bars = series(flat(20) + ramp(10, 100.0, 0.01), spike([1000.0] * 30, 2.0))
    verdict = evaluate(bars, config)
    assert set(verdict.ma) == {3, 6}
    assert verdict.selected, verdict.reasons


def test_select_sorts_by_score_then_least_extended():
    slow = evaluate(mid_rally(daily=0.002), CONFIG, code="slow")
    fast = evaluate(mid_rally(daily=0.004), CONFIG, code="fast")
    picked = select([fast, slow])
    assert [v.code for v in picked] == ["slow", "fast"], [(v.code, v.rise) for v in picked]


def test_to_row_matches_the_notebook_schema():
    verdict = evaluate(mid_rally(), CONFIG, code="sh.600000")
    row = to_row(verdict, CONFIG, name="示例", analysis_date="2026-11-10")
    for column in ("股票代码", "股票名称", "分析日期", "最新日期", "最新收盘",
                   "MA5", "MA10", "MA20", "MA30", "MA60", "10日涨幅(%)",
                   "当日成交量", "5日均量", "10日均量", "得分", "满足条件", "入选"):
        assert column in row, column
    assert row["入选"] is True
    assert CHECK_KEYS == ("aligned", "above_ma10", "rising", "not_extended", "volume_expansion")


# ------------------------------------------------------------------ runner
def main() -> int:
    tests = [(name, obj) for name, obj in sorted(globals().items())
             if name.startswith("test_") and callable(obj)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
        except Exception:
            failed += 1
            print(f"FAIL {name}")
            traceback.print_exc()
        else:
            print(f"ok   {name}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
