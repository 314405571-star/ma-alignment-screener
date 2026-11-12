"""Five-line bullish-alignment screener for A-shares."""

from .alignment import Bar, Config, Verdict, evaluate, rank, select, simple_ma, to_row

__all__ = ["Bar", "Config", "Verdict", "evaluate", "rank", "select", "simple_ma", "to_row"]
__version__ = "1.0.0"
