"""Tests for frac_forecast.py on a synthetic world with KNOWN truth.
Run: python3 -m unittest scripts/frac/test_frac_forecast.py -v
These prove the code does what the spec says. They say NOTHING about real Reeves
accuracy; that needs the real RRC and FracFocus data (gate G2)."""
import copy, importlib.util, json, random, statistics, tempfile, unittest
from datetime import date, timedelta
from pathlib import Path

spec = importlib.util.spec_from_file_location("ff", Path(__file__).resolve().parent / "frac_forecast.py")
ff = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ff)

TRUTH = {  # operator -> (mean lag days, sd, water gal/ft, proppant lb/ft)
    "ALPHA OIL": (120, 15, 2000.0, 1800.0),
    "BRAVO ENERGY": (200, 25, 2400.0, 2200.0),
}
COMPLETION = 0.85


def world(seed=7, start=date(2022, 1, 3), months=40, county="REEVES"):
    """Permit volume comes in waves (so a flat baseline is a poor predictor)."""
    rnd = random.Random(seed)
    permits, fracs, n = [], [], 0
    for m in range(months):
        wave = 3 + int(14 * (1 + __import__("math").sin(m / 3.0)) / 2)   # 3..17 per month
        for _ in range(wave):
            op = rnd.choice(list(TRUTH))
            lag_mu, lag_sd, w_int, p_int = TRUTH[op]
            n += 1
            api = f"42389{n:05d}"
            pdate = start + timedelta(days=m * 30 + rnd.randint(0, 29))
            lateral = rnd.randint(7000, 13000)
            permits.append({"api": api, "permit_date": pdate.isoformat(), "operator": op,
                            "county": county, "lateral_ft": lateral})
            if rnd.random() < COMPLETION:
                lag = max(30, int(rnd.gauss(lag_mu, lag_sd)))
                fracs.append({"api": api + "0000", "frac_start": (pdate + timedelta(days=lag)).isoformat(),
                              "operator": op, "water_gal": lateral * w_int * rnd.uniform(.9, 1.1),
                              "proppant_lb": lateral * p_int * rnd.uniform(.9, 1.1)})
    return permits, fracs


def prepared(**kw):
    permits, fracs = world(**kw)
    p, _ = ff.clean_permits(permits)
    f, _ = ff.clean_fracs(fracs)
    joined, report = ff.join(p, f)
    return p, f, joined, report


class Parsing(unittest.TestCase):
    def test_api_normalization(self):
        self.assertEqual(ff.api10("389-12345"), "4238912345")            # 8 digits: no state code
        self.assertEqual(ff.api10("42-389-12345-00-00"), "4238912345")   # 14 digits with dashes
        self.assertEqual(ff.api10("4238912345"), "4238912345")
        self.assertIsNone(ff.api10("12345"))
        self.assertIsNone(ff.api10(None))
        self.assertIsNone(ff.api10("35-001-12345-00-00"))               # not Texas

    def test_bad_rows_are_counted_not_guessed(self):
        _, rej = ff.clean_permits([{"api": "x", "permit_date": "2024-01-01"},
                                   {"api": "389-12345", "permit_date": "not a date"}])
        self.assertEqual(rej, {"bad_api": 1, "bad_permit_date": 1})

    def test_amended_permit_keeps_earliest_date(self):
        rows = [{"api": "389-12345", "permit_date": "2024-03-01"}, {"api": "389-12345", "permit_date": "2024-01-15"}]
        p, _ = ff.clean_permits(rows)
        self.assertEqual(p["4238912345"]["permit_date"], date(2024, 1, 15))


class Join(unittest.TestCase):
    def test_frac_before_permit_is_excluded_and_reported(self):
        p, _ = ff.clean_permits([{"api": "389-00001", "permit_date": "2024-06-01"}])
        f, _ = ff.clean_fracs([{"api": "42389000010000", "frac_start": "2024-05-01"}])
        joined, rep = ff.join(p, f)
        self.assertEqual(joined, [])
        self.assertEqual(rep["frac_before_permit"], 1)

    def test_multiple_jobs_first_used_and_flagged(self):
        p, _ = ff.clean_permits([{"api": "389-00001", "permit_date": "2024-01-01"}])
        f, _ = ff.clean_fracs([{"api": "42389000010000", "frac_start": "2024-06-01"},
                               {"api": "42389000010001", "frac_start": "2024-05-01"}])
        joined, rep = ff.join(p, f)
        self.assertEqual(joined[0]["frac_start"], date(2024, 5, 1))
        self.assertEqual(rep["multiple_jobs_first_used"], 1)

    def test_match_rate_matches_completion_in_synthetic_world(self):
        _, _, joined, rep = prepared()
        self.assertAlmostEqual(rep["match_rate"], COMPLETION, delta=0.06)
        self.assertEqual(rep["permits"], rep["joined"] + rep["no_frac_found"])


