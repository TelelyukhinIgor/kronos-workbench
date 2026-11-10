"""Offline checks. Fixtures are synthetic and never saved as market forecasts."""
import unittest
import numpy as np
import pandas as pd
from market_sources import aggregate_four_hours, parse_yahoo, future_timestamps, BY_ID, COLS
from kronos_window import result_directory, load_result


def candles(stamps):
    n = len(stamps)
    return pd.DataFrame(dict(timestamps=stamps, open=np.ones(n)*100,
                             high=np.ones(n)*102, low=np.ones(n)*99,
                             close=np.ones(n)*101, volume=np.ones(n)*10, amount=np.zeros(n)))


class SourceChecks(unittest.TestCase):
    def test_daily_index_excludes_current_session_and_normalizes_dates(self):
        stamps = pd.date_range('2026-10-19 09:30', periods=2, freq='D', tz='America/New_York')
        df = candles(stamps)
        payload = {'chart': {'result': [{'timestamp': [int(x.timestamp()) for x in stamps],
                   'meta': {'exchangeTimezoneName': 'America/New_York'},
                   'indicators': {'quote': [{c: df[c].tolist() for c in COLS[:5]}]}}]}}
        result, _ = parse_yahoo(payload, BY_ID['SP500'], '1d', 1, now='2026-10-20 22:30Z')
        self.assertEqual(result.timestamps.iloc[0], pd.Timestamp('2026-10-19 00:00Z'))

    def test_daily_weekends_differ_for_crypto_and_other_markets(self):
        df = candles(pd.DatetimeIndex(['2026-10-23 00:00Z']))
        for symbol in ['SP500', 'NASDAQ100', 'EURUSD', 'GOLD', 'WTI']:
            result = future_timestamps(df, BY_ID[symbol], '1d', 24)
            self.assertEqual(result.iloc[0], pd.Timestamp('2026-10-26 00:00Z'))
            self.assertTrue((result.dt.weekday < 5).all())
            self.assertEqual(len(result), 24)
        result = future_timestamps(df, BY_ID['BTCUSDT'], '1d', 24)
        self.assertEqual(result.iloc[0], pd.Timestamp('2026-10-24 00:00Z'))

    def test_four_hour_blocks_do_not_bridge_gap(self):
        stamps = pd.date_range('2026-10-19', periods=12, freq='h', tz='UTC').delete(5)
        result = aggregate_four_hours(candles(stamps), 'UTC', 0)
        self.assertEqual(result.timestamps.dt.hour.tolist(), [0, 8])
        self.assertEqual(result.volume.tolist(), [40, 40])

    def test_index_session_tail_excluded(self):
        stamps = pd.date_range('2026-10-19 09:30', periods=7, freq='h', tz='America/New_York').tz_convert('UTC')
        result = aggregate_four_hours(candles(stamps), 'America/New_York', 570)
        self.assertEqual(len(result), 1)
        self.assertEqual(result.timestamps.iloc[0], stamps[0])

    def test_incomplete_yahoo_candles_removed_and_missing_fx_volume_marked(self):
        stamps = pd.date_range('2026-10-19', periods=48, freq='h', tz='UTC')
        df = candles(stamps)
        payload = {'chart': {'result': [{'timestamp': [int(x.timestamp()) for x in stamps],
                   'meta': {}, 'indicators': {'quote': [{c: df[c].tolist() for c in COLS[:5]}]}}]}}
        result, meta = parse_yahoo(payload, BY_ID['EURUSD'], '1h', 32, now='2026-10-20 22:30Z')
        self.assertEqual(result.timestamps.iloc[-1], pd.Timestamp('2026-10-20 21:00Z'))
        self.assertTrue(result.volume.eq(0).all())
        self.assertEqual(meta['volume_semantics'], 'zero_missing')

    def test_weekly_forecast_respects_observed_weekend_and_dst(self):
        stamps = pd.DatetimeIndex([f'2026-10-{d:02d} 09:30' for d in [19,20,21,22,23,26,27,28,29,30]],tz='America/New_York').tz_convert('UTC')
        y = future_timestamps(candles(stamps), BY_ID['SP500'], '4h', 3)
        self.assertEqual(y.iloc[0], pd.Timestamp('2026-11-02 14:30Z'))
        self.assertTrue((y.dt.weekday < 5).all())

    def test_unsafe_file_paths_rejected(self):
        with self.assertRaises(ValueError): result_directory('../Kronos')
        with self.assertRaises(ValueError): result_directory('/etc')

    def test_synthetic_saved_result_loads(self):
        import tempfile, json
        from pathlib import Path
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp) / 'signals' / 'synthetic-fixture'
            run.mkdir(parents=True)
            base = candles(pd.date_range('2026-01-01', periods=24, freq='h', tz='UTC'))
            base.to_csv(run / 'input.csv', index=False)
            base.to_csv(run / 'forecast.csv', index=False)
            paths = pd.concat([base.assign(sample_id=i) for i in [0, 1]])
            paths.to_csv(run / 'trajectories.csv', index=False)
            (run / 'metadata.json').write_text(json.dumps({'synthetic': True}))
            with patch('kronos_window.ROOT', Path(temp)):
                r = load_result('synthetic-fixture')
            self.assertEqual(len(r['forecast']), 24)
            self.assertEqual(len(r['paths']), 48)
            self.assertTrue(r['metadata']['synthetic'])

    def test_ethereum_removed(self):
        self.assertNotIn('ETHUSDT', BY_ID)

    def test_fx_horizon_not_limited_by_observed_weekday_slots(self):
        stamps = pd.date_range('2026-10-16', '2026-10-20 04:00', freq='15min', tz='UTC')
        df = candles(stamps[stamps.weekday < 5]).tail(400)
        y = future_timestamps(df, BY_ID['EURUSD'], '15m', 24)
        self.assertEqual(y.iloc[0], pd.Timestamp('2026-10-20 04:15Z'))
        self.assertEqual(y.iloc[-1], pd.Timestamp('2026-10-20 10:00Z'))
        self.assertTrue(y.diff().iloc[1:].eq(pd.Timedelta(minutes=15)).all())

    def test_futures_nightly_break_not_forecast(self):
        df = candles(pd.date_range('2026-10-19 16:00', periods=1, tz='America/New_York').tz_convert('UTC'))
        y = future_timestamps(df, BY_ID['GOLD'], '1h', 3)
        self.assertEqual(y.iloc[0].tz_convert('America/New_York').hour, 18)


if __name__ == '__main__': unittest.main()
