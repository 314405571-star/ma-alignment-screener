"""baostock adapter: pool → bars → verdicts → date-scoped CSV.

Everything that can fail over the network lives here; everything that decides
whether a stock passes lives in :mod:`src.alignment`. Keep that split, and the
strategy stays testable while the vendor moves.

Two behaviours differ from the original notebook, on purpose:

* prices are fetched **forward-adjusted** by default (``adjustflag="2"``). The
  notebook used unadjusted prices (``"3"``) and then computed 60-day moving
  averages and a 10-day return across ex-dividend and split dates — silently
  turning a clean trend into a broken one, or the reverse.
* checkpoints and the processed-code log are keyed by date. See
  :mod:`src.cache` for why.
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import socket
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Sequence

from .alignment import Bar, Config, Verdict, evaluate, select, to_row
from .cache import (
    checkpoint_path,
    filter_stale,
    load_rows,
    normalize_codes,
    processed_path,
    reusable_rows,
)

__all__ = ["BaoStockSource", "run_screen", "build_pool"]

log = logging.getLogger("ma_screen")

ADJUST_FLAGS = {"forward": "2", "backward": "1", "raw": "3"}
FIELDS = "date,code,open,high,low,close,volume,amount,turn"


class BaoStockSource:
    """A thin, retrying wrapper around baostock's daily-bar query."""

    def __init__(
        self,
        *,
        adjust: str = "forward",
        request_delay: float = 0.3,
        max_retries: int = 3,
        stock_timeout: float = 8.0,
    ) -> None:
        if adjust not in ADJUST_FLAGS:
            raise ValueError(f"adjust must be one of {sorted(ADJUST_FLAGS)}")
        self.adjust = adjust
        self.request_delay = request_delay
        self.max_retries = max_retries
        self.stock_timeout = stock_timeout
        self.logged_in = False
        self._bs = None

    # -- connection -------------------------------------------------------
    @property
    def bs(self):
        if self._bs is None:
            import baostock as bs  # lazy: importing is cheap but pointless offline

            self._bs = bs
        return self._bs

    def login(self) -> bool:
        for attempt in range(self.max_retries):
            try:
                time.sleep(self.request_delay)
                res = self.bs.login()
                if res.error_code == "0":
                    self.logged_in = True
                    # Warm-up query: baostock's login can succeed while the
                    # session is not yet usable, which shows up as an empty
                    # first response rather than an error.
                    today = datetime.now().strftime("%Y-%m-%d")
                    try:
                        self.bs.query_trade_dates(start_date=today, end_date=today)
                    except Exception as exc:  # pragma: no cover - network
                        log.debug("warm-up failed (harmless): %s", exc)
                    return True
                log.warning("login failed: %s (retry %d/%d)",
                            res.error_msg, attempt + 1, self.max_retries)
            except Exception as exc:  # pragma: no cover - network
                log.error("login raised: %s", exc)
            time.sleep(2 ** attempt)
        return False

    def logout(self) -> None:
        if self.logged_in:
            try:
                self.bs.logout()
            except Exception:  # pragma: no cover - network
                pass
            self.logged_in = False

    def _relogin(self, code: str) -> bool:
        log.warning("[%s] session looks expired, reconnecting", code)
        self.logout()
        return self.login()

    # -- data -------------------------------------------------------------
    def fetch_bars(self, code: str, end_date: str, days: int = 150) -> list[Bar] | None:
        """Daily bars, ascending, at most ``days`` rows, ending at ``end_date``."""

        start_date = (
            datetime.strptime(end_date, "%Y-%m-%d") - timedelta(days=days * 2)
        ).strftime("%Y-%m-%d")
        adjustflag = ADJUST_FLAGS[self.adjust]
        reconnects = 0

        for attempt in range(self.max_retries):
            try:
                time.sleep(self.request_delay + 2 ** attempt)
                rs = self.bs.query_history_k_data_plus(
                    code, FIELDS, start_date=start_date, end_date=end_date,
                    frequency="d", adjustflag=adjustflag,
                )
                if rs.error_code != "0":
                    msg = (rs.error_msg or "").lower()
                    if any(k in msg for k in ("login", "session", "expired", "未登录", "令牌")):
                        if reconnects < 2 and self._relogin(code):
                            reconnects += 1
                            continue
                        return None
                    if rs.error_code.startswith("10001"):
                        return None  # unknown/invalid code, not a transient error
                    log.warning("[%s] query error: %s", code, rs.error_msg)
                    continue

                rows: list[list[str]] = []
                while rs.error_code == "0" and rs.next():
                    rows.append(rs.get_row_data())
                if not rows:
                    return None

                bars: list[Bar] = []
                for row in rows:
                    record = dict(zip(FIELDS.split(","), row))
                    try:
                        close = float(record["close"])
                        volume = float(record["volume"])
                    except (TypeError, ValueError):
                        continue
                    bars.append(Bar(date=record["date"], close=close, volume=volume))
                bars.sort(key=lambda b: b.date)
                return bars[-days:]

            except (socket.timeout, ConnectionResetError, OSError) as exc:
                log.warning("[%s] network %s (retry %d/%d)",
                            code, type(exc).__name__, attempt + 1, self.max_retries)
                if attempt == self.max_retries - 1 and reconnects < 2:
                    if self._relogin(code):
                        reconnects += 1
                        continue
                if attempt == self.max_retries - 1:
                    return None
            except Exception as exc:  # pragma: no cover - network
                log.error("[%s] unexpected error: %s", code, exc)
                return None
        return None

    def stock_name(self, code: str) -> str:
        for attempt in range(self.max_retries):
            try:
                rs = self.bs.query_stock_basic(code=code)
                if rs.error_code == "0" and rs.next():
                    return rs.get_row_data()[1]
            except Exception:  # pragma: no cover - network
                pass
            time.sleep(0.5 * (attempt + 1))
        return "未知"

    def all_codes(self, day: str | None = None) -> list[tuple[str, str]]:
        day = day or datetime.now().strftime("%Y-%m-%d")
        rs = self.bs.query_all_stock(day=day)
        found: dict[str, str] = {}
        while rs.error_code == "0" and rs.next():
            row = rs.get_row_data()
            code = row[0]
            if code.startswith(("sh.6", "sz.0", "sz.3", "bj.8", "bj.4")):
                found[code] = row[2] if len(row) > 2 else ""
        return [(code, found[code]) for code in normalize_codes(found)]


