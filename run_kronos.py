#!/usr/bin/env python3
"""CPU-only Kronos ensemble. All output timestamps are UTC candle OPEN times."""
import argparse
from datetime import datetime, timezone
from functools import lru_cache
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import random
import re
import subprocess
import sys
import time
import resource
from market_sources import BY_ID, fetch_yahoo, future_timestamps

ROOT = Path(__file__).resolve().parent
os.environ['HF_HOME'] = str(ROOT / '.cache/huggingface')
os.environ['MPLCONFIGDIR'] = str(ROOT / '.cache/matplotlib')
os.environ['XDG_CACHE_HOME'] = str(ROOT / '.cache')
os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'
os.environ['HF_HUB_DISABLE_XET'] = '1'
os.environ['CUDA_VISIBLE_DEVICES'] = ''
TF = {'5m': 300, '15m': 900, '1h': 3600, '4h': 14400, '1d': 86400}
COLS = ['open', 'high', 'low', 'close', 'volume', 'amount']


def arguments():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--symbol', default='BTCUSDT')
    p.add_argument('--timeframe', choices=TF, default='1h')
    p.add_argument('--lookback', type=int, default=400)
    p.add_argument('--pred_len', type=int, default=24)
    p.add_argument('--samples', type=int, default=20)
    p.add_argument('--model', choices=['small', 'mini'], default='small')
    p.add_argument('--threads', type=int, default=4)
    p.add_argument('--seed', type=int, default=42)
    p.add_argument('--no-plot', action='store_true')
    a = p.parse_args()
    a.symbol = a.symbol.upper().replace('/', '')
    if not re.fullmatch(r'[A-Z0-9]{3,30}', a.symbol): p.error('Invalid symbol')
    if a.symbol in BY_ID and BY_ID[a.symbol]['source'] == 'yahoo' and a.timeframe not in ['15m', '1h', '4h', '1d']:
        p.error('Yahoo instruments support 15m, 1h, 4h, 1d')
    limit = 512 if a.model == 'small' else 2048
    if not 32 <= a.lookback <= limit: p.error(f'lookback must be 32..{limit}')
    if not 1 <= a.pred_len <= 128: p.error('pred_len must be 1..128')
    if not 1 <= a.samples <= 50: p.error('samples must be 1..50')
    if not 1 <= a.threads <= 6: p.error('threads must be 1..6')
    return a


