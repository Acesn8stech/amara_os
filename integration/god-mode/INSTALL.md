# Installing the frac-demand lane into AMARA God Mode

The globe and UI are never touched: the installer refuses to write under `src/app`, `src/canvas`,
`src/components` or `public`, and checks they are byte-identical afterwards.

```bash
python3 scripts/frac/install_into_god_mode.py --target /path/to/god-mode           # dry run, writes nothing
python3 scripts/frac/install_into_god_mode.py --target /path/to/god-mode --apply   # apply
```

It adds, and nothing else: `scripts/frac/**`, `docs/FRAC-DEMAND-PHASE0-SPEC.md`, `hermes/frac-demand.jobs.json`
(an always-on service entry, **not a schedule**), `integration/god-mode/*` (policy, keep-alive, these notes), and the
npm scripts `fd`, `fd:live`, `fd:desk`, `fd:watch`, `fd:forecast`, `test:frac` (existing scripts are never changed).

## Running it live (no schedule)

```bash
# the live service: stays running, re-evaluates the moment the input files change, serves read-only status on localhost
npm run fd:live -- --permits ~/frac-data/work/permits.csv --frac ~/frac-data/work/frac_v2.csv --out ~/frac-data/work/live --port 8765 --publish

# the desk: a live terminal board (status, risk, per-county edge for water and sand, event tape). Read-only.
npm run fd:desk -- --dir ~/frac-data/work/live
npm run fd:desk -- --dir ~/frac-data/work/live --once     # one frame; exit 0 OK, 1 WARN, 2 ALERT, 3 feed down
npm run fd:desk -- --dir ~/frac-data/work/live --recap 24 # what happened in the last 24 hours
```

Stop the service with Ctrl-C, or `touch ~/frac-data/work/live/STOP`, or set `FD_KILL_SWITCH=1`.
Read-only status: `http://127.0.0.1:8765/health`, `/status`, `/summary`, `/alert` (localhost only; POST is refused).
`--publish` pushes only summary `.json`/`.md` files to the branch `claude/frac-results`, never CSVs or zips.

## Staying up without anyone logged in to a terminal

`com.amara.frac-live.plist` is a macOS keep-alive definition: it starts at login and restarts the service if it
dies. It deliberately has **no schedule keys**. It changes login items, so it needs Scott's go-ahead. Replace
`__PYTHON__`, `__REPO__`, `__HOME__`, copy to `~/Library/LaunchAgents/`, then `launchctl load` it.

## What stays human

FracFocus requires a person to accept its terms for every download, so refreshing that data is a person's job: drop
the rebuilt CSVs in place and the live service notices on its own. A service that reaches `stopped_after_failures`
must be read (ALERT.md) by a person before it is restarted. Bots must never retune the model to clear an alert.
See `autonomy-policy.json`. Visible globe features (for example a demand layer or numbers in the parcel popup) need a
preview and Scott's approval first. Output is an ESTIMATE-labeled backtest, not a live forecast.