# -- orchestration ---------------------------------------------------------
def _read_pool(path: str | os.PathLike[str], column: str | None = None) -> tuple[list[str], dict[str, str]]:
    with Path(path).open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            return [], {}
        cols = list(reader.fieldnames)
        code_col = column if column in cols else cols[0]
        name_col = "name" if "name" in cols else ("名称" if "名称" in cols else None)
        codes, names = [], {}
        for row in reader:
            code = str(row.get(code_col, "")).strip()
            if code:
                codes.append(code)
                if name_col:
                    names[code] = str(row.get(name_col, "")).strip()
        return codes, names


def _run_with_timeout(source: BaoStockSource, code: str, date: str, days: int):
    try:
        from func_timeout import func_timeout
    except ImportError:  # pragma: no cover - optional dependency
        return source.fetch_bars(code, date, days)
    return func_timeout(source.stock_timeout, source.fetch_bars, args=(code, date, days))


def run_screen(
    pool: str | os.PathLike[str],
    date: str,
    *,
    config: Config = Config(),
    out_dir: str | os.PathLike[str] = "output",
    cache_dir: str | os.PathLike[str] = "cache",
    adjust: str = "forward",
    use_cache: bool = True,
    code_column: str | None = None,
    limit: int | None = None,
) -> list[dict[str, object]]:
    """Screen a pool for one trading day. Returns the selected rows."""

    codes_raw, names = _read_pool(pool, code_column)
    codes = normalize_codes(codes_raw)
    if limit:
        codes = codes[:limit]
    if not codes:
        log.warning("empty pool: %s", pool)
        return []

    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    ckpt = checkpoint_path(cache_dir, date)
    processed_file = processed_path(cache_dir, date)

    rows: list[dict] = []
    processed: set[str] = set()
    if use_cache:
        rows = reusable_rows(load_rows(ckpt), date)
        processed = {str(r.get("股票代码", "")).strip() for r in rows} - {""}
        if processed_file.exists():
            processed |= {ln.strip() for ln in processed_file.read_text("utf-8").splitlines() if ln.strip()}
        if rows:
            log.info("resuming: %d rows from %s", len(rows), ckpt.name)
    else:
        for f in (ckpt, processed_file):
            if f.exists():
                f.unlink()

    remaining = [c for c in codes if c not in processed]
    log.info("pool %d codes, %d already done, %d to fetch", len(codes), len(processed), len(remaining))

    source = BaoStockSource(adjust=adjust)
    if not source.login():
        log.error("login failed; aborting")
        return []

    verdicts: list[Verdict] = []
    try:
        for idx, code in enumerate(remaining, 1):
            try:
                bars = _run_with_timeout(source, code, date, 150)
            except Exception as exc:
                # FunctionTimedOut included: one slow stock must not stop the run.
                log.warning("[%s] skipped: %s", code, type(exc).__name__)
                bars = None
            finally:
                with processed_file.open("a", encoding="utf-8") as fh:
                    fh.write(code + "\n")
                processed.add(code)

            if bars:
                verdict = evaluate(bars, config, code=code)
                if verdict.date != date:
                    log.info("[%s] latest bar is %s, not %s (suspension/holiday)",
                             code, verdict.date, date)
                    verdict.stale = True
                verdicts.append(verdict)
                if verdict.selected:
                    row = to_row(verdict, config, name=names.get(code, ""), analysis_date=date)
                    rows.append(row)
                    log.info("selected %s score=%d", code, verdict.score)

            if idx % 20 == 0:
                _write_rows(ckpt, filter_stale(rows, date))
                log.info("checkpoint: %d selected so far", len(rows))

        _write_rows(ckpt, filter_stale(rows, date))
    finally:
        source.logout()

    picks = select(verdicts)
    log.info("done: %d selected of %d evaluated", len(picks), len(verdicts))

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    final = out_dir / f"五线多头_{date}_{stamp}.csv"
    _write_rows(final, rows)
    log.info("wrote %s", final)
    return rows