class Models(unittest.TestCase):
    def setUp(self):
        self.p, self.f, self.joined, _ = prepared()
        self.as_of = date(2024, 12, 1)
        self.m = ff.fit(self.joined, self.p, self.as_of)

    def test_lag_recovered_per_operator(self):
        for op, (mu, _, _, _) in TRUTH.items():
            values, level = ff.pick(self.m["lags"], op, "REEVES")
            self.assertEqual(level, "operator+county")
            self.assertAlmostEqual(statistics.median(values), mu, delta=12)

    def test_intensity_recovered(self):
        for op, (_, _, w, pp) in TRUTH.items():
            wv, _ = ff.pick(self.m["water"], op, "REEVES")
            pv, _ = ff.pick(self.m["prop"], op, "REEVES")
            self.assertAlmostEqual(statistics.median(wv), w, delta=w * .05)
            self.assertAlmostEqual(statistics.median(pv), pp, delta=pp * .05)

    def test_completion_rate_uses_matured_cohort(self):
        rate, level = ff.completion_rate(self.m, "ALPHA OIL", "REEVES")
        self.assertAlmostEqual(rate, COMPLETION, delta=0.10)

    def test_fallback_to_broader_group_when_sample_small(self):
        rows = [dict(r) for r in self.joined]
        rare = [dict(rows[0], operator="TINY CO", api=f"4238999{i:03d}0") for i in range(2)]
        m = ff.fit(rows + rare, {**self.p}, date(2030, 1, 1))
        values, level = ff.pick(m["lags"], "TINY CO", "REEVES")
        self.assertIn(level, ("county", "all"))      # never the 2-sample operator group

    def test_unknown_group_reports_unknown(self):
        values, level = ff.pick({}, "X", "Y")
        self.assertIsNone(values)
        self.assertEqual(level, "UNKNOWN")


class NoLookAhead(unittest.TestCase):
    def test_future_fracs_cannot_change_a_forecast(self):
        p, f, joined, _ = prepared()
        as_of = date(2024, 6, 1)
        base, _ = ff.forecast(p, ff.fit(joined, p, as_of), as_of)
        poisoned = copy.deepcopy(joined)
        for r in poisoned:
            if r["frac_start"] > as_of:
                r["water_gal"], r["proppant_lb"], r["lag_days"] = 9e12, 9e12, 1
        again, _ = ff.forecast(p, ff.fit(poisoned, p, as_of), as_of)
        self.assertEqual(base, again)

    def test_permits_after_as_of_are_ignored(self):
        p, f, joined, _ = prepared()
        as_of = date(2023, 6, 1)
        model = ff.fit(joined, p, as_of)
        only_past = {k: v for k, v in p.items() if v["permit_date"] <= as_of}
        a, _ = ff.forecast(p, model, as_of)
        b, _ = ff.forecast(only_past, model, as_of)
        self.assertEqual(a, b)


class Forecast(unittest.TestCase):
    def setUp(self):
        self.p, self.f, self.joined, _ = prepared()

    def test_output_is_labeled_estimate_and_nonnegative(self):
        as_of = date(2024, 6, 1)
        buckets, _ = ff.forecast(self.p, ff.fit(self.joined, self.p, as_of), as_of)
        self.assertEqual(len(buckets), 13)
        for b in buckets:
            self.assertEqual(b["basis"], "ESTIMATE")
            self.assertGreaterEqual(b["water_gal"], 0)

    def test_permit_mass_is_conserved(self):
        """Expected permits placed in the horizon cannot exceed eligible permits x completion."""
        as_of = date(2024, 6, 1)
        model = ff.fit(self.joined, self.p, as_of)
        buckets, _ = ff.forecast(self.p, model, as_of, weeks=60)
        eligible = [q for q in self.p.values() if q["permit_date"] <= as_of
                    and (as_of - q["permit_date"]).days <= ff.MAX_PERMIT_AGE
                    and q["api"] not in model["seen_fracs"]]
        self.assertLessEqual(sum(b["permits"] for b in buckets), len(eligible) + 1e-6)

    def test_unknown_intensity_is_counted_not_zero_filled(self):
        as_of = date(2024, 6, 1)
        joined = [dict(r, proppant_lb=None) for r in self.joined]
        buckets, _ = ff.forecast(self.p, ff.fit(joined, self.p, as_of), as_of)
        self.assertTrue(any(b["proppant_unknown_permits"] > 0 for b in buckets))
        self.assertTrue(all(b["proppant_lb"] == 0 for b in buckets))
        self.assertTrue(any(b["water_gal"] > 0 for b in buckets))

    def test_overdue_permit_with_no_comparable_history_is_reported_not_forecast(self):
        as_of = date(2024, 6, 1)
        model = ff.fit(self.joined, self.p, as_of)
        old = {"api": "4238999999", "permit_date": as_of - timedelta(days=500), "operator": "ALPHA OIL",
               "county": "REEVES", "lateral_ft": 10000}
        _, notes = ff.forecast({"x": old}, model, as_of)
        self.assertEqual(notes.get("overdue_no_comparable_history"), 1)


