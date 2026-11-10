"""Market adapters. Yahoo is an unofficial public endpoint, without an SLA."""
from urllib.parse import quote
import time

INSTRUMENTS = [
    dict(id='BTCUSDT', label='Bitcoin', ticker='BTCUSDT', group='Crypto', source='binance', kind='Spot · USDT'),
    dict(id='SP500', label='S&P 500', ticker='^GSPC', group='Indices', source='yahoo', kind='Index · points', tz='America/New_York', anchor=570),
    dict(id='NASDAQ100', label='Nasdaq-100', ticker='^NDX', group='Indices', source='yahoo', kind='Index · points', tz='America/New_York', anchor=570),
    dict(id='EURUSD', label='EUR / USD', ticker='EURUSD=X', group='FX', source='yahoo', kind='Currency pair · USD', tz='UTC', anchor=0),
    dict(id='GOLD', label='Gold', ticker='GC=F', group='Commodities', source='yahoo', kind='GC futures · USD/oz', tz='America/New_York', anchor=1080),
    dict(id='WTI', label='WTI crude oil', ticker='CL=F', group='Commodities', source='yahoo', kind='CL futures · USD/barrel', tz='America/New_York', anchor=1080),
]
BY_ID = {i['id']: i for i in INSTRUMENTS}
SECONDS = {'15m': 900, '1h': 3600, '4h': 14400, '1d': 86400}
COLS = ['open', 'high', 'low', 'close', 'volume', 'amount']


def aggregate_four_hours(df, tz, anchor):
    """Full four consecutive hourly bars only, aligned to local session anchor.

    Partial session tails, gaps, and DST irregular blocks are discarded explicitly.
    No fabricated prices or overnight stitching.
    """
    import pandas as pd
    if df.empty: return df.copy()
    local = df.timestamps.dt.tz_convert(tz).dt.tz_localize(None)
    shifted = local - pd.Timedelta(minutes=anchor)
    keys = shifted.dt.floor('4h') + pd.Timedelta(minutes=anchor)
    rows = []
    for _, g in df.groupby(keys, sort=True):
        if len(g) != 4 or not g.timestamps.diff().iloc[1:].eq(pd.Timedelta(hours=1)).all(): continue
        rows.append(dict(timestamps=g.timestamps.iloc[0], open=g.open.iloc[0], high=g.high.max(),
                         low=g.low.min(), close=g.close.iloc[-1], volume=g.volume.sum(), amount=g.amount.sum()))
    return pd.DataFrame(rows, columns=['timestamps'] + COLS)


