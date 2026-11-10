# Data contract

| Instrument ID | Source | Provider symbol | Meaning |
|---|---|---|---|
| BTCUSDT | Binance Spot | BTCUSDT | Bitcoin quoted in USDT |
| SP500 | Yahoo Finance | ^GSPC | S&P 500 index value |
| NASDAQ100 | Yahoo Finance | ^NDX | Nasdaq-100 index value |
| EURUSD | Yahoo Finance | EURUSD=X | Currency pair |
| GOLD | Yahoo Finance | GC=F | Rolling gold futures series, USD/oz |
| WTI | Yahoo Finance | CL=F | Rolling WTI futures series, USD/barrel |

Yahoo's public chart endpoint is unofficial and has no availability guarantee. This repository contains no raw provider history, saved market forecasts or weights.

## Candles

Only closed candles are used. Four-hour bars require four adjacent hourly bars; gaps and incomplete session tails are not stitched. Daily Yahoo timestamps label the UTC session date, not a verified market-open time. The source's current date is excluded entirely.

Missing Yahoo FX/index volume is passed as volume=0. Yahoo turnover is passed as amount=0. These are explicit missing-data conventions, not measured traded volume or turnover. Model quality under those conventions is not established.

Future session dates account for typical weekly hours and time zones/DST, but not a complete exchange holiday calendar. Crypto uses a 24/7 calendar. Rolling futures can jump at contract changes. None of the series is silently treated as a broker CFD.

## Output semantics

Each predictor call produces one retained scenario. The workbench computes the mean across retained paths. CSV timestamps are UTC; the browser renders its labels in the computer's local time zone.

metadata.json records the input hash, parameters, provider semantics, model references, package versions, elapsed time and consistency/quality checks. Individual paths are kept in trajectories.csv.

OHLC-invalid prices and negative volume/turnover are flagged separately. Raw forecasts are not silently repaired. A result marked unusable must not be treated as executable candle history.

Direction agreement and path efficiency are descriptive heuristics. The labels trending, choppy and mixed are not established market-regime probabilities. Scenario quantiles are not calibrated confidence intervals.
