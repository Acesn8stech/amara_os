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


def world(seed=7, start=date(2022, 1, 3), months=40, county="REEVES", drift=0.0):
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
                grow = 1 + drift * m / 12          # well sizes drift upward over time when drift > 0
                fracs.append({"api": api + "0000", "frac_start": (pdate + timedelta(days=lag)).isoformat(),
                              "operator": op, "water_gal": lateral * w_int * grow * rnd.uniform(.9, 1.1),
                              "proppant_lb": lateral * p_int * grow * rnd.uniform(.9, 1.1)})
    return permits, fracs


def prepared(**kw):  # kw may include drift
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


V2 = ff.ABLATION_VARIANTS[3][1]


class ModelOptions(unittest.TestCase):
    def test_trimmed_mean_ignores_extreme_values(self):
        vals = [10.0] * 38 + [1e9, 1e9]
        self.assertAlmostEqual(ff.trimmed_mean(vals), 10.0)
        self.assertGreater(sum(vals) / len(vals) - 10.0, 1e7)               # a plain mean would be wrecked

    def test_trimmed_mean_small_samples_fall_back_to_plain_mean(self):
        self.assertEqual(ff.trimmed_mean([2.0, 4.0]), 3.0)

    def test_recent_window_uses_only_recent_fracs_for_size(self):
        p, f, joined, _ = prepared()
        as_of = date(2024, 6, 1)
        cut = as_of - timedelta(days=120)             # old wells (most of the history) are small, the last 4 months big
        small_old = [dict(r, water_gal=1e6) if r["frac_start"] < cut else dict(r, water_gal=9e6)
                     for r in joined if r["frac_start"] <= as_of]
        full = ff.fit(small_old, p, as_of)
        recent = ff.fit(small_old, p, as_of, ff.opts_with(intensity_recent_days=120))
        full_med = statistics.median(ff.pick(full["water_well"], "ALPHA OIL", "REEVES")[0])
        recent_med = statistics.median(ff.pick(recent["water_well"], "ALPHA OIL", "REEVES")[0])
        self.assertEqual(recent_med, 9e6)
        self.assertLess(full_med, recent_med)

    def test_recent_window_does_not_touch_lag_model(self):
        p, f, joined, _ = prepared()
        as_of = date(2024, 6, 1)
        a = ff.fit(joined, p, as_of)
        b = ff.fit(joined, p, as_of, ff.opts_with(intensity_recent_days=365))
        self.assertEqual(a["lags"], b["lags"])        # restricting lags to recent completions would bias them short

    def test_hazard_shares_sum_to_one_over_a_long_horizon(self):
        lags = sorted([30, 60, 90, 120, 150, 180, 210, 240, 300, 400])
        shares = ff.hazard_weeks(lags, age=45, weeks=100)
        self.assertAlmostEqual(sum(shares), 1.0, places=9)
        self.assertIsNone(ff.hazard_weeks(lags, age=390, weeks=13))       # only 1 comparable lag left

    def test_hazard_shares_match_hand_count(self):
        lags = [10, 20, 30, 40, 50]
        shares = ff.hazard_weeks(lags, age=15, weeks=4)     # remaining: 5, 15, 25, 35 -> weeks 0, 2, 3, 5
        self.assertEqual(len(shares), 4)
        self.assertAlmostEqual(shares[0], 0.25)              # 5
        self.assertAlmostEqual(shares[1], 0.0)
        self.assertAlmostEqual(shares[2], 0.25)              # 15 is in [14, 21)
        self.assertAlmostEqual(shares[3], 0.25)              # 25 is in [21, 28)

    def test_hazard_week_boundaries_are_exact(self):
        """A remaining delay of exactly 7 days is week 1; exactly 14 is week 2 (matches actual_by_week)."""
        shares = ff.hazard_weeks([6, 7, 13, 14], age=0, weeks=3)
        self.assertEqual(shares, [0.25, 0.5, 0.25])

    def test_hazard_forecast_has_no_look_ahead(self):
        p, f, joined, _ = prepared()
        as_of = date(2024, 6, 1)
        base, _ = ff.forecast(p, ff.fit(joined, p, as_of, V2), as_of, opts=V2)
        poisoned = copy.deepcopy(joined)
        for r in poisoned:
            if r["frac_start"] > as_of:
                r["water_gal"], r["proppant_lb"], r["lag_days"] = 9e12, 9e12, 1
        again, _ = ff.forecast(p, ff.fit(poisoned, p, as_of, V2), as_of, opts=V2)
        self.assertEqual(base, again)

    def test_hazard_and_iqr_place_the_same_total_permit_mass(self):
        p, f, joined, _ = prepared()
        as_of = date(2024, 6, 1)
        model = ff.fit(joined, p, as_of)
        v1, _ = ff.forecast(p, model, as_of, weeks=120)
        hz, _ = ff.forecast(p, model, as_of, weeks=120, opts=ff.opts_with(timing="hazard"))
        self.assertAlmostEqual(sum(b["permits"] for b in v1), sum(b["permits"] for b in hz), delta=0.5)

    def test_options_default_to_v1_behaviour(self):
        p, f, joined, _ = prepared()
        as_of = date(2024, 6, 1)
        model = ff.fit(joined, p, as_of)
        self.assertEqual(ff.forecast(p, model, as_of), ff.forecast(p, model, as_of, opts=ff.V1))

    def test_recent_sizes_cut_the_bias_when_well_sizes_grow(self):
        p, f, joined, _ = prepared(drift=0.6)                 # wells grow ~60% a year
        first, last = date(2023, 1, 1), date(2024, 6, 30)
        r1, _ = ff.backtest(p, joined, first, last, opts=ff.V1)
        rb, _ = ff.backtest(p, joined, first, last, opts=ff.ABLATION_VARIANTS[2][1])
        b1 = abs(ff.quick_summary(r1)["median_13wk_total_bias"])
        bb = abs(ff.quick_summary(rb)["median_13wk_total_bias"])
        self.assertLess(bb, b1)

    def test_fixes_do_no_harm_when_nothing_drifts(self):
        p, f, joined, _ = prepared()
        first, last = date(2023, 1, 1), date(2024, 6, 30)
        r1, _ = ff.backtest(p, joined, first, last, opts=ff.V1)
        rc, _ = ff.backtest(p, joined, first, last, opts=V2)
        e1 = ff.quick_summary(r1)["median_abs_13wk_total_error"]
        ec = ff.quick_summary(rc)["median_abs_13wk_total_error"]
        self.assertLess(ec, e1 + 0.15)

    def test_ablate_returns_every_variant_with_a_time_split(self):
        p, f, joined, _ = prepared()
        out = ff.ablate(p, joined, date(2023, 1, 1), date(2024, 3, 1), split_year=2023)
        self.assertEqual(len(out), 4)
        for name, parts in out.items():
            self.assertEqual(set(parts), {"all_windows", "through_2023", "2024_onward"})
            early, late, total = (parts["through_2023"]["windows"], parts["2024_onward"]["windows"],
                                  parts["all_windows"]["windows"])
            self.assertEqual(early + late, total)               # every window lands in exactly one half
            self.assertGreater(early, 0)
            self.assertGreater(late, 0)

    def test_model_v2_flag_runs_through_the_cli(self):
        import io, contextlib, csv
        permits, fracs = world()
        def write(rows, name):
            path = Path(tempfile.mkdtemp()) / name
            with open(path, "w", newline="") as h:
                w = csv.DictWriter(h, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
            return str(path)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            ff.main(["backtest", "--permits", write(permits, "p.csv"), "--frac", write(fracs, "f.csv"),
                     "--summary-only", "--model", "v2"])
        self.assertIn("median_model_wape", json.loads(out.getvalue())["summary"])


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


class Explain(unittest.TestCase):
    def setUp(self):
        self.p, self.f, self.joined, _ = prepared()
        self.as_of = date(2024, 6, 1)
        self.model = ff.fit(self.joined, self.p, self.as_of)

    def row(self, **kw):
        base = {"api": "4238999000", "permit_date": self.as_of - timedelta(days=100), "operator": "ALPHA OIL",
                "county": "REEVES", "lateral_ft": 10000, "frac_start": self.as_of + timedelta(days=20),
                "water_gal": 20_000_000.0, "proppant_lb": None, "lag_days": 120}
        base.update(kw)
        return base

    def test_each_cause_is_classified(self):
        rows = [self.row(),                                                                   # modelled
                self.row(permit_date=self.as_of + timedelta(days=5), frac_start=self.as_of + timedelta(days=30)),
                self.row(permit_date=self.as_of - timedelta(days=ff.MAX_PERMIT_AGE + 60)),
                self.row(operator="NEW OPERATOR LLC", county="NOWHERE")]   # unknown operator falls back to ALL: still modelled
        water, counts, _, _ = ff.classify_actuals(self.p, rows, self.model, self.as_of, 13)
        self.assertEqual(counts["modelled"], 2)
        self.assertEqual(counts["permit_issued_after_forecast_date"], 1)
        self.assertEqual(counts[f"permit_older_than_{ff.MAX_PERMIT_AGE}_day_cutoff"], 1)

    def test_no_model_class_only_when_nothing_to_fall_back_on(self):
        empty = ff.fit([], {}, self.as_of)
        _, counts, _, _ = ff.classify_actuals(self.p, [self.row()], empty, self.as_of, 13)
        self.assertEqual(counts, {"no_lag_model_for_operator": 1})

    def test_only_fracs_inside_the_horizon_count(self):
        rows = [self.row(frac_start=self.as_of - timedelta(days=1)), self.row(frac_start=self.as_of + timedelta(weeks=14))]
        water, counts, _, _ = ff.classify_actuals(self.p, rows, self.model, self.as_of, 13)
        self.assertEqual(counts, {})

    def test_cause_shares_sum_to_one_and_match_actual_total(self):
        first = date(2024, 1, 1)
        results, summary = ff.backtest(self.p, self.joined, first, date(2024, 6, 30), explain=True)
        self.assertAlmostEqual(sum(summary["actual_water_by_cause_share"].values()), 1.0, delta=0.01)

    def test_intensity_ratio_is_near_one_in_a_world_with_known_intensity(self):
        _, summary = ff.backtest(self.p, self.joined, date(2024, 1, 1), date(2024, 6, 30), explain=True)
        self.assertAlmostEqual(summary["per_well_volume_actual_over_predicted_for_modelled_wells"], 1.0, delta=0.2)

    def test_intensity_ratio_direction_when_actuals_run_double(self):
        """Fracs after 2024-01-01 use twice the water the model learned from: ratio must be well above 1."""
        doubled = [dict(r, water_gal=(r["water_gal"] or 0) * 2) if r["frac_start"] > date(2024, 1, 1) else r
                   for r in self.joined]
        _, summary = ff.backtest(self.p, doubled, date(2024, 1, 1), date(2024, 3, 1), explain=True)
        self.assertGreater(summary["per_well_volume_actual_over_predicted_for_modelled_wells"], 1.4)

    def test_summary_only_cli_omits_windows_and_explain_adds_causes(self):
        import io, contextlib, csv
        permits, fracs = world()
        def write(rows, name):
            path = Path(tempfile.mkdtemp()) / name
            with open(path, "w", newline="") as h:
                w = csv.DictWriter(h, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
            return str(path)
        p, f = write(permits, "p.csv"), write(fracs, "f.csv")
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            ff.main(["backtest", "--permits", p, "--frac", f, "--summary-only", "--explain"])
        data = json.loads(out.getvalue())
        self.assertNotIn("windows", data)
        self.assertIn("actual_water_by_cause_share", data["summary"])
        self.assertIn("windows_where_model_beat_baseline", data["summary"])


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