def fetch_candles(symbol, timeframe, lookback):
    """Binance spot, closed candles only; paginate for mini's 2048 context."""
    import numpy as np
    import pandas as pd
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    base = 'https://data-api.binance.vision'
    with requests.Session() as session:
        session.mount('https://', HTTPAdapter(max_retries=Retry(
            total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])))
        def get(path, params=None):
            r = session.get(base + path, params=params, timeout=(10, 30))
            r.raise_for_status()
            return r.json()
        now_ms = int(get('/api/v3/time')['serverTime'])
        end = now_ms
        rows = []
        while len(rows) < lookback:
            page = get('/api/v3/klines', {'symbol': symbol, 'interval': timeframe,
                       'limit': min(1000, lookback - len(rows) + 1), 'endTime': end})
            if not isinstance(page, list) or not page: break
            rows.extend(r for r in page if int(r[6]) < now_ms)
            new_end = int(page[0][0]) - 1
            if new_end >= end: raise ValueError('Non-progressing pagination')
            end = new_end
    unique = {int(r[0]): r for r in rows}
    ordered = sorted(unique.values(), key=lambda r: int(r[0]))[-lookback:]
    if len(ordered) != lookback: raise ValueError('Insufficient closed candles')
    df = pd.DataFrame([dict(timestamps=pd.to_datetime(r[0], unit='ms', utc=True),
                          **dict(zip(COLS, map(float, r[1:6] + [r[7]])))) for r in ordered])
    values = df[COLS].to_numpy()
    if not np.isfinite(values).all(): raise ValueError('Non-finite input')
    if (df[COLS[:4]] <= 0).any().any() or (df[COLS[4:]] < 0).any().any():
        raise ValueError('Invalid price or volume')
    if ((df.high < df[['open', 'close', 'low']].max(axis=1)) |
            (df.low > df[['open', 'close', 'high']].min(axis=1))).any():
        raise ValueError('Invalid input OHLC geometry')
    step = pd.Timedelta(seconds=TF[timeframe])
    if not df.timestamps.diff().iloc[1:].eq(step).all(): raise ValueError('Missing candles')
    expected_open = (now_ms // (TF[timeframe] * 1000) - 1) * TF[timeframe] * 1000
    if df.timestamps.iloc[-1] != pd.to_datetime(expected_open, unit='ms', utc=True):
        raise ValueError('Stale market data: latest closed candle is absent')
    return df, now_ms, base


@lru_cache(maxsize=1)
def load_predictor(size):
    from huggingface_hub import snapshot_download
    sys.path.insert(0, str(ROOT / 'Kronos'))
    from model import Kronos, KronosTokenizer, KronosPredictor
    tok_id = 'NeoQuasar/Kronos-Tokenizer-' + ('base' if size == 'small' else '2k')
    model_id = 'NeoQuasar/Kronos-' + size
    paths = []
    for repo in [tok_id, model_id]:
        try:
            cached = snapshot_download(repo_id=repo, local_files_only=True)
            if not list(Path(cached).glob('*.safetensors')): raise FileNotFoundError(repo)
            paths.append(cached)
        except (OSError, ValueError):
            paths.append(snapshot_download(repo_id=repo, allow_patterns=['*.json', '*.safetensors']))
    tokenizer = KronosTokenizer.from_pretrained(paths[0])
    model = Kronos.from_pretrained(paths[1])
    tokenizer.eval()
    model.eval()
    predictor = KronosPredictor(model, tokenizer, device='cpu',
                               max_context=512 if size == 'small' else 2048)
    return predictor, {'tokenizer': tok_id, 'model': model_id,
                       'tokenizer_revision': Path(paths[0]).name,
                       'model_revision': Path(paths[1]).name}


def summarize(paths, last_close):
    import numpy as np
    close = paths[:, :, 3]
    returns = close[:, -1] / last_close - 1
    signs = np.sign(returns)
    up, down = float(np.mean(signs > 0)), float(np.mean(signs < 0))
    agreement = max(up, down)
    deltas = np.diff(np.column_stack([np.full(len(close), last_close), close]), axis=1)
    distance = np.abs(deltas).sum(axis=1)
    efficiency = np.divide(np.abs(close[:, -1] - last_close), distance,
                           out=np.zeros_like(distance), where=distance > 0)
    mean_eff = float(efficiency.mean())
    regime = ('mixed' if len(paths) < 2 else
              'trending' if agreement >= .75 and mean_eff >= .35 else
              'choppy' if mean_eff <= .20 else 'mixed')
    return dict(regime=regime, direction_agreement=agreement,
                mean_direction=float(signs.mean()), up_fraction=up, down_fraction=down,
                flat_fraction=float(np.mean(signs == 0)), mean_path_efficiency=mean_eff,
                mean_terminal_return=float(returns.mean()),
                terminal_return_p10=float(np.quantile(returns, .1)),
                terminal_return_p90=float(np.quantile(returns, .9)),
                classification='heuristic of model samples; not calibrated probabilities',
                thresholds={'trending_agreement': .75, 'trending_efficiency': .35,
                            'choppy_efficiency': .20})


_THREADS_READY = False


def main(a=None, progress=None, data_override=None):
    global _THREADS_READY
    a = arguments() if a is None else a
    def report(message):
        print(message, flush=True)
        if progress: progress(message)
    if sys.version_info < (3, 10): raise RuntimeError('Python 3.10+ required; run setup_kronos.sh')
    os.environ['OMP_NUM_THREADS'] = str(a.threads)
    os.environ['MKL_NUM_THREADS'] = str(a.threads)
    import numpy as np
    import pandas as pd
    import psutil
    import torch
    torch.set_num_threads(a.threads)
    if not _THREADS_READY:
        torch.set_num_interop_threads(1)
        _THREADS_READY = True
    random.seed(a.seed)
    np.random.seed(a.seed)
    torch.manual_seed(a.seed)
    started = datetime.now(timezone.utc)
    start = time.monotonic()
    instrument = BY_ID.get(a.symbol, dict(id=a.symbol, label=a.symbol, source='binance'))
    report('Loading closed candles…')
    if data_override is not None:
        df, data_meta = data_override
    elif instrument['source'] == 'yahoo':
        df, data_meta = fetch_yahoo(instrument, a.timeframe, a.lookback)
    else:
        df, server_ms, source = fetch_candles(a.symbol, a.timeframe, a.lookback)
        data_meta = dict(source=source, market='Binance spot', binance_server_time_ms=server_ms,
                         calendar='24/7', warnings=[], amount_semantics='quote_turnover', volume_semantics='base_asset')
    report(f'Data: {len(df)} closed candles, last open={df.timestamps.iloc[-1]}')
    report('Loading the model into memory…')
    predictor, model_meta = load_predictor(a.model)
    report(f'Loaded {model_meta["model"]}, CPU, threads={a.threads}')
    if instrument['source'] == 'yahoo':
        y = future_timestamps(df, instrument, a.timeframe, a.pred_len)
    else:
        y = pd.Series(pd.date_range(df.timestamps.iloc[-1], periods=a.pred_len + 1,
                                   freq=pd.Timedelta(seconds=TF[a.timeframe]))[1:])
    paths = []
    # predict(sample_count=N) returns only a mean. N calls with count=1 retain paths
    # and bound activation memory; model/tokenizer remain loaded throughout.
    with torch.inference_mode():
        for i in range(a.samples):
            if psutil.virtual_memory().available < 1.5 * 1024**3:
                raise MemoryError('Less than 1.5 GiB available RAM')
            tick = time.monotonic()
            result = predictor.predict(df=df[COLS], x_timestamp=df.timestamps,
                                       y_timestamp=y, pred_len=a.pred_len, T=1.0,
                                       top_p=.9, sample_count=1, verbose=False)
            arr = result[COLS].to_numpy(dtype=float)
            if arr.shape != (a.pred_len, 6) or not np.isfinite(arr).all():
                raise ValueError('Invalid model output')
            paths.append(arr)
            rss = psutil.Process().memory_info().rss / 1024**3
            report(f'Sample {i+1}/{a.samples}: {time.monotonic()-tick:.1f}s, RSS={rss:.2f} GiB')
            if i == 0 and (time.monotonic() - tick) * a.samples > 600:
                print('Slow CPU run. Faster option: --model mini --samples 5', flush=True)
    paths = np.stack(paths)
    invalid_prices = ((paths[:, :, :4] <= 0).any(axis=2) |
               (paths[:, :, 1] < paths[:, :, [0, 2, 3]].max(axis=2)) |
               (paths[:, :, 2] > paths[:, :, [0, 1, 3]].min(axis=2)))
    invalid_volume = (paths[:, :, 4:] < 0).any(axis=2)
    invalid = invalid_prices | invalid_volume
    forecast = pd.DataFrame(paths.mean(axis=0), columns=COLS)
    forecast.insert(0, 'timestamps', y)
    run_id = started.strftime('%Y%m%dT%H%M%S%fZ') + '_' + a.symbol + '_' + a.timeframe
    out = ROOT / 'signals' / run_id
    out.mkdir(parents=True, exist_ok=False)
    df.to_csv(out / 'input.csv', index=False)
    forecast.to_csv(out / 'forecast.csv', index=False)
    frames = []
    for i, arr in enumerate(paths):
        f = pd.DataFrame(arr, columns=COLS)
        f.insert(0, 'timestamps', y)
        f.insert(0, 'sample_id', i)
        frames.append(f)
    pd.concat(frames).to_csv(out / 'trajectories.csv', index=False)
    try:
        commit = subprocess.check_output(['git', '-C', str(ROOT / 'Kronos'),
                                          'rev-parse', 'HEAD'], text=True).strip()
    except (OSError, subprocess.CalledProcessError): commit = None
    meta = dict(schema_version='bot-world.kronos.v2', run_id=run_id,
                started_at=started.isoformat(), completed_at=datetime.now(timezone.utc).isoformat(),
                symbol=a.symbol, timeframe=a.timeframe, lookback=a.lookback,
                pred_len=a.pred_len, sample_count=a.samples, device='cpu', threads=a.threads,
                seed=a.seed, temperature=1.0, top_p=.9, sampling_batch_size=1,
                **data_meta, instrument_label=instrument['label'],
                last_observed_open=df.timestamps.iloc[-1].isoformat(), timestamp_semantics=('UTC session date label' if instrument['source'] == 'yahoo' and a.timeframe == '1d' else 'UTC candle open'),
                input_sha256=hashlib.sha256((out / 'input.csv').read_bytes()).hexdigest(),
                repository_commit=commit, **model_meta,
                forecast_quality={'invalid_raw_candle_count': int(invalid.sum()),
                                  'invalid_price_candle_count': int(invalid_prices.sum()),
                                  'negative_volume_or_amount_count': int(invalid_volume.sum()),
                                  'usable_ohlc': bool(not invalid_prices.any()),
                                  'usable_ohlcv': bool(not invalid.any()), 'repair_applied': False},
                metrics=summarize(paths, float(df.close.iloc[-1])),
                elapsed_seconds=time.monotonic() - start,
                process_peak_rss_gib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024**3 if sys.platform == 'darwin' else 1024**2),
                versions={p: importlib.metadata.version(p) for p in
                          ['torch', 'numpy', 'pandas', 'huggingface_hub', 'safetensors']},
                disclaimer='Model scenarios, not a trading system or validated market edge.')
    if not a.no_plot:
        try:
            import matplotlib
            matplotlib.use('Agg')
            import matplotlib.pyplot as plt
            fig, ax = plt.subplots(figsize=(12, 5))
            hist = df.tail(100)
            ax.plot(hist.timestamps, hist.close, color='black', label='Observed close')
            for arr in paths: ax.plot(y, arr[:, 3], color='steelblue', alpha=.15)
            ax.plot(y, forecast.close, color='darkorange', label='Mean forecast close')
            ax.axvline(df.timestamps.iloc[-1], color='grey', linestyle='--')
            ax.set_title(f'{a.symbol} {a.timeframe} | Kronos-{a.model} | UTC')
            ax.legend()
            fig.autofmt_xdate()
            fig.tight_layout()
            fig.savefig(out / 'forecast.png', dpi=140)
            plt.close(fig)
        except Exception as exc: meta['plot_error'] = str(exc)
    (out / 'metadata.json').write_text(json.dumps(meta, indent=2, allow_nan=False) + '\n')
    print(forecast.head().to_string(index=False))
    print(json.dumps(meta, indent=2, allow_nan=False))
    report(f'Saved: {out}')
    return out


if __name__ == '__main__':
    try:
        main()
    except (Exception, KeyboardInterrupt) as exc:
        print(f'ERROR: {type(exc).__name__}: {exc}', file=sys.stderr)
        if isinstance(exc, MemoryError) or 'out of memory' in str(exc).lower():
            print('Retry: --model mini --lookback 400 --pred_len 24 --samples 5', file=sys.stderr)
        sys.exit(1)
