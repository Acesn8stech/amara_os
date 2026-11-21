"""Tests for watchdog.py. Run: python3 -m unittest scripts/frac/test_watchdog.py -v"""
import contextlib, importlib.util, io, json, os, subprocess, tempfile, time, unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

wd = load("wd", "watchdog.py")
rs = wd.rs


def county(v2_total=0.2, base_total=0.3, v2_turn=0.25, base_turn=0.4, match=0.8, bias=0.0):
    return {"permits": 100, "joined": 80, "match_rate": match, "v1": {},
            "v2": {"median_abs_13wk_total_error": v2_total, "baseline_median_abs_13wk_total_error": base_total,
                   "median_model_wape": 0.5, "median_baseline_wape": 0.55, "median_13wk_total_bias": bias,
                   "turning_point_windows": {"n": 5, "model_median_abs_error": v2_turn, "baseline_median_abs_error": base_turn}}}


def write_run(folder, counties=None, tests="PASS: OK", only=None):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    counties = counties or {f"C{i}": county() for i in range(5)}
    for name in wd.RUNS:
        if only is None or name in only:
            (folder / f"{name}.json").write_text(json.dumps(counties))
    (folder / "SUMMARY.md").write_text(f"# s\n\n- unit tests: {tests}\n")
    return folder


def checks_for(run_dir, previous_dir=None, inputs=(), **kw):
    return wd.check(wd.load_run(run_dir), wd.load_run(previous_dir) if previous_dir else None, inputs, **kw)


def levels(checks):
    return {(c["level"], c["check"]) for c in checks}


