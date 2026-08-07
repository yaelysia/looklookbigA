# Market data history

This branch is maintained automatically by GitHub Actions.

- `history/daily_k/`: persistent per-symbol daily K-line cache used by realtime analysis.
- `history/snapshots/`: compact intraday snapshots retained for later comparison.
- `history/manifest.json`: latest persisted history metadata.

Code changes belong on `master`; this branch stores generated market data only.
