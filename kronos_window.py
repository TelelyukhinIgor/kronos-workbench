#!/usr/bin/env python3
"""Local-only UI server for Kronos, no external UI dependencies."""
import argparse
import csv
import errno
import json
import mimetypes
import os
from pathlib import Path
import secrets
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import urlsplit, parse_qs
import webbrowser

ROOT = Path(__file__).resolve().parent
from market_sources import INSTRUMENTS, BY_ID
TOKEN = secrets.token_urlsafe(32)
LOCK = threading.Lock()
CANCEL = threading.Event()
STATE = dict(running=False, log=[], error=None, result=None, current=None)
PORT = 8765


def runs():
    result = []
    for p in sorted((ROOT / 'signals').glob('*/metadata.json'), reverse=True):
        try:
            m = json.loads(p.read_text())
            result.append(dict(id=p.parent.name, symbol=m['symbol'], timeframe=m['timeframe'],
                               started_at=m['started_at'], sample_count=m['sample_count'],
                               model=m.get('model', ''),
                               replay=m.get('offline_replay', False)))
        except (ValueError, KeyError): continue
        if len(result) == 50: break
    return result


def result_directory(run):
    parent = (ROOT / 'signals').resolve()
    path = (parent / run).resolve()
    if path.parent != parent or not path.is_dir(): raise ValueError('Unknown result')
    return path


def load_result(run):
    p = result_directory(run)
    def rows(name):
        with (p / name).open() as stream:
            return [{k: v if k == 'timestamps' else float(v) for k, v in r.items()}
                    for r in csv.DictReader(stream)]
    return dict(id=run, metadata=json.loads((p / 'metadata.json').read_text()),
                history=rows('input.csv')[-100:], forecast=rows('forecast.csv'), paths=rows('trajectories.csv'))


def worker(params):
    def progress(msg):
        if CANCEL.is_set(): raise InterruptedError('Run cancelled by the user')
        with LOCK:
            STATE['log'].append(msg)
            STATE['log'] = STATE['log'][-100:]
    try:
        from run_kronos import main
        out = main(SimpleNamespace(**params), progress=progress)
        with LOCK: STATE['result'] = out.name
    except Exception as exc:
        traceback.print_exc()
        message = str(exc)
        if any(s in message for s in ['NameResolution', 'resolve', 'ConnectionError', 'HTTPSConnectionPool']):
            message = 'Unable to load market data. Check your internet connection; the source may be temporarily unavailable. ' + message[:220]
        with LOCK: STATE['error'] = message
    finally:
        with LOCK: STATE['running'] = False


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass

    def send_bytes(self, content, kind, status=200):
        self.send_response(status)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(content)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.end_headers()
        self.wfile.write(content)

    def data(self, obj, status=200):
        self.send_bytes(json.dumps(obj, ensure_ascii=False, allow_nan=False).encode(), 'application/json; charset=utf-8', status)

    def valid_host(self):
        return self.headers.get('Host') in [f'127.0.0.1:{PORT}', f'localhost:{PORT}']

    def do_GET(self):
        if not self.valid_host(): return self.data({'error': 'Invalid Host'}, 403)
        url = urlsplit(self.path)
        try:
            if url.path == '/':
                html = (ROOT / 'kronos_window.html').read_text().replace('__TOKEN__', TOKEN)
                return self.send_bytes(html.encode(), 'text/html; charset=utf-8')
            if url.path == '/api/health': return self.data({'app': 'kronos-workbench', 'root': str(ROOT)})
            if url.path == '/api/catalog': return self.data({'instruments': INSTRUMENTS})
            if url.path == '/api/runs': return self.data(runs())
            if url.path == '/api/status':
                with LOCK: snapshot = {**STATE, 'log': list(STATE['log'])}
                return self.data(snapshot)
            if url.path == '/api/result': return self.data(load_result(parse_qs(url.query)['id'][0]))
            if url.path == '/file':
                q = parse_qs(url.query)
                name = q['name'][0]
                if name not in ['forecast.csv', 'input.csv', 'trajectories.csv', 'metadata.json', 'forecast.png']:
                    raise ValueError('Unknown file')
                path = result_directory(q['id'][0]) / name
                return self.send_bytes(path.read_bytes(), mimetypes.guess_type(name)[0] or 'application/octet-stream')
            return self.data({'error': 'Not found'}, 404)
        except (ValueError, KeyError, OSError) as exc: return self.data({'error': str(exc)}, 400)

    def do_POST(self):
        if not self.valid_host() or self.headers.get('X-Kronos-Token') != TOKEN:
            return self.data({'error': 'Invalid token'}, 403)
        origin = self.headers.get('Origin')
        if origin and origin not in [f'http://127.0.0.1:{PORT}', f'http://localhost:{PORT}']:
            return self.data({'error': 'Invalid Origin'}, 403)
        try:
            n = int(self.headers.get('Content-Length', '0'))
            if n > 4096: raise ValueError('Request too large')
            body = json.loads(self.rfile.read(n) or '{}')
            if self.path == '/api/cancel':
                CANCEL.set()
                return self.data({'ok': True})
            if self.path == '/api/shutdown':
                with LOCK:
                    if STATE['running']: return self.data({'error': 'Stop the current run first'}, 409)
                self.data({'ok': True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            if self.path != '/api/run': return self.data({'error': 'Not found'}, 404)
            params = dict(symbol=body.get('symbol', 'BTCUSDT'), timeframe=body.get('timeframe', '1h'),
                          samples=body.get('samples', 20), lookback=body.get('lookback', 400),
                          pred_len=body.get('pred_len', 24), model=body.get('model', 'small'),
                          threads=4, seed=42, no_plot=False)
            if params['symbol'] not in BY_ID or params['timeframe'] not in ['15m', '1h', '4h', '1d']:
                raise ValueError('Invalid instrument or timeframe')
            if params['model'] not in ['small', 'mini']: raise ValueError('Invalid model')
            if params['samples'] not in [2, 5, 20, 50] or params['lookback'] not in [200, 400, 512] or params['pred_len'] not in [6, 12, 24, 48]:
                raise ValueError('Invalid parameters')
            with LOCK:
                if STATE['running']: return self.data({'error': 'A run is already in progress'}, 409)
                CANCEL.clear()
                STATE.update(running=True, log=['Preparing run…'], error=None, result=None, current=params)
            threading.Thread(target=worker, args=(params,), daemon=True).start()
            self.data({'ok': True})
        except (ValueError, KeyError, TypeError) as exc: self.data({'error': str(exc)}, 400)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--open-browser', action='store_true')
    args = p.parse_args()
    try:
        server = ThreadingHTTPServer(('127.0.0.1', PORT), Handler)
    except OSError as exc:
        if exc.errno != errno.EADDRINUSE:
            raise RuntimeError('Unable to start the local server. Check whether your environment allows localhost sockets.') from exc
        from urllib.request import urlopen
        with urlopen(f'http://127.0.0.1:{PORT}/api/health', timeout=3) as r: health = json.load(r)
        if health != {'app': 'kronos-workbench', 'root': str(ROOT)}:
            raise RuntimeError('Port 8765 is used by another application')
        if args.open_browser: webbrowser.open(f'http://127.0.0.1:{PORT}')
        return
    if args.open_browser: webbrowser.open(f'http://127.0.0.1:{PORT}')
    print(f'Kronos: http://127.0.0.1:{PORT}', flush=True)
    try: server.serve_forever()
    finally: server.server_close()


if __name__ == '__main__': main()