class Checks(unittest.TestCase):
    def tmp(self):
        return Path(tempfile.mkdtemp())

    def test_a_healthy_run_is_ok(self):
        c = checks_for(write_run(self.tmp() / "r"))
        self.assertEqual(wd.overall(c), "OK")

    def test_incomplete_run_alerts(self):
        c = checks_for(write_run(self.tmp() / "r", only={"water_no_delay"}))
        self.assertIn(("ALERT", "complete_run"), levels(c))

    def test_failed_or_skipped_tests(self):
        self.assertIn(("ALERT", "unit_tests"), levels(checks_for(write_run(self.tmp() / "a", tests="FAIL: boom"))))
        self.assertIn(("WARN", "unit_tests"), levels(checks_for(write_run(self.tmp() / "b", tests="skipped"))))

    def test_stale_and_missing_inputs(self):
        d = self.tmp()
        run = write_run(d / "r")
        old, ancient, fresh = d / "old.csv", d / "ancient.csv", d / "fresh.csv"
        for p, days in ((old, 60), (ancient, 100), (fresh, 1)):
            p.write_text("x")
            t = time.time() - days * 86400
            os.utime(p, (t, t))
        self.assertIn(("WARN", "inputs_fresh"), levels(checks_for(run, inputs=[old])))
        self.assertIn(("ALERT", "inputs_fresh"), levels(checks_for(run, inputs=[ancient])))
        self.assertEqual(wd.overall(checks_for(run, inputs=[fresh])), "OK")
        self.assertIn(("ALERT", "inputs_exist"), levels(checks_for(run, inputs=[d / "nope.csv"])))

    def test_beats_baseline_threshold_is_60_percent_of_counties(self):
        def mix(n_good, n_bad):
            cs = {f"G{i}": county() for i in range(n_good)}
            cs.update({f"B{i}": county(v2_total=0.5, base_total=0.3, v2_turn=0.6, base_turn=0.4) for i in range(n_bad)})
            return cs
        ok = checks_for(write_run(self.tmp() / "ok", mix(3, 2)))            # exactly 60%
        bad = checks_for(write_run(self.tmp() / "bad", mix(2, 3)))          # 40%
        self.assertNotIn(("ALERT", "beats_baseline"), levels(ok))
        self.assertIn(("ALERT", "beats_baseline"), levels(bad))

    def test_no_county_results_alerts(self):
        c = checks_for(write_run(self.tmp() / "r", {"X": {"status": "NO_JOINED_ROWS", "permits": 0}}))
        self.assertIn(("ALERT", "beats_baseline"), levels(c))

    def test_large_bias_in_over_half_the_counties_warns(self):
        cs = {f"C{i}": county(bias=0.3 if i < 3 else 0.0) for i in range(5)}
        self.assertIn(("WARN", "bias"), levels(checks_for(write_run(self.tmp() / "a", cs))))
        cs = {f"C{i}": county(bias=0.3 if i < 2 else 0.0) for i in range(5)}
        self.assertNotIn(("WARN", "bias"), levels(checks_for(write_run(self.tmp() / "b", cs))))

    def test_low_match_is_information_only(self):
        cs = {f"C{i}": county(match=0.4 if i == 0 else 0.8) for i in range(5)}
        c = checks_for(write_run(self.tmp() / "r", cs))
        self.assertIn(("INFO", "low_match"), levels(c))
        self.assertEqual(wd.overall(c), "OK")

    def test_regression_needs_both_relative_and_absolute_worsening(self):
        d = self.tmp()
        prev = write_run(d / "prev", {f"C{i}": county(v2_total=0.20) for i in range(5)})
        one = {f"C{i}": county(v2_total=0.28 if i == 0 else 0.20) for i in range(5)}      # +40% and +0.08
        small = {f"C{i}": county(v2_total=0.23 if i == 0 else 0.20) for i in range(5)}    # +15% only
        three = {f"C{i}": county(v2_total=0.30 if i < 3 else 0.20) for i in range(5)}
        self.assertIn(("WARN", "regression"), levels(checks_for(write_run(d / "a", one), prev)))
        self.assertNotIn(("WARN", "regression"), levels(checks_for(write_run(d / "b", small), prev)))
        self.assertIn(("ALERT", "regression"), levels(checks_for(write_run(d / "c", three), prev)))

    def test_tiny_absolute_changes_never_count_even_if_relatively_large(self):
        d = self.tmp()
        prev = write_run(d / "prev", {f"C{i}": county(v2_total=0.02) for i in range(5)})
        now = write_run(d / "now", {f"C{i}": county(v2_total=0.04) for i in range(5)})     # +100% but only +0.02
        self.assertNotIn("regression", {c["check"] for c in checks_for(now, prev)})

    def test_no_previous_run_means_no_regression_check(self):
        self.assertNotIn("regression", {c["check"] for c in checks_for(write_run(self.tmp() / "r"))})

    def test_latest_two_picks_the_two_newest_by_name(self):
        d = self.tmp()
        for stamp in ("20261101-010101", "20261103-010101", "20261102-010101"):
            write_run(d / stamp)
        latest, prev = wd.latest_two(d)
        self.assertEqual((latest.name, prev.name), ("20261103-010101", "20261102-010101"))
        single, none = wd.latest_two(write_run(d / "solo"))
        self.assertEqual((single.name, none), ("solo", None))