class PerWellFallback(unittest.TestCase):
    """RRC publishes no lateral length, so real permits will usually have lateral_ft blank."""
    def setUp(self):
        self.p, self.f, self.joined, _ = prepared()
        self.as_of = date(2024, 6, 1)
        self.model = ff.fit(self.joined, self.p, self.as_of)

    def test_blank_lateral_uses_per_well_volume_and_says_so(self):
        blank = {k: dict(v, lateral_ft=None) for k, v in self.p.items()}
        buckets, notes = ff.forecast(blank, self.model, self.as_of)
        self.assertTrue(any(b["water_gal"] > 0 for b in buckets))
        self.assertTrue(any(b["proppant_lb"] > 0 for b in buckets))
        self.assertTrue(any(b["per_well_fallback_permits"] > 0 for b in buckets))
        self.assertGreater(notes["no_lateral_length_used_per_well_volume"], 0)
        self.assertTrue(all(b["water_unknown_permits"] == 0 for b in buckets))

    def test_known_lateral_still_uses_per_foot_model(self):
        buckets, _ = ff.forecast(self.p, self.model, self.as_of)
        self.assertTrue(all(b["per_well_fallback_permits"] == 0 for b in buckets))

    def test_per_well_estimate_is_in_the_right_ballpark(self):
        """Per-well median should be near the true mean volume (lateral ~10,000 ft x intensity)."""
        values, _ = ff.pick(self.model["water_well"], "ALPHA OIL", "REEVES")
        self.assertAlmostEqual(statistics.median(values), 10000 * 2000.0, delta=10000 * 2000.0 * 0.15)

    def test_no_volume_history_at_all_stays_unknown_not_zero_filled(self):
        joined = [dict(r, water_gal=None, proppant_lb=None) for r in self.joined]
        model = ff.fit(joined, self.p, self.as_of)
        blank = {k: dict(v, lateral_ft=None) for k, v in self.p.items()}
        buckets, _ = ff.forecast(blank, model, self.as_of)
        self.assertTrue(any(b["water_unknown_permits"] > 0 for b in buckets))
        self.assertTrue(all(b["water_gal"] == 0 for b in buckets))


class Backtest(unittest.TestCase):
    def test_model_beats_flat_baseline_on_wavy_synthetic_world(self):
        p, f, joined, _ = prepared()
        first = min(r["frac_start"] for r in joined) + timedelta(days=365)
        last = max(r["frac_start"] for r in joined) - timedelta(weeks=13)
        results, summary = ff.backtest(p, joined, first, last)
        self.assertGreater(summary["scored_windows"], 5)
        self.assertTrue(summary["gate_G3_beats_baseline"], summary)

    def test_gate_is_unknown_when_nothing_can_be_scored(self):
        _, summary = ff.backtest({}, [], date(2024, 1, 1), date(2024, 2, 1))
        self.assertEqual(summary["gate_G3_beats_baseline"], "UNKNOWN")


class CLI(unittest.TestCase):
    def _write(self, rows, name):
        import csv
        path = Path(tempfile.mkdtemp()) / name
        with open(path, "w", newline="") as h:
            w = csv.DictWriter(h, fieldnames=list(rows[0]))
            w.writeheader(); w.writerows(rows)
        return str(path)

    def test_county_filter_and_forecast_run(self):
        permits, fracs = world()
        other, ofr = world(seed=9, county="PECOS")
        for r in other: r["api"] = "42" + "999" + r["api"][5:]
        for r in ofr: r["api"] = "42" + "999" + r["api"][5:]
        p = self._write(permits + other, "p.csv"); f = self._write(fracs + ofr, "f.csv")
        import io, contextlib
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = ff.main(["forecast", "--permits", p, "--frac", f, "--as-of", "2024-06-01", "--county", "reeves"])
        self.assertEqual(rc, 0)
        data = json.loads(out.getvalue())
        self.assertEqual(data["basis"], "ESTIMATE")
        self.assertEqual(data["join"]["permits"], len(permits))

    def test_absurd_date_range_is_refused_not_looped(self):
        permits, fracs = world()
        fracs[0]["frac_start"] = "3012-01-01"        # one bad row used to stretch the backtest to the year 3012
        fracs[0]["api"] = permits[0]["api"] + "0000"
        import io, contextlib
        p = self._write(permits, "p.csv"); f = self._write(fracs, "f.csv")
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = ff.main(["backtest", "--permits", p, "--frac", f])
        self.assertEqual(rc, 2)
        self.assertEqual(json.loads(out.getvalue())["status"], "RANGE_TOO_LARGE")

    def test_from_and_to_dates_bound_the_backtest(self):
        permits, fracs = world()
        import io, contextlib
        p = self._write(permits, "p.csv"); f = self._write(fracs, "f.csv")
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            ff.main(["backtest", "--permits", p, "--frac", f, "--from-date", "2024-01-01", "--to-date", "2024-06-30"])
        windows = json.loads(out.getvalue())["windows"]
        self.assertTrue(windows)
        self.assertTrue(all("2024-01-01" <= w["as_of"] <= "2024-06-30" for w in windows))

    def test_not_enough_history_exits_2(self):
        permits, fracs = world(months=3)
        import io, contextlib
        p = self._write(permits, "p.csv"); f = self._write(fracs, "f.csv")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            rc = ff.main(["backtest", "--permits", p, "--frac", f])
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
