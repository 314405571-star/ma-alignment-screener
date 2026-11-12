"""Offline demo: eight synthetic 160-day histories, one screen, one figure.

Run from the repository root:

    python examples/demo_mid_rally.py

No baostock, no network, no randomness on the pass/fail path. Every case is a
hand-built price series that isolates one condition, so the table doubles as a
specification of what the screen does and does not accept.
"""

from __future__ import annotations

import math
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.alignment import Bar, Config, Verdict, evaluate, simple_ma  # noqa: E402

try:  # keep the demo usable on a GBK console
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # pragma: no cover
    pass

N_DAYS = 160
LAST_DAY = date(2026, 11, 10)


def business_days(n: int) -> list[str]:
    out: list[str] = []
    day = LAST_DAY
    while len(out) < n:
        if day.weekday() < 5:
            out.append(day.isoformat())
        day -= timedelta(days=1)
    return list(reversed(out))


@dataclass
class Case:
    key: str
    label: str
    closes: list[float]
    volumes: list[float]
    note: str = ""
    adjusted_closes: list[float] | None = None
    bars: list[Bar] = field(default_factory=list)


def _flat(n: int, price: float = 100.0) -> list[float]:
    return [price] * n


def _ramp(n: int, start: float, rate: float) -> list[float]:
    return [start * (1.0 + rate) ** i for i in range(n)]


def _spike(volumes: list[float], multiple: float = 3.0) -> list[float]:
    """Set the last bar's volume to ``multiple`` times the prior 10-day mean."""

    prior = sum(volumes[-11:-1]) / 10.0
    volumes = list(volumes)
    volumes[-1] = multiple * prior
    return volumes


def build_cases() -> list[Case]:
    cases: list[Case] = []

    # 1. Textbook mid-rally: a long base, then a steady 0.35%/day trend.
    closes = _flat(120) + _ramp(40, 100.0, 0.0035)
    cases.append(Case("mid_rally", "Mid-rally", closes,
                      _spike([1000.0 + 4 * i for i in range(N_DAYS)])))

    # 2. Same shape, but the last ten days already ran 18% — the cap exists
    #    precisely to keep this out of the list.
    closes = _flat(120) + _ramp(40, 100.0, 0.017)
    cases.append(Case("late_stage", "Late stage", closes,
                      _spike([1000.0 + 4 * i for i in range(N_DAYS)])))

    # 3. A perfectly linear ramp: +0.5 CNY per day for 100 days. Every MA has
    #    exactly the same slope. The notebook's strict `>` rejects it.
    closes = [100.0] * 60 + [100.0 + 0.5 * i for i in range(100)]
    cases.append(Case("linear", "Linear ramp", closes,
                      _spike([1000.0 + 4 * i for i in range(N_DAYS)]),
                      note="all five slopes identical"))

    # 4. Sideways chop: no trend, but a volume spike.
    closes = [100.0 + 2.5 * math.sin(2 * math.pi * i / 20.0) for i in range(N_DAYS)]
    cases.append(Case("chop", "Chop", closes,
                      _spike([1000.0 + 4 * i for i in range(N_DAYS)])))

    # 5. A pop after a crash: the last eight days are strong, but MA60 still
    #    sits above the short averages. The bounce is real; the trend is not.
    down = _ramp(100, 150.0, -0.003) + _ramp(52, 150.0 * 0.997 ** 100, -0.02)
    closes = down + _ramp(8, down[-1], 0.015)
    cases.append(Case("recovering", "Oversold pop", closes,
                      _spike([1000.0 + 4 * i for i in range(N_DAYS)])))

    # 6. Distribution at the top: short MAs roll over, volume expands.
    up = _ramp(140, 100.0, 0.003)
    closes = up + _ramp(20, up[-1], -0.008)
    cases.append(Case("rollover", "Top rollover", closes,
                      _spike([1000.0 + 4 * i for i in range(N_DAYS)])))

    # 7. Perfect alignment, perfect rise cap, but volume dries up.
    closes = _flat(120) + _ramp(40, 100.0, 0.0035)
    cases.append(Case("volume_dry", "Volume dry-up", closes,
                      _spike([1000.0 + 4 * i for i in range(N_DAYS)], 0.4)))

    # 8. A 3-for-1 split 20 days ago. The raw series is a cliff; the adjusted
    #    series is a clean trend. The notebook screens the raw one.
    closes = [60.0] * 140 + [20.0 * (1.007 ** i) for i in range(20)]
    volumes = _spike([1000.0 + 4 * i for i in range(N_DAYS)])
    adjusted = [c / 3.0 for c in closes[:140]] + list(closes[140:])
    cases.append(Case("split", "3:1 split (raw)", closes, volumes,
                      note="adjusted series would pass",
                      adjusted_closes=adjusted))
    return cases


