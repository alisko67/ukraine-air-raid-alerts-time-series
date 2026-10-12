# Time Series Analysis of Air Raid Alerts in Ukraine

An educational project: collect historical air raid alert data, store it in SQLite, build an hourly time
series, compute descriptive statistics, and compare a naive baseline with one statistical model (AR(1)) on a
held-out, most recent segment. A separate command shows the history of any single territory (oblast, district,
community or city).

> **Not for safety decisions.** This is a study of a short historical sample. It does not predict real threats
> and does not prove the quality of any model (see [Limitations](#7-limitations)).

User-facing program output (reports, plot labels, CLI messages) is in Ukrainian; code and this README are in English.

## 1. Data source

- **tryvoha.online API** (`https://tryvoha.online/api/v1`) — public JSON API, no key required
  (docs: <https://tryvoha.online/api>). Terms: free to use, **credit Tryvoha.online**; the service is unofficial,
  carries no availability guarantee; rate limit 120 requests/min per IP; responses are cached for 30–60 s.
- Endpoints used (all verified with real requests): `GET /regions`, `GET /alerts`,
  `GET /stats/{slug}?period=24h|7d|30d`.
- Why not the official map (map.ukrainealarm.com): it only shows currently active alerts; its history endpoint
  (`getHistory`) requires authorization (HTTP 401) and the official API sits behind a bot check and is issued by
  key. Neither authorization nor bot protection was bypassed.
- What the API provides per episode: `started_at`, `ended_at` (`null` while ongoing), region, and `level`
  (`red`/`yellow`, only from `/alerts`). There is no per-episode threat type (`threats` exist only in aggregates).

## 2. Quick start

Requires Python 3.11+ (tested on 3.12; pandas 3.0.6, matplotlib 3.11.2, statsmodels 0.15.0, numpy 2.5.4).

```bash
pip install -r requirements.txt     # tzdata is needed on Windows for Europe/Kyiv
```

**A. Reproduce the published results (no network).** The repository ships a frozen snapshot of the database
(`data/alarms.sqlite`, collected 2026-10-12 ~02:30 Kyiv time), so the numbers below can be regenerated:

```bash
python report.py                    # row counts and data-quality checks
python analysis.py                  # national analysis: series, statistics, forecast, plots -> outputs/
python region.py --list             # all territories with a data-status column
python region.py kyiv               # one territory: report + plot -> outputs/regions/
python -m unittest                  # 26 offline tests
```

**B. Collect fresh data.** Note that `backfill` updates the target database and moves the data window, so
results will differ from the published ones (use a different file with `--db` to keep the snapshot intact):

```bash
python collect.py --db data/fresh.sqlite backfill     # ~2 min: regions, /alerts, /stats for 154 regions
python report.py --db data/fresh.sqlite
python analysis.py --db data/fresh.sqlite --out outputs_fresh
```

`python collect.py poll` is an optional long-running collector (a `/alerts` snapshot every 60 s). It fills gaps in
the event log and produces real `end_unconfirmed` statuses; it was not used for the results below.

| File | Purpose |
|---|---|
| `timeutil.py` | UTC ↔ `Europe/Kyiv` conversion (DST handled automatically) |
| `client.py` | HTTP client: request pacing, retries, empty/invalid response handling, structure checks |
| `db.py` | SQLite schema, upserts without duplicates, episode statuses, suspicious-record flags |
| `collect.py` | `backfill`, `refresh`, `poll` commands |
| `report.py` | database contents and data-quality report |
| `analysis.py`, `plots.py` | national analysis: series, statistics, baseline/AR(1), report, plots |
| `region.py` | single-territory selection, statistics, report and plot (`plots.plot_region`) |
| `test_pipeline.py` | offline tests |

## 3. Methodology

**Storage.** API timestamps are stored in UTC, unchanged; the receive time of every snapshot is stored
separately (`snapshots`). An episode is identified by `(slug, started_at)` (the API gives no event id).
`/alerts` and `/stats` sometimes differ by 1 s in `started_at`, so keys are matched with a 2 s tolerance (the
first-seen timestamp is kept, the other goes to `started_at_alt`). Statuses: `active`, `ended`,
`end_unconfirmed` (disappeared from the active list without an `ended_at` — this is **not** proof of an end).
Episodes longer than 24 h are only flagged (`suspect`), never deleted.

**Metric (one).** The number of "leaf" territorial units (districts, communities, cities/oblasts without
districts — 131 units) with an alert during the hour. Oblasts that have districts are not counted: their episodes
are the union of district episodes, so including them would double count. The series is hourly; hour boundaries
in UTC and Kyiv time coincide.

**History completeness.** The `recent` event log holds at most 20 latest episodes per region. For each unit the
moment from which its history is complete is known; earlier values are only a lower bound. Hence two series:
- **Primary series**: all units, only hours where history is complete for at least 95% of units (threshold fixed
  in advance);
- **Control series**: a fixed panel of 80 units whose history is complete for the whole 72 h. The panel is biased
  towards quieter regions and does not represent the country; it only checks the robustness of the conclusion.

Four stuck episodes (>24 h with no known end: Luhansk oblast since 2022, Lypetska community since March,
Pokrovska and Chervonohryhorivka communities since 17 Sep) are excluded from the series. This is a constant
level shift that barely affects forecast errors but does shift the mean and maximum.

**Forecast.** Target: the value of the next hour (horizon 1 h). Time-based split, rolling origin: each test hour
uses only earlier hours. Methods: `naive` (last value), `mean` (mean of known hours), `AR(1)` with a constant
(`statsmodels` ARIMA, refit at every step). Model order and test sizes (12 h for the primary series, 24 h for the
control series) were fixed before looking at the metrics. Metrics: MAE and RMSE.

## 4. Results (data as of 2026-10-12 02:30 Kyiv time, input fingerprint `b0ee17b78f7975dd`)

History is complete for at least 95% of units only from 2026-10-10 11:00 Kyiv time, so the primary series has
39 hours (27 train, 12 test).

| Series | naive MAE | mean MAE | AR(1) MAE | AR(1) better than naive |
|---|---|---|---|---|
| Primary (131 units, 39 h) | 10.17 | 11.34 | 9.64 | 7 of 12 h |
| Control (80 units, 72 h) | 3.08 | 4.01 | 3.40 | 12 of 24 h |

Descriptive (primary series): on average 58 units under alert per hour (min 35, max 84); lag-1 autocorrelation ≈ 0.50.

**Conclusion.** The direction differs between the two series: AR(1) is slightly better than the baseline on the
primary series and slightly worse on the control series. An advantage of the more complex method is **not
confirmed**; `mean` is worse on both. The sample (12 and 24 test points) is far too small to call any difference
significant.

Plots: `outputs/01_series.png` (series + share of units with complete history; the incomplete part is shaded) and
`outputs/02_forecasts.png` (actual, baseline and AR(1) on the test segment).

## 5. Single-territory view

```bash
python region.py --list [query] [--type state|district|community] [--oblast SLUG]   # [] = optional parts
python region.py kyiv                      # slug or part of a name (Ukrainian or English)
python region.py kupianskii-raion --hours 72    # window up to 168 h (the API has no deeper history)
python region.py kharkiv --pick 2          # pick by list number when several match (otherwise it asks)
```

All 154 territories from `/regions` are available (23 oblasts with districts, 4 oblasts/cities without districts
in the data, 118 districts, 9 communities). Output: `outputs/regions/<slug>.txt` (report), `<slug>.png` (plot),
`<slug>_hourly.csv` (minutes under alert per hour, UTC and Kyiv time). There is no web/GUI front end: it is a
terminal program and the plot is saved as an image file.

**Levels are never mixed.** Only the territory's own log (its slug) is used. Data of child or parent territories is
never added or substituted. An oblast's episodes are, per the API, the union of its districts' episodes ("an alert
somewhere in the oblast"), not a count of districts, and are not summed with district data. No forecast is built
for a single territory (too few events).

**Reliability varies by territory:**
- 20 territories have no episode at all in the database and 22 leaf units have `alerts.count = 0` in `/stats`.
  These include cities (Kharkiv, Kherson, Kramatorsk, Marhanets, Zaporizhzhia). Only for Kharkiv city is it
  confirmed that this is suspicious: the official map showed an alert there during source research while Tryvoha
  has no episode for that slug. For such territories the absence of records is **not** reported as "0% of time under
  alert": history is considered known only from our first `/alerts` snapshot, and the report says so.
- The event log is truncated for 72 districts and 17 oblasts: full history is known only from some moment on
  (shaded in the plot).
- Dnipropetrovsk oblast has a single merged episode since 17 Sep (flagged), Luhansk oblast a record since 2022.
  They are shown as they are, flagged; the share of time is given both with and without them.

## 6. Verification

| Check | Result |
|---|---|
| `python -m unittest` | 26 tests pass: deduplication, `ended_at` updates, `end_unconfirmed`, a failed request does not change status, 1 s key tolerance, client retries/empty responses, hour-overlap boundaries, DST transitions (2026-03-29, 2026-10-25), no future leakage in the rolling forecast, territory search, minutes per hour, levels not mixed, "untracked" ≠ calm, truncated log, suspicious episodes |
| Data leakage | a test changes "future" values (+1000) and confirms forecasts for earlier hours do not change |
| Database integrity | `PRAGMA integrity_check` ok; all timestamps in UTC `…Z` format; no event starting after its first observation; snapshot times monotonic |
| Duplicates | repeated `backfill` creates none (exact keys: 0; near pairs < 120 s: 0) |
| Reproducibility | two runs of `analysis.py` on the same database give byte-identical `report.txt` and CSV files |
| End-to-end | fresh empty database → `backfill` (154 regions, 0 errors) → `report` → `analysis` → `region` all run; the README commands were executed literally, in order, in a clean copy |
| Per-territory numbers | for all 154 territories, minutes per hour, the complete-history boundary and own-slug-only episodes were recomputed independently: 0 discrepancies; `--type`/`--oblast` filters match direct SQL |
| National analysis stability | adding `region.py` left `report.txt` and `series_hourly.csv` byte-identical |

## 7. Limitations

- History is incomplete: the log is capped at ~20 episodes per region, so the busiest regions are known only for the
  last 1–2 days.
- Only 39 reliable hours (primary) and 72 (control, biased); weekday patterns and seasonality cannot be assessed.
- The data is third-party and unofficial; the endpoints disagreed in places (Zaporizhzhia city: `/alerts` shows an
  active alert while `/stats` returns `count = 0`).
- The assumption that leaf units do not overlap spatially was not verified.
- A missing record in the API is not proof that no alert happened or that it ended.
- The national analysis treats the 22 units with `alerts.count = 0` as "history complete, no alerts", because calm
  cannot be told apart from an untracked unit. Their observed contribution to the primary series is tiny (0.03 units
  on average), but if they were treated as unknown, no hour would reach the 95% completeness threshold (maximum
  83%), so the length of the primary series depends on this assumption. Unlike the single-territory view, this
  behaviour was intentionally left unchanged.
- Reproducing the numbers requires the shipped database snapshot; fresh data shifts the window and the figures.

## 8. Development log (how the approach changed)

1. **Source research.** The official map shows only active alerts; its history endpoint needs authorization, and
   the official API is behind a bot check, so history for the last three days could not be obtained legitimately.
   Tryvoha.online was then evaluated: public, key-less, and it exposes `started_at`/`ended_at`.
2. **API limits discovered by real requests.** Periods are only `24h|7d|30d` (no 72 h), and the event log is capped at
   20 episodes per region. The plan changed from "download 72 h" to "collect what is available and track exactly how
   complete it is for each region".
3. **Storage.** SQLite with a composite `(slug, started_at)` key. Real data revealed a 1 s disagreement between
   `/alerts` and `/stats` (it would have created duplicates) → tolerant key matching. Snapshots and an active-log
   table were added so that disappearance from `/alerts` is recorded as `end_unconfirmed`, not as an end.
4. **Analysis design.** Hourly coverage showed full history only for the last ~31–39 h. Rather than present
   under-counted early hours as data (which would fake a rising trend), a primary series with a coverage threshold
   was fixed in advance, plus a control panel series as a robustness check.
5. **Forecast.** One baseline family and one model (AR(1)) with rolling-origin evaluation and an explicit leakage
   test. The two series disagree on whether AR(1) helps, so no advantage is claimed.
6. **Single-territory view.** Survey of all 154 territories found 22 with zero counts, merged/stuck episodes and
   `/alerts` vs `/stats` disagreements; the view therefore never reports absence of records as calm, and never mixes
   geographic levels. Final checks also found and fixed a Windows encoding crash when output is redirected, and two
   display bugs (`NaN` treated as truthy; a misplaced "incomplete history" marker).

## 9. Data attribution

Alert data comes from [Tryvoha.online](https://tryvoha.online) and must be credited when reused, per its terms.
The service is informational and not an official alert channel.