def _write_rows(path: Path, rows: Sequence[dict]) -> None:
    if not rows:
        return
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def build_pool(path: str | os.PathLike[str], *, adjust: str = "forward") -> None:
    source = BaoStockSource(adjust=adjust)
    if not source.login():
        raise SystemExit("login failed")
    try:
        pairs = source.all_codes()
    finally:
        source.logout()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with Path(path).open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["code", "name"])
        writer.writerows(pairs)
    log.info("wrote %d codes to %s", len(pairs), path)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Five-MA bullish-alignment screener")
    sub = parser.add_subparsers(dest="cmd", required=True)

    pool = sub.add_parser("build-pool", help="write a full-market code list")
    pool.add_argument("--out", default="stock_pool.csv")

    run = sub.add_parser("screen", help="screen a pool for one day")
    run.add_argument("--pool", required=True)
    run.add_argument("--date", required=True)
    run.add_argument("--out", default="output")
    run.add_argument("--cache", default="cache")
    run.add_argument("--column", default=None, help="code column name")
    run.add_argument("--adjust", choices=sorted(ADJUST_FLAGS), default="forward")
    run.add_argument("--volume-ratio", type=float, default=Config().volume_ratio)
    run.add_argument("--max-rise", type=float, default=Config().max_rise)
    run.add_argument("--no-cache", action="store_true")
    run.add_argument("--limit", type=int, default=None)
    run.add_argument("-v", "--verbose", action="store_true")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    if args.cmd == "build-pool":
        build_pool(args.out)
        return 0

    config = Config(volume_ratio=args.volume_ratio, max_rise=args.max_rise)
    run_screen(
        args.pool, args.date, config=config, out_dir=args.out, cache_dir=args.cache,
        adjust=args.adjust, use_cache=not args.no_cache, code_column=args.column,
        limit=args.limit,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
