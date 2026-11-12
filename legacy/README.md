# Legacy versions

These three files are the four code cells of
`notebooks/追涨中段因子.ipynb`, dumped verbatim. They are kept for two reasons:
the screenshots in the docs refer to them, and the tests in `tests/` name the
bugs they contain.

| File | Notebook cell (1-based) | What it does | What breaks |
|---|---|---|---|
| `five_line_v1_strict.py` | 1 | strict AND of the five conditions; outputs only full matches | rejects a perfectly linear trend (strict slope chain); unadjusted prices; date-blind `checkpoint.csv` |
| `five_line_v2_scored.py` | 2 | +10 per condition, max 50; outputs anything with score > 0 | the score replaces the AND: a sideways stock scores 30/50; volume baseline includes today; still date-blind |
| `five_line_v3_scored_cached.py` | 3 | v2 plus an explicit empty-set guard in `_load_processed_codes` and a top-50 print | same; the cache guard does not fix the date problem |

They are not imported by anything in `src/` and are not covered by the test
suite. They are the "before" picture.

Reproducing the central finding without the network — the linear ramp that v1
rejects and `src/alignment.py` accepts — is

```bash
python examples/demo_mid_rally.py
```

and the row labelled `Linear ramp`.
