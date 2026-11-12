"""Regression tests for the date-scoped checkpoint (defect #4)."""

from __future__ import annotations

import sys
import tempfile
import traceback
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.cache import (  # noqa: E402
    checkpoint_path,
    filter_stale,
    load_rows,
    normalize_codes,
    processed_path,
    reusable_rows,
)


def test_checkpoint_path_is_date_scoped():
    a = checkpoint_path("cache", "2026-11-10")
    b = checkpoint_path("cache", "2026-11-11")
    assert a != b
    assert a.name == "checkpoint_2026-11-10.csv"
    assert processed_path("cache", "2026-11-10").name == "processed_2026-11-10.csv"


def test_invalid_date_cannot_escape_the_cache_directory():
    for bad in ("", "../x", "2026/11/10", "2026\\11\\10"):
        try:
            checkpoint_path("cache", bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad!r} must be rejected")


def test_previous_day_rows_are_not_reused():
    yesterday = [{"股票代码": "sh.600000", "分析日期": "2026-11-09", "得分": 50}]
    assert reusable_rows(yesterday, "2026-11-10") == []
    assert reusable_rows(yesterday, "2026-11-09") == yesterday


def test_rows_without_a_date_column_are_dropped():
    unlabelled = [{"股票代码": "sh.600000", "得分": 50}]
    assert reusable_rows(unlabelled, "2026-11-10") == []


def test_checkpoint_round_trip():
    with tempfile.TemporaryDirectory() as tmp:
        path = checkpoint_path(tmp, "2026-11-10")
        path.write_text(
            "股票代码,股票名称,分析日期,最新日期,得分\n"
            "sh.600000,浦发银行,2026-11-10,2026-11-10,50\n",
            encoding="utf-8-sig",
        )
        rows = load_rows(path)
        assert len(rows) == 1
        assert reusable_rows(rows, "2026-11-10")[0]["得分"] == "50"


def test_normalize_codes_prefixes_and_dedupes():
    codes = ["600000", "sh.600000", "000001", "300750", "688981", "830799", " 000001 "]
    assert normalize_codes(codes) == [
        "sh.600000", "sz.000001", "sz.300750", "sh.688981", "bj.830799",
    ]


def test_normalize_codes_skips_empty_and_nan():
    assert normalize_codes(["", "  ", "nan", None, 600000]) == ["sh.600000"]


def test_filter_stale_drops_last_weeks_bar():
    rows = [
        {"最新日期": "2026-11-10", "股票代码": "sh.600000"},
        {"最新日期": "2026-11-07", "股票代码": "sh.600001"},
    ]
    kept = filter_stale(rows, "2026-11-10")
    assert [r["股票代码"] for r in kept] == ["sh.600000"]


def test_a_suspended_stock_cannot_report_yesterdays_close_as_today():
    """The scenario the notebook gets wrong: the screen runs on the 10th, the
    stock did not trade, and the row still says 10th while the bar is the 7th."""

    today = date(2026, 11, 10)
    stale_bar_date = (today - timedelta(days=3)).isoformat()
    row = {"股票代码": "sz.000001", "分析日期": "2026-11-10", "最新日期": stale_bar_date}
    assert filter_stale([row], "2026-11-10") == []


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