def parse_yahoo(payload, instrument, timeframe, lookback, now=None):
    import numpy as np
    import pandas as pd
    now = pd.Timestamp.now(tz='UTC') if now is None else pd.Timestamp(now)
    chart = payload.get('chart', {})
    if chart.get('error'): raise ValueError('Yahoo: ' + str(chart['error']))
    result = chart.get('result')
    if not result: raise ValueError('Yahoo returned an empty result')
    r = result[0]
    meta = r.get('meta', {})
    stamps = r.get('timestamp', [])
    q = r.get('indicators', {}).get('quote', [{}])[0]
    if not stamps: raise ValueError('No history available for this instrument')
    df = pd.DataFrame({c: q.get(c, [None] * len(stamps)) for c in COLS[:5]})
    df.insert(0, 'timestamps', pd.to_datetime(stamps, unit='s', utc=True))
    for c in COLS[:5]: df[c] = pd.to_numeric(df[c], errors='coerce')
    missing_prices = int(df[COLS[:4]].isna().any(axis=1).sum())
    df = df.dropna(subset=COLS[:4]).drop_duplicates('timestamps').sort_values('timestamps').reset_index(drop=True)
    # FX and indices have no directly tradeable centralized volume. Do not infer it.
    missing_volume = instrument['id'] in ['EURUSD', 'SP500', 'NASDAQ100']
    if missing_volume:
        df['volume'] = 0.0
    elif df.volume.isna().any():
        raise ValueError('The source has no futures volume; run stopped')
    # Yahoo does not supply quote turnover; do not disguise an OHLC*volume proxy as actual amount.
    df['amount'] = 0.0
    base_seconds = 900 if timeframe == '15m' else 3600
    delay = max(20, int(meta.get('exchangeDataDelayedBy', 0) or 0))
    cutoff = now - pd.Timedelta(minutes=delay)
    if timeframe == '1d':
        # Provider daily bars are session-date labels, not 24-hour intraday bars.
        # Conservatively exclude the provider's entire current local date.
        zone = meta.get('exchangeTimezoneName') or instrument['tz']
        dates = df.timestamps.dt.tz_convert(zone).dt.tz_localize(None).dt.normalize()
        df = df[dates < cutoff.tz_convert(zone).tz_localize(None).normalize()].copy()
        df['timestamps'] = dates.loc[df.index].dt.tz_localize('UTC')
        df = df.drop_duplicates('timestamps')
    else:
        df = df[df.timestamps + pd.Timedelta(seconds=base_seconds) <= cutoff].copy()
    if timeframe != '1d' and instrument['id'] in ['SP500', 'NASDAQ100']:
        local = df.timestamps.dt.tz_convert('America/New_York')
        minutes = local.dt.hour * 60 + local.dt.minute
        df = df[(minutes >= 570) & (minutes + base_seconds // 60 <= 960)].copy()
    if timeframe == '4h':
        df = aggregate_four_hours(df, instrument['tz'], instrument['anchor'])
    if len(df) < lookback:
        raise ValueError(f'The source returned {len(df)} complete candles; required: {lookback}. Try 200 candles or another timeframe.')
    df = df.tail(lookback).reset_index(drop=True)
    if not np.isfinite(df[COLS].to_numpy()).all(): raise ValueError('Non-finite input values')
    if (df[COLS[:4]] <= 0).any().any() or (df.volume < 0).any(): raise ValueError('Unsupported price or volume')
    if ((df.high < df[['open', 'close', 'low']].max(axis=1)) |
        (df.low > df[['open', 'close', 'high']].min(axis=1))).any(): raise ValueError('Invalid OHLC geometry')
    age_hours = (now - df.timestamps.iloc[-1]).total_seconds() / 3600
    if age_hours > 120: raise ValueError('History is more than five days old; forecast stopped')
    warnings = [
        'Yahoo Finance uses an unofficial endpoint; data may be delayed or unavailable.',
        'Future timestamps are approximate: a typical weekly schedule is used without guaranteed holiday handling.',
        'Turnover is unavailable from this source and is passed to the model as amount=0.',
    ]
    if missing_volume: warnings.append('This series uses volume=0. Forecast quality with this input needs evaluation.')
    if instrument['id'] in ['GOLD', 'WTI']:
        warnings.append('Yahoo futures series: contract rolls may introduce gaps. This is neither spot nor a fixed-contract series.')
    if timeframe == '4h': warnings.append('Four-hour bars contain four adjacent hourly candles. Incomplete session blocks are excluded.')
    if timeframe == '1d': warnings.append('Daily dates label sessions in UTC; the source current date is excluded entirely. Forecast dates do not account for holidays.')
    if age_hours > (72 if timeframe == '1d' else 8): warnings.append(f'The latest complete candle opened {age_hours:.1f} hours ago: check market hours and data freshness.')
    if missing_prices: warnings.append(f'Source history omitted {missing_prices} records without OHLC; prices were not fabricated.')
    return df, dict(source='Yahoo Finance chart', source_symbol=instrument['ticker'],
                    market=instrument['kind'], exchange_timezone=instrument['tz'],
                    reported_exchange=meta.get('exchangeName'), source_last_trade_time=meta.get('regularMarketTime'),
                    provider_delay_minutes=meta.get('exchangeDataDelayedBy'), closed_bar_buffer_minutes=delay,
                    data_age_hours=age_hours, volume_semantics='zero_missing' if missing_volume else 'provider_reported',
                    amount_semantics='zero_missing', calendar='regular_weekly_not_holiday_aware', warnings=warnings)


def fetch_yahoo(instrument, timeframe, lookback):
    import requests
    interval = timeframe if timeframe in ['15m', '1d'] else '1h'
    # 15-minute data: under 60 days. Hourly: under 730 days.
    days = 59 if timeframe == '15m' else (720 if timeframe == '4h' else 180)
    if timeframe == '1d': days = max(1100, lookback * 2 + 30)
    endpoint = 'https://query1.finance.yahoo.com/v8/finance/chart/' + quote(instrument['ticker'], safe='')
    response = requests.get(endpoint, params=dict(interval=interval, period1=int(time.time()) - days*86400,
                            period2=int(time.time()), includePrePost='false', events='div,splits'),
                            headers={'User-Agent': 'Mozilla/5.0'}, timeout=(10, 45))
    if response.status_code == 429: raise ValueError('Yahoo rate limit reached. Try again later.')
    response.raise_for_status()
    return parse_yahoo(response.json(), instrument, timeframe, lookback)


def future_timestamps(df, instrument, timeframe, count):
    """Regular weekly sessions, timezone/DST aware; holidays remain approximate."""
    import pandas as pd
    last = df.timestamps.iloc[-1]
    step = pd.Timedelta(seconds=SECONDS[timeframe])
    if instrument['source'] == 'binance':
        return pd.Series(pd.date_range(last, periods=count+1, freq=step)[1:])
    if timeframe == '1d':
        return pd.Series(pd.bdate_range(last.normalize() + pd.Timedelta(days=1), periods=count))
    zone = instrument['tz']
    first_day = last.tz_convert(zone).tz_localize(None).normalize()
    candidates = []
    for day in pd.date_range(first_day, periods=100, freq='D'):
        if day.weekday() >= 5:
            continue
        if instrument['id'] in ['SP500', 'NASDAQ100']:
            start = day + pd.Timedelta(hours=9, minutes=30)
            end = day + pd.Timedelta(hours=16)
        elif instrument['id'] in ['GOLD', 'WTI']:
            # Trading date Mon..Fri: previous evening 18:00 to 17:00 NY.
            start = day - pd.Timedelta(days=1) + pd.Timedelta(hours=18)
            end = day + pd.Timedelta(hours=17)
        else:
            # Yahoo FX regular weekday template; no claim about holiday sessions.
            start = day
            end = day + pd.Timedelta(days=1)
        start = start.tz_localize(zone, ambiguous='NaT', nonexistent='NaT')
        end = end.tz_localize(zone, ambiguous='NaT', nonexistent='NaT')
        if pd.isna(start) or pd.isna(end):
            continue
        for ts in pd.date_range(start, end, freq=step):
            if ts + step <= end and ts.tz_convert('UTC') > last:
                candidates.append(ts.tz_convert('UTC'))
        if len(candidates) >= count:
            break
    if len(candidates) < count:
        raise ValueError('Not enough dates to infer future sessions')
    return pd.Series(sorted(set(candidates))[:count])
