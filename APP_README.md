# TCFC OMRC Dashboard

Pluggable Off-Market Rate Control dashboard. **Data Fetch is the source of truth:**
pull from EPE (exceptions) or BRV S3 (trades) once, cache it as a dataset, then
Threshold Analysis and Audit Sampling run against that cached dataset — the user
picks which dataset (and which source) a module uses.

```
Data Fetch ──pull──> [server-side dataset store] <──read── Threshold Analysis
   (EPE / S3)               (cached by id)         <──read── Audit Sampling
```

## Run it

Backend (Python 3.9+):
```bash
cd backend
python -m venv .venv && .venv\Scripts\activate     # Windows
pip install -r requirements.txt
copy .env.example .env
uvicorn app.main:app --reload --port 8000
```
Frontend:
```bash
cd frontend && npm install && npm run dev          # http://localhost:5173
```
With `USE_MOCK=true` it runs on synthetic data. Set `USE_MOCK=false` for live.

## Going live (EPE + S3)

Everything is config — no code edits to point at your environment. In `.env`:

- **EPE (REST):** `EPE_BASE_URL`, `EPE_EXCEPTIONS_PATH`, auth (`EPE_AUTH_SCHEME` =
  bearer/apikey/basic, `EPE_AUTH_HEADER`, `EPE_TOKEN`), and `EPE_RESPONSE_PATH`
  (the JSON key holding the row array).
- **BRV S3:** `S3_BUCKET`, `S3_REGION`, `S3_PREFIX_TEMPLATE`
  (`{product_type}` / `{date}` substituted per day), `S3_FORMAT` (parquet/csv).
  AWS creds come from the standard boto3 chain — never hardcoded.

Then map your real columns once in **`app/core/column_map.py`**
(`TRADE_MAP`, `EXCEPTION_MAP`). Point `bucket` at a rating/sub-product/tenor
column to calibrate per sub-bucket; otherwise it groups by product type. The
deviation metric per product type lives in `app/core/metrics.py`
(% for cash/equity, bps for rates, pips for FX) and
`CONFIRMED_STATUSES` (EPE statuses = confirmed off-market) in `column_map.py`.

## Modules & flow

**Data Fetch** — filters mirror the Streamlit `layout.py` (product type, legal
entities, source systems, dates; lists in `core/data_filters.py`). Each pull is
stored and listed under *Loaded datasets*.
- `POST /data-fetch/trades` · `POST /data-fetch/exceptions` → store, return id + preview
- `GET /data-fetch/datasets?source=BRV_S3|EPE` · `GET|DELETE /data-fetch/datasets/{id}`

**Threshold Analysis** — calibration & backtesting **dispatched by product type**.
Each product has a calibrator under `app/modules/threshold_analysis/calibrators/`:
- **GFX Cash** → volatility banding (hybrid): per-currency vol, USD pairs use the
  pair's own vol, crosses use a correlation-aware combination, deterministic vol
  groups, threshold = `k · vol` at a chosen horizon (daily fixes the annualization
  gap; annualized kept for RDR continuity).
- **GDM Cash Bonds** → composite-key, per-source: threshold key = Product Sub-Type
  + Rating Region + Tenor + Credit Rating + Issuer Rating + Notional Range
  (granularity selectable); one threshold **per price source** (BVAL/RDAM/Reuters/EOD)
  from the deviation distribution (percentile/MAD); alert if any source breaches (OR);
  sparse cells fall back to product level. Source prices read from the data for now
  (`SOURCE_COLS` in `cashbonds.py`) — swap in the separate market-data dump there.
- **Default** (other products) → per-check percentile / MAD with OR/AND/Combination.

To add a product: drop a calibrator module exposing `CALIBRATOR` (a `product_types`
list + `config/calibrate/backtest` returning the generic envelope). It's
auto-discovered and dispatched; the UI renders it from the envelope with no change.
Backtesting uses the selected **EPE** dataset (statuses APPROVED/ESCALATED) as
confirmed off-market ground truth.

**Threshold Analysis
- *Calibration*: `POST /calibrate {dataset_id, k, floor}` → Median + k·MAD per bucket.
- *Backtesting*: `POST /backtest {dataset_id, epe_dataset_id?, k, floor, baseline_k}`
  → flag volume over time (candidate vs baseline), and — if you select an EPE
  dataset for ground truth — precision/recall/F1 + confusion vs confirmed off-market.

**Audit Sampling** — pick an EPE dataset.
- `POST /sample {dataset_id, size, method, seed}` → seeded, reproducible sample.

## Adding a module (plugin contract)

Backend: drop a package in `app/modules/` exposing `META` + `router` — auto-mounted
at `/api/modules/{id}`. Frontend: drop `src/modules/<name>/index.jsx` exporting
`meta` + a default component — `registry.js` (`import.meta.glob`) wires nav + routes.
No edits to the shell on either side.

## Notes

- Dataset store is in-memory + a JSON copy per dataset under `DATA_DIR`, so
  datasets survive a backend reload during a session. Clear `DATA_DIR` to reset.
- All analysis is pure stdlib (statistics), so it runs on StatPy / Py3.9. `boto3`
  and `pyarrow` are only needed for live S3 reads.


## Business benefit (false-positive reduction)

EPE exceptions carry their resolution outcome, which is the real ground truth:
**L1-closed = false positive** (alert fired, found to be nothing) and
**L2-confirmed / escalated = true positive** (genuine off-market). These sets are
defined in `column_map.py` (`GENUINE_STATUSES`, `L1_FP_STATUSES`) — edit to match
your real EPE status values.

Backtesting (with an EPE dataset selected) reports, on the EPE alert population:
false positives eliminated by the candidate thresholds, genuine (L2) exceptions
retained, and estimated ops hours saved (`minutes_per_l1`). It also shows the
**join match rate** (EPE trade_ids found in the trade dataset) — a low rate
flags an ID-join problem and explains misleadingly low precision/recall.
