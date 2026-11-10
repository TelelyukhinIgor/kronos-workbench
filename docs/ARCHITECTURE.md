# Architecture

| Module | Responsibility |
|---|---|
| market_sources.py | Instrument catalog, Yahoo adapter, 4h aggregation and future session timestamps |
| run_kronos.py | Binance adapter, CPU model loading, sequential scenarios, quality metrics and saved artifacts |
| kronos_window.py | Local HTTP server, a single running job, cancellation, file access and server shutdown |
| kronos_window.html | English research interface, charts, settings, progress, run history and downloads |
| setup_kronos.sh | Project-local Python environment, pinned upstream checkout and recorded dependencies |
| Launch.command | Project-relative macOS launcher |
| test_kronos_window.py | Eleven offline checks with synthetic fixtures |

The server binds to 127.0.0.1:8765. Mutations require a process-generated token and Host/Origin checks. Result files are resolved under signals/ with path traversal rejected.

Models are cached in the process between runs; downloaded assets remain in project-local cache directories. The workbench uses CPU execution with four threads by default and at most six. Cancellation is checked between scenarios, not during a model call.

The upstream model is retrieved into the ignored Kronos/ directory. It is not re-published here. Weight terms and provider access are external to this wrapper.
