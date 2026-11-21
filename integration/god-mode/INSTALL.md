# Installing the frac-demand lane into AMARA God Mode

The globe and UI are never touched: the installer refuses to write under `src/app`, `src/canvas`,
`src/components` or `public`, and checks they are byte-identical afterwards.

```bash
# 1. dry run (writes nothing): see exactly what would change
python3 scripts/frac/install_into_god_mode.py --target /path/to/god-mode

# 2. apply
python3 scripts/frac/install_into_god_mode.py --target /path/to/god-mode --apply
```

What it adds to God Mode, and nothing else:

| path | what |
|---|---|
| `scripts/frac/**` | forecast, adapters, evaluation suite, tests |
| `docs/FRAC-DEMAND-PHASE0-SPEC.md` | the spec and every result so far |
| `hermes/frac-demand.jobs.json` | a Hermes lane in the same format as `open-acreage.jobs.json` |
| `package.json` scripts | `fd` (run the suite), `fd:forecast`, `test:frac` (existing scripts are never changed) |

Then, inside God Mode:

```bash
npm run test:frac
npm run fd -- --permits ~/frac-data/work/permits.csv --frac ~/frac-data/work/frac_v2.csv --out ~/frac-data/work/results --publish
```

`--publish` pushes ONLY summary `.json`/`.md` files to the branch `claude/frac-results` (never CSVs or zips).
Output rows are ESTIMATE-labeled backtests, not live forecasts. Visible globe features, if ever wanted
(for example a demand heat layer or numbers in the parcel popup), need a preview and Scott's approval first.
