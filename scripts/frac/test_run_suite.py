"""Tests for run_suite.py. Run: python3 -m unittest scripts/frac/test_run_suite.py -v
Publishing is tested against a real local git repo standing in for GitHub."""
import csv, importlib.util, json, os, subprocess, tempfile, unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

rs = load("rs", "run_suite.py")
tf = load("tf", "test_frac_forecast.py")      # reuse its synthetic world()


def write_csv(rows, path):
    with open(path, "w", newline="") as h:
        w = csv.DictWriter(h, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    return str(path)


def synthetic_inputs():
    d = Path(tempfile.mkdtemp())
    a_p, a_f = tf.world(seed=7, county="REEVES")
    b_p, b_f = tf.world(seed=9, county="PECOS")
    for r in b_p + b_f:
        r["api"] = "42" + "371" + r["api"][5:]
    return write_csv(a_p + b_p, d / "permits.csv"), write_csv(a_f + b_f, d / "frac.csv"), d


class Verdict(unittest.TestCase):
    RES = {
        "A": {"permits": 1, "joined": 1, "match_rate": 0.8,
              "v1": {"median_abs_13wk_total_error": .5},
              "v2": {"median_abs_13wk_total_error": .2, "baseline_median_abs_13wk_total_error": .3,
                     "median_model_wape": .5, "median_baseline_wape": .6, "median_13wk_total_bias": 0.01,
                     "turning_point_windows": {"n": 5, "model_median_abs_error": .2, "baseline_median_abs_error": .4}}},
        "B": {"permits": 1, "joined": 1, "match_rate": 0.5,
              "v1": {}, "v2": {"median_abs_13wk_total_error": .5, "baseline_median_abs_13wk_total_error": .3,
                               "median_model_wape": 1.2, "median_baseline_wape": 1.0, "median_13wk_total_bias": -0.3,
                               "turning_point_windows": {"n": 5, "model_median_abs_error": .6, "baseline_median_abs_error": .4}}},
        "C": {"status": "NO_JOINED_ROWS", "permits": 0},
    }

    def test_verdict_counts_only_counties_that_ran(self):
        v = rs.verdict(self.RES)
        self.assertEqual(v, {"counties": 2, "better_on_13wk_total": "1 of 2",
                             "better_at_turning_points": "1 of 2", "better_weekly": "1 of 2"})

    def test_table_has_a_row_per_county_and_marks_skipped_ones(self):
        t = rs.table(self.RES)
        self.assertIn("| A | 80 |", t)
        self.assertIn("| B | 50 |", t)
        self.assertIn("| C | - | NO_JOINED_ROWS", t)

    def test_summary_contains_verdicts_caveats_and_hand_check(self):
        s = rs.build_summary({"x": ("Water, no filing delay", self.RES)},
                             {"stamp": "t", "counties": "A, B", "from_date": "2016-01-01", "lag_days": 45},
                             "PASS: OK", ["[A] API 1"])
        for needle in ("better on 13-week totals in 1 of 2", "Hand check", "Read this before trusting", "- [A] API 1",
                       "assumed filing delay", "unit tests: PASS: OK"):
            self.assertIn(needle, s)


class Suite(unittest.TestCase):
    def test_runs_end_to_end_and_writes_every_output(self):
        permits, fracs, d = synthetic_inputs()
        out = d / "results"
        rc = rs.main(["--permits", permits, "--frac", fracs, "--out", str(out), "--counties", "reeves,pecos",
                      "--from-date", "2024-01-01", "--lag-days", "30", "--skip-tests"])
        self.assertEqual(rc, 0)
        names = {p.name for p in out.iterdir()}
        self.assertEqual(names, {"SUMMARY.md", "water_no_delay.json", "water_with_delay.json",
                                 "sand_no_delay.json", "sand_with_delay.json"})
        summary = (out / "SUMMARY.md").read_text()
        for needle in ("Water, no filing delay", "Water, assumed 30-day filing delay", "Sand (recorded pounds)",
                       "| REEVES |", "| PECOS |", "[REEVES] API", "unit tests: skipped"):
            self.assertIn(needle, summary)
        delayed = json.loads((out / "water_with_delay.json").read_text())
        self.assertIn("v2_not_lag_aware", delayed["REEVES"])
        self.assertNotIn("v2_not_lag_aware", json.loads((out / "water_no_delay.json").read_text())["REEVES"])

    def test_failing_unit_tests_stop_the_run_before_any_backtest(self):
        permits, fracs, d = synthetic_inputs()
        out = d / "results"
        original = rs.run_tests
        rs.run_tests = lambda root: (False, "boom")
        try:
            rc = rs.main(["--permits", permits, "--frac", fracs, "--out", str(out), "--counties", "reeves"])
        finally:
            rs.run_tests = original
        self.assertEqual(rc, 1)
        self.assertEqual({p.name for p in out.iterdir()}, {"SUMMARY.md"})
        self.assertIn("boom", (out / "SUMMARY.md").read_text())

    def test_a_failed_publish_does_not_fail_the_run_and_results_stay_on_disk(self):
        permits, fracs, d = synthetic_inputs()
        out = d / "results"
        original = rs.publish
        def boom(*a, **kw):
            raise SystemExit("git commit failed: Author identity unknown")
        rs.publish = boom
        try:
            rc = rs.main(["--permits", permits, "--frac", fracs, "--out", str(out), "--counties", "reeves",
                          "--from-date", "2024-01-01", "--skip-tests", "--publish"])
        finally:
            rs.publish = original
        self.assertEqual(rc, 0)
        self.assertEqual((rs.LAST_PUBLISH["attempted"], rs.LAST_PUBLISH["ok"]), (True, False))
        self.assertIn("Author identity unknown", rs.LAST_PUBLISH["message"])
        self.assertTrue((out / "SUMMARY.md").exists())
        self.assertTrue((out / "water_no_delay.json").exists())

    def test_publish_state_resets_on_the_next_run_and_passes_the_author_through(self):
        permits, fracs, d = synthetic_inputs()
        seen = {}
        original = rs.publish
        rs.publish = lambda out, root, branch="b", author=None: seen.setdefault("author", author) or "pushed"
        try:
            rs.main(["--permits", permits, "--frac", fracs, "--out", str(d / "a"), "--counties", "reeves",
                     "--from-date", "2024-01-01", "--skip-tests", "--publish", "--publish-author", "A B <a@b.co>"])
            self.assertEqual((rs.LAST_PUBLISH["ok"], seen["author"]), (True, "A B <a@b.co>"))
            rs.main(["--permits", permits, "--frac", fracs, "--out", str(d / "b"), "--counties", "reeves",
                     "--from-date", "2024-01-01", "--skip-tests"])
            self.assertFalse(rs.LAST_PUBLISH["attempted"])
        finally:
            rs.publish = original

    def test_bad_from_date_is_rejected(self):
        permits, fracs, d = synthetic_inputs()
        with self.assertRaises(SystemExit):
            rs.main(["--permits", permits, "--frac", fracs, "--out", str(d / "o"), "--skip-tests", "--from-date", "nope"])


class Publish(unittest.TestCase):
    def setUp(self):
        self.env = {k: os.environ.get(k) for k in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL")}
        os.environ.update({"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})
        root = Path(tempfile.mkdtemp())
        self.bare, self.repo = root / "origin.git", root / "repo"
        self.git("init", "-q", "--bare", str(self.bare), cwd=root)
        self.git("init", "-q", "-b", "main", str(self.repo), cwd=root)
        (self.repo / "README.md").write_text("code lives here")
        (self.repo / "secret_data.csv").write_text("owner,address\n")          # must never be pushed by publish
        self.git("add", "-A", cwd=self.repo)
        self.git("commit", "-q", "-m", "init", cwd=self.repo)
        self.git("remote", "add", "origin", str(self.bare), cwd=self.repo)
        self.out = root / "out"
        self.out.mkdir()
        (self.out / "SUMMARY.md").write_text("# results")
        (self.out / "water.json").write_text("{}")

    def tearDown(self):
        for k, v in self.env.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)

    def git(self, *a, cwd):
        p = subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout

    def remote_files(self, branch="claude/frac-results"):
        return self.git("--git-dir", str(self.bare), "ls-tree", "-r", "--name-only", branch, cwd=self.repo).split()

    def test_pushes_only_the_summary_files_to_a_results_branch(self):
        msg = rs.publish(self.out, self.repo)
        self.assertIn("claude/frac-results", msg)
        files = self.remote_files()
        self.assertEqual(len(files), 2)
        self.assertTrue(all(f.startswith("results/") for f in files))
        self.assertFalse(any(f.endswith("secret_data.csv") or f.endswith("README.md") for f in files))   # no code, no data

    def test_working_branch_and_worktrees_are_left_alone(self):
        rs.publish(self.out, self.repo)
        self.assertEqual(self.git("rev-parse", "--abbrev-ref", "HEAD", cwd=self.repo).strip(), "main")
        self.assertTrue((self.repo / "secret_data.csv").exists())
        self.assertEqual(self.git("worktree", "list", cwd=self.repo).count("\n"), 1)         # temp worktree cleaned up
        local = self.git("branch", cwd=self.repo)
        self.assertNotIn("claude/frac-results", local)                                        # no stray local branch

    def test_an_existing_local_branch_of_the_same_name_is_not_deleted(self):
        self.git("branch", "claude/frac-results", cwd=self.repo)
        rs.publish(self.out, self.repo)
        self.assertIn("claude/frac-results", self.git("branch", cwd=self.repo))

    def test_a_second_publish_adds_another_folder_instead_of_overwriting(self):
        rs.publish(self.out, self.repo)
        (self.out / "SUMMARY.md").write_text("# newer results")
        rs.publish(self.out, self.repo)
        dirs = {f.split("/")[1] for f in self.remote_files()}
        self.assertEqual(len(dirs), 2)

    def test_refuses_csv_zip_or_oversized_files_and_creates_nothing(self):
        for name in ("frac.csv", "FracFocusCSV.zip", "notes.pdf"):
            (self.out / name).write_text("x")
            with self.assertRaises(SystemExit):
                rs.publish(self.out, self.repo)
            (self.out / name).unlink()
        (self.out / "big.json").write_bytes(b"x" * (rs.MAX_PUBLISH_BYTES + 1))
        with self.assertRaises(SystemExit):
            rs.publish(self.out, self.repo)
        heads = subprocess.run(["git", "--git-dir", str(self.bare), "branch"], capture_output=True, text=True).stdout
        self.assertNotIn("claude/frac-results", heads)

    def author_of_last_results_commit(self):
        return self.git("--git-dir", str(self.bare), "log", "-1", "--format=%an|%ae", "claude/frac-results", cwd=self.repo).strip()

    def no_identity(self):
        """Recreate a machine where git has no author identity at all (what happened on the Mac)."""
        cfg = self.out.parent / "empty-gitconfig"
        cfg.write_text("[user]\n\tuseConfigOnly = true\n")
        for k in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
            os.environ.pop(k, None)
        saved = {k: os.environ.get(k) for k in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM")}
        os.environ["GIT_CONFIG_GLOBAL"], os.environ["GIT_CONFIG_SYSTEM"] = str(cfg), os.devnull
        def restore():
            for k, v in saved.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        self.addCleanup(restore)

    def test_git_really_has_no_identity_in_that_setup(self):
        self.no_identity()
        p = subprocess.run(["git", "var", "GIT_AUTHOR_IDENT"], cwd=self.repo, capture_output=True, text=True)
        self.assertNotEqual(p.returncode, 0)                       # so the next test is a real reproduction

    def test_publishes_with_a_labelled_bot_author_when_git_has_no_identity(self):
        self.no_identity()
        rs.publish(self.out, self.repo)
        self.assertEqual(self.author_of_last_results_commit(), "|".join(rs.FALLBACK_AUTHOR))
        self.assertIn("automated", rs.FALLBACK_AUTHOR[0])
        self.assertTrue(rs.FALLBACK_AUTHOR[1].endswith(".invalid"))  # cannot be a real person's address

    def test_an_explicit_author_wins_even_without_a_git_identity(self):
        self.no_identity()
        rs.publish(self.out, self.repo, author="Desk Owner <owner@example.com>")
        self.assertEqual(self.author_of_last_results_commit(), "Desk Owner|owner@example.com")

    def test_a_configured_git_identity_is_used_untouched(self):
        rs.publish(self.out, self.repo)                              # setUp provides identity "t <t@t>"
        self.assertEqual(self.author_of_last_results_commit(), "t|t@t")

    def test_an_identity_saved_in_git_config_is_respected_not_overridden_by_the_bot(self):
        self.no_identity()                                             # no env identity, no global identity
        self.git("config", "user.name", "Configured Person", cwd=self.repo)
        self.git("config", "user.email", "person@example.com", cwd=self.repo)
        rs.publish(self.out, self.repo)
        self.assertEqual(self.author_of_last_results_commit(), "Configured Person|person@example.com")

    def test_a_malformed_author_is_refused(self):
        for bad in ("just a name", "<only@email.com>", "Name <not-an-email>"):
            with self.assertRaises(SystemExit):
                rs.publish(self.out, self.repo, author=bad)

    def test_the_fallback_identity_is_not_written_into_git_config(self):
        self.no_identity()
        rs.publish(self.out, self.repo)
        p = subprocess.run(["git", "config", "--local", "user.name"], cwd=self.repo, capture_output=True, text=True)
        self.assertEqual(p.stdout.strip(), "")

    def test_empty_output_dir_is_refused(self):
        empty = self.out.parent / "empty"
        empty.mkdir()
        with self.assertRaises(SystemExit):
            rs.publish(empty, self.repo)


if __name__ == "__main__":
    unittest.main()