class Outputs(unittest.TestCase):
    def run_main(self, *args):
        with contextlib.redirect_stdout(io.StringIO()):
            return wd.main(list(args))

    def test_exit_codes_and_files_for_ok_warn_alert(self):
        d = Path(tempfile.mkdtemp())
        ok, warn, alert = write_run(d / "ok"), write_run(d / "warn", tests="skipped"), write_run(d / "alert", tests="FAIL: x")
        self.assertEqual(self.run_main("--results", str(ok), "--out", str(d / "o1")), 0)
        self.assertEqual(self.run_main("--results", str(warn), "--out", str(d / "o2")), 1)
        self.assertEqual(self.run_main("--results", str(alert), "--out", str(d / "o3")), 2)
        self.assertFalse((d / "o1" / "ALERT.md").exists())
        self.assertTrue((d / "o3" / "ALERT.md").exists())
        self.assertEqual(json.loads((d / "o3" / "status.json").read_text())["status"], "ALERT")

    def test_alert_is_plain_english_and_names_the_human_actions_and_the_rules(self):
        d = Path(tempfile.mkdtemp())
        self.run_main("--results", str(write_run(d / "r", tests="FAIL: x")), "--out", str(d / "o"))
        text = (d / "o" / "ALERT.md").read_text()
        for needle in ("What to do", "must not accept website terms", "autonomy-policy.json", "unit tests"):
            self.assertIn(needle, text)
        status = json.loads((d / "o" / "status.json").read_text())
        self.assertIn("forbidden", status["policy"])                      # any bot reading status.json sees the rules

    def test_a_later_ok_run_clears_the_old_alert(self):
        d = Path(tempfile.mkdtemp())
        self.run_main("--results", str(write_run(d / "bad", tests="FAIL: x")), "--out", str(d / "o"))
        self.assertTrue((d / "o" / "ALERT.md").exists())
        self.run_main("--results", str(write_run(d / "good")), "--out", str(d / "o"))
        self.assertFalse((d / "o" / "ALERT.md").exists())

    def test_kill_switch_does_nothing(self):
        d = Path(tempfile.mkdtemp())
        os.environ["FD_KILL_WATCH"] = "1"
        try:
            rc = self.run_main("--results", str(write_run(d / "r", tests="FAIL: x")), "--out", str(d / "o"))
        finally:
            del os.environ["FD_KILL_WATCH"]
        self.assertEqual(rc, 0)
        self.assertFalse((d / "o").exists())

    def test_nothing_to_read_is_an_error_not_a_pass(self):
        d = Path(tempfile.mkdtemp())
        (d / "empty").mkdir()
        with self.assertRaises(SystemExit):
            self.run_main("--results", str(d / "empty"), "--out", str(d / "o"))

    def test_it_never_writes_outside_its_output_folder(self):
        d = Path(tempfile.mkdtemp())
        run = write_run(d / "r", tests="FAIL: x")
        before = {p.name for p in run.iterdir()}
        self.run_main("--results", str(run), "--out", str(d / "o"))
        self.assertEqual({p.name for p in run.iterdir()}, before)


class FromGit(unittest.TestCase):
    def test_reads_the_two_newest_published_runs_from_a_branch(self):
        env = {k: os.environ.get(k) for k in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL")}
        os.environ.update({"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})
        try:
            root = Path(tempfile.mkdtemp())
            bare, repo = root / "origin.git", root / "repo"
            g = lambda *a, cwd=repo: subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True, check=True)
            g("init", "-q", "--bare", str(bare), cwd=root)
            g("init", "-q", "-b", "main", str(repo), cwd=root)
            (repo / "README.md").write_text("x")
            g("add", "-A"); g("commit", "-q", "-m", "init"); g("remote", "add", "origin", str(bare))
            good = write_run(root / "good")
            bad = write_run(root / "bad", tests="FAIL: broke")
            rs.publish(good, repo)
            time.sleep(1.1)                                                  # distinct timestamps
            rs.publish(bad, repo)
            g("fetch", "-q", "origin")
            with contextlib.redirect_stdout(io.StringIO()):
                rc = wd.main(["--results-ref", "origin/claude/frac-results", "--repo", str(repo), "--out", str(root / "o")])
            self.assertEqual(rc, 2)                                          # the NEWEST published run is the failing one
            self.assertEqual(json.loads((root / "o" / "status.json").read_text())["status"], "ALERT")
        finally:
            for k, v in env.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)

    def test_a_ref_without_results_is_an_error(self):
        root = Path(tempfile.mkdtemp())
        repo = root / "repo"
        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, capture_output=True)
        (repo / "a").write_text("x")
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(repo), "commit", "-q", "-m", "i"], check=True, capture_output=True)
        with self.assertRaises(SystemExit):
            wd.extract_from_git("main", repo)


if __name__ == "__main__":
    unittest.main()