def attach_bars(cases: list[Case], days: list[str]) -> None:
    for case in cases:
        case.bars = [Bar(d, c, v) for d, c, v in zip(days, case.closes, case.volumes)]


SHORT = {
    "aligned": "alignment",
    "above_ma10": "close<MA10",
    "rising": "slope",
    "not_extended": "10d rise",
    "volume_expansion": "volume",
}


def fmt_failed(verdict: Verdict) -> str:
    if not verdict.failed:
        return "-"
    return ", ".join(SHORT[k] for k in verdict.failed)


def legacy_v1_selected(closes: list[float], volumes: list[float], config: Config) -> bool:
    """The original notebook's strict AND, reproduced to show where it differs."""

    if len(closes) < max(config.ma_windows) + 10:
        return False
    ma = {w: simple_ma(closes, w)[-1] for w in config.ma_windows}
    prev = {w: simple_ma(closes, w)[-2] for w in config.ma_windows}
    if any(v is None for v in ma.values()) or any(v is None for v in prev.values()):
        return False
    values = [ma[w] for w in config.ma_windows]
    if not all(values[i] > values[i + 1] for i in range(len(values) - 1)):
        return False
    if not closes[-1] >= ma[10]:
        return False
    slopes = [ma[w] - prev[w] for w in config.ma_windows]
    if not all(s > 0 for s in slopes):
        return False
    if not all(slopes[i] > slopes[i + 1] for i in range(len(slopes) - 1)):
        return False
    if closes[-1] / closes[-11] - 1 >= config.max_rise:
        return False
    if not (volumes[-1] > sum(volumes[-5:]) / 5 and volumes[-1] > sum(volumes[-10:]) / 10):
        return False
    return True


def print_table(cases: list[Case], rows: list[tuple[str, Verdict]], config: Config) -> None:
    legacy = {c.label: legacy_v1_selected(c.closes, c.volumes, config) for c in cases}
    print(f"{'case':<22} {'score':>5}  {'here':<8} {'v1':<8} {'rise%':>7}  fails (here)")
    print("-" * 92)
    for label, verdict in rows:
        rise = "-" if verdict.rise is None else f"{verdict.rise * 100:6.2f}"
        ours = "SELECT" if verdict.selected else "reject"
        v1 = "SELECT" if legacy.get(label) else "reject"
        print(f"{label:<22} {verdict.score:>3}/50  {ours:<8} {v1:<8} {rise:>7}  {fmt_failed(verdict)}")
    print("-" * 92)


