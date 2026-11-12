"""Date-scoped checkpoint and code-normalisation helpers.

The original notebook wrote ``checkpoint.csv`` and ``processed_codes.csv`` with
no date in either the filename or the schema check. Running it the next day
therefore loaded *yesterday's* finished list, treated every code as already
processed, and printed the previous day's picks as if they were today's — with
no error, no warning, and a correct-looking log line.

The fix is boring and total: the date is part of the cache key, and any row
whose ``分析日期`` is not the requested date is discarded on load.
"""

from __future__ import annotations

import csv
import os
from pathlib import Path
from typing import Iterable, Mapping, Sequence

__all__ = [
    "checkpoint_path",
    "processed_path",
    "load_rows",
    "reusable_rows",
    "normalize_codes",
    "filter_stale",
]

PREFIXES = ("sh.", "sz.", "bj.")
DATE_COLUMN = "分析日期"
CODE_COLUMN = "股票代码"


def checkpoint_path(directory: str | os.PathLike[str], date: str) -> Path:
    """``checkpoint_2026-11-10.csv`` — never a single shared ``checkpoint.csv``."""

    if not date or "/" in date or "\\" in date:
        raise ValueError(f"invalid date for a cache key: {date!r}")
    return Path(directory) / f"checkpoint_{date}.csv"


def processed_path(directory: str | os.PathLike[str], date: str) -> Path:
    if not date or "/" in date or "\\" in date:
        raise ValueError(f"invalid date for a cache key: {date!r}")
    return Path(directory) / f"processed_{date}.csv"


def load_rows(path: str | os.PathLike[str]) -> list[dict[str, str]]:
    p = Path(path)
    if not p.exists():
        return []
    with p.open("r", encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def reusable_rows(
    rows: Iterable[Mapping[str, object]], date: str
) -> list[Mapping[str, object]]:
    """Keep only rows that were produced for ``date``.

    A missing or empty date column disqualifies the row: an unlabelled
    checkpoint is exactly the case that silently leaked yesterday's results.
    """

    if not rows:
        return []
    first = next(iter(rows))
    if DATE_COLUMN not in first:
        return []
    return [r for r in rows if str(r.get(DATE_COLUMN, "")).strip() == date]


def normalize_codes(codes: Iterable[object]) -> list[str]:
    """Strip, prefix, and de-duplicate a code list, preserving first-seen order.

    The notebook appended both a processed code and every duplicate input code
    to the result list, so a pool with one duplicated line produced two rows for
    the same stock. Deduplicating at the boundary removes the whole class.
    """

    seen: set[str] = set()
    out: list[str] = []
    for raw in codes:
        if raw is None:
            continue
        code = str(raw).strip()
        if not code or code.lower() in {"nan", "none"}:
            continue
        if not code.startswith(PREFIXES):
            if code.startswith("6"):
                code = "sh." + code
            elif code.startswith(("0", "3")):
                code = "sz." + code
            elif code.startswith(("8", "4")):
                code = "bj." + code
            else:
                code = "sh." + code
        if code not in seen:
            seen.add(code)
            out.append(code)
    return out


def filter_stale(rows: Sequence[Mapping[str, object]], date: str) -> list[Mapping[str, object]]:
    """Drop data rows whose ``最新日期`` is not the requested trading day.

    A suspension, a holiday, or a stale vendor snapshot otherwise returns a
    verdict computed on last week's close while the report says today.
    """

    return [r for r in rows if str(r.get("最新日期", "")).strip() == date]
