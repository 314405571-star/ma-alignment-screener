"""Pure evaluation of the five-line bullish-alignment screen (五线多头排列).

No pandas, no network, no global state: everything here is a function of a
sequence of bars and a configuration. That is deliberate — the interesting bugs
in this strategy are all boundary bugs, and boundary bugs are only findable when
the whole rule set can be called in a unit test.

The five conditions, as stated by the original notebook:

1. ``MA5 > MA10 > MA20 > MA30 > MA60``            (aligned)
2. ``close >= MA10``                                (above_ma10)
3. every MA slope > 0 and the slopes are ordered
   ``slope(MA5) > slope(MA10) > ... > slope(MA60)`` (rising)
4. 10-day return ``< 15%``                          (not_extended)
5. ``volume >`` each volume baseline                (volume_expansion)

Two departures from the notebook, both documented in ``docs/算法总结.md``:

* condition 5 compares today's volume with the mean of the *previous* N days.
  The notebook compares with a mean that includes today, which algebraically
  collapses to "today > the previous 4-day mean" — a bar almost any day clears.
* condition 3 allows equal slopes (within a price-scaled tolerance). A perfectly
  linear uptrend has *identical* slopes for every window and the notebook's
  strict ``>`` rejects it — sometimes, depending on float noise.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Sequence

__all__ = [
    "Bar",
    "Config",
    "Verdict",
    "CHECK_KEYS",
    "evaluate",
    "select",
    "rank",
    "simple_ma",
    "to_row",
]


CHECK_KEYS = ("aligned", "above_ma10", "rising", "not_extended", "volume_expansion")


@dataclass(frozen=True, slots=True)
class Bar:
    """One daily bar. ``date`` is an ISO string, ascending within a series."""

    date: str
    close: float
    volume: float = 0.0


@dataclass(frozen=True)
class Config:
    """Parameters of the screen. Defaults match the notebook unless noted."""

    ma_windows: tuple[int, ...] = (5, 10, 20, 30, 60)
    max_rise: float = 0.15
    rise_lookback: int = 10
    # The notebook's condition 5 used a 5/10-day mean *including* today, which
    # is equivalent to "above the previous 4/9-day mean". Here the baseline
    # excludes today and the default requires a real expansion (1.5x).
    volume_windows: tuple[int, ...] = (5, 10)
    volume_ratio: float = 1.5
    # Relative price tolerance, used for slope comparisons and for the
    # close-vs-MA10 test. 1e-9 * price is far below any real tick.
    rel_tolerance: float = 1e-9

    def __post_init__(self) -> None:
        if len(self.ma_windows) < 2:
            raise ValueError("ma_windows needs at least two windows")
        if sorted(self.ma_windows) != list(self.ma_windows):
            raise ValueError("ma_windows must be ascending")
        if any(w <= 0 for w in self.ma_windows):
            raise ValueError("ma_windows must be positive")
        if any(w <= 0 for w in self.volume_windows):
            raise ValueError("volume_windows must be positive")
        if not 0 < self.max_rise < 1:
            raise ValueError("max_rise must be a fraction in (0, 1)")
        if self.volume_ratio <= 0:
            raise ValueError("volume_ratio must be positive")

    @property
    def min_history(self) -> int:
        """Bars needed for every MA at both today and yesterday."""
        return max(max(self.ma_windows) + 1, self.rise_lookback + 1,
                   max(self.volume_windows) + 1)

    def labels(self) -> dict[str, str]:
        return {
            "aligned": "五线多头",
            "above_ma10": "收盘>=MA10",
            "rising": "斜率多头",
            "not_extended": f"{self.rise_lookback}日涨幅<{self.max_rise:.0%}",
            "volume_expansion": f"量能>{self.volume_ratio:g}倍均量",
        }


def _eps(value: float, rel: float) -> float:
    return rel * max(1.0, abs(value))


def simple_ma(values: Sequence[float], window: int) -> list[float | None]:
    """Rolling simple mean, ``None`` until ``window`` observations exist.

    Incremental sum: O(n) instead of O(n*window). The naive
    ``sum(values[i-window:i])`` in a loop is what makes a 5,000-stock screen
    feel slow in the notebook version.
    """

    if window <= 0:
        raise ValueError("window must be positive")
    out: list[float | None] = [None] * len(values)
    if window > len(values):
        return out
    total = math.fsum(values[:window])
    out[window - 1] = total / window
    for i in range(window, len(values)):
        total += values[i] - values[i - window]
        out[i] = total / window
    return out


@dataclass
class Verdict:
    """The result of evaluating one stock on one day."""

    code: str
    date: str
    checks: dict[str, bool]
    score: int
    selected: bool
    structural: bool
    ma: dict[int, float | None] = field(default_factory=dict)
    slope: dict[int, float | None] = field(default_factory=dict)
    close: float | None = None
    rise: float | None = None
    volume: float | None = None
    volume_bases: dict[int, float | None] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    stale: bool = False

    @property
    def failed(self) -> list[str]:
        return [k for k in CHECK_KEYS if not self.checks.get(k, False)]


def _rejected(code: str, date: str, reason: str) -> Verdict:
    return Verdict(
        code=code,
        date=date,
        checks={k: False for k in CHECK_KEYS},
        score=0,
        selected=False,
        structural=False,
        reasons=[reason],
    )


def evaluate(bars: Sequence[Bar], config: Config = Config(), *, code: str = "") -> Verdict:
    """Evaluate the last bar of ``bars`` against the five conditions.

    ``bars`` must be sorted oldest → newest; an unsorted series raises instead
    of silently computing a moving average across a date discontinuity.
    """

    n = len(bars)
    latest_date = bars[-1].date if bars else ""

    if n < 2:
        return _rejected(code, latest_date, "insufficient_history")
    for prev, cur in zip(bars, bars[1:]):
        if prev.date >= cur.date:
            raise ValueError(
                f"bars must be strictly ascending by date; got {prev.date} then {cur.date}"
            )

    if n < config.min_history:
        return _rejected(
            code, latest_date,
            f"insufficient_history: need {config.min_history} bars, got {n}",
        )

    closes = [b.close for b in bars]
    volumes = [b.volume for b in bars]
    if not all(math.isfinite(c) for c in closes):
        return _rejected(code, latest_date, "non_finite_close")
    if not all(math.isfinite(v) for v in volumes):
        return _rejected(code, latest_date, "non_finite_volume")

    target = closes[-1]
    eps = _eps(target, config.rel_tolerance)

    # --- moving averages, today and yesterday -----------------------------
    ma_today: dict[int, float | None] = {}
    ma_prev: dict[int, float | None] = {}
    for w in config.ma_windows:
        series = simple_ma(closes, w)
        ma_today[w] = series[-1]
        ma_prev[w] = series[-2]
    if any(v is None for v in ma_today.values()) or any(v is None for v in ma_prev.values()):
        return _rejected(code, latest_date, "insufficient_history")

    windows = config.ma_windows
    ma_values = [ma_today[w] for w in windows]  # type: ignore[list-item]
    slopes = {w: ma_today[w] - ma_prev[w] for w in windows}  # type: ignore[operator]

    checks: dict[str, bool] = {}
    reasons: list[str] = []

    # 1. aligned: strictly decreasing MA by window, beyond noise.
    checks["aligned"] = all(
        ma_values[i] - ma_values[i + 1] > eps for i in range(len(windows) - 1)
    )

    # 2. close above MA10 (the middle window, or the closest to 10).
    ref_window = min(windows, key=lambda w: abs(w - 10))
    checks["above_ma10"] = closes[-1] + eps >= ma_today[ref_window]  # type: ignore[operator]

    # 3. all MAs rising, and shorter windows rising at least as fast.
    #    Equality is allowed: a linear ramp has identical slopes everywhere and
    #    is the healthiest trend there is.
    slope_values = [slopes[w] for w in windows]
    checks["rising"] = all(s > eps for s in slope_values) and all(
        slope_values[i] + eps >= slope_values[i + 1] for i in range(len(windows) - 1)
    )

    # 4. not extended: 10-day return below the cap.
    close_then = closes[-1 - config.rise_lookback]
    if close_then <= 0:
        rise = None
        checks["not_extended"] = False
        reasons.append("non_positive_close_in_lookback")
    else:
        rise = closes[-1] / close_then - 1.0
        checks["not_extended"] = rise < config.max_rise

    # 5. volume expansion against the *previous* N days.
    bases: dict[int, float | None] = {}
    volume_ok = True
    for w in config.volume_windows:
        prior = volumes[-1 - w:-1]
        if len(prior) < w or any(v <= 0 for v in prior):
            bases[w] = None
            volume_ok = False
            reasons.append(f"volume_baseline_unusable_{w}d")
            continue
        base = math.fsum(prior) / w
        bases[w] = base
        if not volumes[-1] > config.volume_ratio * base:
            volume_ok = False
    checks["volume_expansion"] = volume_ok

    score = 10 * sum(1 for v in checks.values() if v)
    structural = checks["aligned"] and checks["rising"]
    selected = all(checks.values())

    if not checks["aligned"]:
        reasons.append("ma_not_aligned")
    if not checks["rising"]:
        rising_flags = [s > eps for s in slope_values]
        if not all(rising_flags):
            reasons.append("ma_slope_not_positive")
        else:
            reasons.append("ma_slope_order_broken")
    if rise is not None and not checks["not_extended"]:
        reasons.append(f"rise_{rise:.1%}_above_cap")

    return Verdict(
        code=code,
        date=latest_date,
        checks=checks,
        score=score,
        selected=selected,
        structural=structural,
        ma=ma_today,
        slope=slopes,
        close=closes[-1],
        rise=rise,
        volume=volumes[-1],
        volume_bases=bases,
        reasons=reasons,
    )


def select(verdicts: Iterable[Verdict], *, min_score: int = 0) -> list[Verdict]:
    """The strict screen: every condition true, best score first, then the
    least-extended name. ``min_score`` exists for callers who score manually."""

    picked = [v for v in verdicts if v.selected and v.score >= min_score]
    return sorted(picked, key=lambda v: (-v.score, v.rise if v.rise is not None else math.inf))


def rank(verdicts: Iterable[Verdict]) -> list[Verdict]:
    """Rank *all* verdicts, including rejected ones.

    This is the notebook's output ordering. It is useful for inspection and
    dangerous as a buy list: a bearish stock can score 20/50 just by being flat
    on the day and trading above its average volume.
    """

    return sorted(verdicts, key=lambda v: (-v.score, v.rise if v.rise is not None else math.inf))


def to_row(verdict: Verdict, config: Config = Config(), *, name: str = "",
           analysis_date: str | None = None) -> dict[str, object]:
    """Flatten a verdict into the notebook's Chinese-column CSV schema."""

    labels = config.labels()
    satisfied = "；".join(labels[k] for k in CHECK_KEYS if verdict.checks.get(k))
    failed = "；".join(labels[k] for k in CHECK_KEYS if not verdict.checks.get(k))

    row: dict[str, object] = {
        "股票代码": verdict.code,
        "股票名称": name,
        "分析日期": analysis_date or verdict.date,
        "最新日期": verdict.date,
        "最新收盘": None if verdict.close is None else round(verdict.close, 2),
        "得分": verdict.score,
        "满足条件": satisfied,
        "未满足条件": failed,
    }
    for w in config.ma_windows:
        row[f"MA{w}"] = None if verdict.ma.get(w) is None else round(verdict.ma[w], 2)
    row["10日涨幅(%)"] = None if verdict.rise is None else round(verdict.rise * 100, 2)
    row["当日成交量"] = None if verdict.volume is None else int(verdict.volume)
    for w in config.volume_windows:
        base = verdict.volume_bases.get(w)
        row[f"{w}日均量"] = None if base is None else int(base)
    row["入选"] = verdict.selected
    return row