def plot(cases: list[Case], verdicts: list[Verdict], out_path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D

    SURFACE = "#fcfcfb"
    INK = "#0b0b0b"
    SECOND = "#52514e"
    MUTED = "#898781"
    GRID = "#e1e0d9"
    AXIS = "#c3c2b7"
    GOOD = "#0ca30c"
    BAD = "#d03b3b"
    RAMP = ["#86b6ef", "#5598e7", "#2a78d6", "#1c5cab", "#104281"]
    WINDOWS = (5, 10, 20, 30, 60)

    plt.rcParams.update({
        "font.family": "DejaVu Sans",
        "figure.facecolor": SURFACE,
        "axes.facecolor": SURFACE,
        "savefig.facecolor": SURFACE,
        "text.color": INK,
        "axes.labelcolor": SECOND,
        "xtick.color": MUTED,
        "ytick.color": MUTED,
        "axes.edgecolor": AXIS,
        "axes.linewidth": 0.8,
    })

    fig = plt.figure(figsize=(13.6, 7.9))
    gs = fig.add_gridspec(2, 4, left=0.045, right=0.985, top=0.785, bottom=0.10,
                          hspace=0.64, wspace=0.20)
    axes = [fig.add_subplot(gs[i // 4, i % 4]) for i in range(len(cases))]

    fig.text(0.045, 0.955, "Five-line bullish alignment: the shape is the signal",
             fontsize=17, fontweight="semibold", color=INK, ha="left", va="top")
    fig.text(0.045, 0.917,
             "Eight synthetic 160-day histories, screened on the last bar. "
             "MA5–MA60 share one ordinal blue ramp; price is the ink line.",
             fontsize=9.6, color=SECOND, ha="left", va="top")

    handles = [Line2D([], [], color=INK, lw=1.8, label="close")]
    handles += [Line2D([], [], color=c, lw=1.15, label=f"MA{w}")
                for c, w in zip(RAMP, WINDOWS)]
    handles += [
        Line2D([], [], marker="o", linestyle="none", markersize=6,
               markerfacecolor=GOOD, markeredgecolor=SURFACE, markeredgewidth=1.5,
               label="selected"),
        Line2D([], [], marker="o", linestyle="none", markersize=6,
               markerfacecolor=BAD, markeredgecolor=SURFACE, markeredgewidth=1.5,
               label="rejected"),
    ]
    fig.legend(handles=handles, loc="upper left", bbox_to_anchor=(0.043, 0.887),
               ncol=8, frameon=False, fontsize=8.2, handlelength=1.6,
               columnspacing=1.2, handletextpad=0.5)

    for ax, case, verdict in zip(axes, cases, verdicts):
        closes = case.closes[-100:]
        x = range(len(closes))
        for w, color in zip(WINDOWS, RAMP):
            series = simple_ma(case.closes, w)[-100:]
            ax.plot(x, [v for v in series], color=color, lw=1.15,
                    solid_capstyle="round", zorder=2)
        ax.plot(x, closes, color=INK, lw=1.8, solid_capstyle="round", zorder=3)
        ax.plot([len(closes) - 1], [closes[-1]], marker="o", markersize=4.5,
                color=INK, markeredgecolor=SURFACE, markeredgewidth=1.2, zorder=4)

        ax.set_xticks([])
        ax.yaxis.set_major_locator(matplotlib.ticker.MaxNLocator(3))
        ax.tick_params(axis="y", labelsize=7.8, length=0, pad=2)
        ax.grid(axis="y", color=GRID, lw=0.6, ls="-")
        ax.set_axisbelow(True)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        ax.spines["left"].set_color(AXIS)
        ax.spines["bottom"].set_color(AXIS)
        ax.margins(x=0.01)

        ax.set_title(case.label, loc="left", fontsize=10.2, fontweight="semibold",
                     color=INK, pad=17)
        status_color = GOOD if verdict.selected else BAD
        ax.plot([0.005], [1.035], transform=ax.transAxes, marker="o", markersize=5,
                markerfacecolor=status_color, markeredgecolor=SURFACE,
                markeredgewidth=1.2, clip_on=False, zorder=5)
        status_text = "selected" if verdict.selected else "rejected"
        ax.text(0.035, 1.025, f"{status_text} · {verdict.score}/50",
                transform=ax.transAxes, fontsize=8.4, color=SECOND,
                va="bottom", ha="left")
        note = f"fails: {fmt_failed(verdict)}"
        if case.note:
            note += f"   ·   {case.note}"
        ax.text(0.0, -0.10, note, transform=ax.transAxes, fontsize=7.1,
                color=MUTED, va="top", ha="left")

    fig.text(0.045, 0.018,
             "Synthetic data built to isolate one condition each; the screen is the same "
             "one documented in src/alignment.py. Not investment advice.",
             fontsize=7.4, color=MUTED, ha="left", va="bottom")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=170)
    plt.close(fig)


def main() -> int:
    config = Config()
    days = business_days(N_DAYS)
    cases = build_cases()
    attach_bars(cases, days)

    rows: list[tuple[str, Verdict]] = []
    for case in cases:
        verdict = evaluate(case.bars, config, code=case.key)
        rows.append((case.label, verdict))

    split_case = next(c for c in cases if c.key == "split")
    assert split_case.adjusted_closes is not None
    adjusted_case = Case(
        "split_adjusted", "3:1 split (adjusted)", split_case.adjusted_closes,
        split_case.volumes, note="forward-adjusted",
    )
    attach_bars([adjusted_case], days)
    adjusted_verdict = evaluate(adjusted_case.bars, config, code="split_adjusted")
    rows.append((adjusted_case.label, adjusted_verdict))

    print_table(cases + [adjusted_case], rows, config)

    picks = [label for label, v in rows if v.selected]
    print(f"\nselected: {len(picks)} of {len(rows)}  ->  {', '.join(picks)}")
    noise = sorted(
        ((label, v) for label, v in rows if v.score > 0 and not v.selected),
        key=lambda item: -item[1].score,
    )
    print("a naive 'score > 0' list would also show:",
          ", ".join(f"{label} ({v.score}/50)" for label, v in noise))

    out = ROOT / "examples" / "figures" / "mid_rally_demo.png"
    plot(cases, [v for _, v in rows[:len(cases)]], out)
    print(f"\nfigure: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
