# Validation record

Publication copy prepared: 2026-11-10.

| Check | Result |
|---|---|
| Offline unit checks | 11 passed using the existing Python 3.11 environment |
| Python syntax | Passed for the included Python files |
| Shell syntax | Passed for setup_kronos.sh and Launch.command |
| English source scan | No Cyrillic remains in included project text |
| Model inference during handoff | Not run |
| New provider requests during handoff | Not run |
| Fresh installation on another machine | Not run |
| Finder launch of the new portable launcher | Not run |
| Browser visual verification | Not run |
| Independent forecast-skill evaluation | Not established |

Offline checks cover daily-date normalization, weekends, session tails, missing FX volume, adjacent 4h blocks, DST, futures breaks, path rejection and synthetic saved-result loading.

The workbench is designed to load Kronos on CPU and save inspectable scenario outputs after local setup. This publication copy passes 11 offline checks; they do not load the model or request market data.

Publication changes: English UI, catalog, messages and logs; project-relative launcher; pinned upstream setup; installation without automatic inference; synthetic replacement for the saved-result test fixture. No original market results, private paths, compiled launcher, model weights, runtime environment or caches are included.

## Re-verification

After the publication copy was received, the 11 offline unit tests were repeated on 2026-11-10 on a Linux host with Python 3.13.5, NumPy 2.5.4, and pandas 3.0.6. All 11 passed. These tests do not import PyTorch and do not request market data.

Python 3.13 is not the documented runtime for a full installation. The supported range remains Python 3.10–3.12. A recorded dependency set is Python 3.11, PyTorch 2.2.2, and NumPy 1.26.4. Confirm the environment on your machine before a full model run. Fresh installation, model inference, Finder launch of the portable launcher, and a browser visual check were not run here.
