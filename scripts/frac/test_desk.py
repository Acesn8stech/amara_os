"""Tests for desk.py. Run: python3 -m unittest scripts/frac/test_desk.py -v"""
import contextlib, importlib.util, io, json, re, tempfile, time, unittest
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("desk", HERE / "desk.py")
desk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(desk)

# "Now" is the real clock, never a hardcoded date: desk.main() reads the real clock, so a fixed date made these
# tests pass or fail depending on the time of day and the timezone (it failed on the Mac at 13:27 PST).
NOW = float(int(time.time()))
iso = lambda secs_ago: datetime.fromtimestamp(NOW - secs_ago).isoformat(timespec="seconds")
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def county(model, base, tmodel=0.25, tbase=0.4, match=0.8, bias=0.0):
    return {"permits": 10, "joined": 8, "match_rate": match, "v1": {},
            "v2": {"median_abs_13wk_total_error": model, "baseline_median_abs_13wk_total_error": base,
                   "median_13wk_total_bias": bias,
                   "turning_point_windows": {"n": 5, "model_median_abs_error": tmodel, "baseline_median_abs_error": tbase}}}


def make_dir(state="idle", heartbeat_ago=5, water=None, sand=None, watch="OK", events=(), checks=(), publish="ok", no_watch=False):
    d = Path(tempfile.mkdtemp())
    (d / "live_status.json").write_text(json.dumps({
        "pid": 1, "state": state, "started_at": iso(7200), "last_check": iso(heartbeat_ago), "last_run_finished": iso(240),
        "last_result": {"watchdog_status": watch, "publish": publish}, "consecutive_failures": 0, "publish": True, "http_port": 8765}))
    run = d / "runs" / "20261121-130000"
    run.mkdir(parents=True)
    (run / "water_no_delay.json").write_text(json.dumps(water if water is not None else {
        "UPTON": county(0.171, 0.310), "REEVES": county(0.233, 0.256, 0.35, 0.41), "PECOS": county(0.737, 0.458, 0.7, 0.675, 0.36),
        "WARD": county(0.40, 0.394)}))
    (run / "sand_no_delay.json").write_text(json.dumps(sand if sand is not None else {"UPTON": county(0.181, 0.332)}))
    (d / "watch").mkdir()
    if not no_watch:
        (d / "watch" / "status.json").write_text(json.dumps({"status": watch, "run": "20261121-130000", "checks": list(checks)}))
    (d / "live_events.jsonl").write_text("\n".join(json.dumps(e) for e in events) + ("\n" if events else ""))
    return d


def frame(d, color=False, **kw):
    return desk.render(desk.load_state(d), now=NOW, color=color, **kw)


class Board(unittest.TestCase):
    def test_counties_are_ranked_by_edge_best_first_with_the_right_numbers(self):
        text, _, _ = frame(make_dir())
        order = [c for c in ("UPTON", "REEVES", "WARD", "PECOS") if c in text]
        positions = [text.index(c) for c in order]
        self.assertEqual(order, ["UPTON", "REEVES", "WARD", "PECOS"])           # +45%, +9%, -2%, -61%
        self.assertEqual(positions, sorted(positions))
        self.assertIn(" +45%", text)                                            # (0.310-0.171)/0.310
        self.assertIn(" -61%", text)

    def test_signals_up_flat_down(self):
        rows, _ = desk.board_rows({"A": county(0.1, 0.3), "B": county(0.30, 0.31), "C": county(0.5, 0.3),
                                   "D": county(0.1, 0.3, tmodel=0.5, tbase=0.3), "E": county(None, None)})
        sig = {r["county"]: r["signal"] for r in rows}
        self.assertEqual(sig["A"], "▲")
        self.assertEqual(sig["B"], "●")
        self.assertEqual(sig["C"], "▼")
        self.assertEqual(sig["D"], "●")           # better in totals but worse at turning points: not a clear buy
        self.assertEqual(sig["E"], "·")

    def test_turning_point_losers_with_weak_totals_are_marked_down(self):
        rows, _ = desk.board_rows({"X": county(0.30, 0.31, tmodel=0.7, tbase=0.3)})
        self.assertEqual(rows[0]["signal"], "▼")

    def test_edge_is_none_when_the_baseline_is_missing_or_zero(self):
        self.assertIsNone(desk.edge(0.2, None))
        self.assertIsNone(desk.edge(None, 0.2))
        self.assertIsNone(desk.edge(0.2, 0))
        self.assertAlmostEqual(desk.edge(0.1, 0.4), 0.75)

    def test_counties_without_a_result_are_listed_not_hidden(self):
        d = make_dir(water={"UPTON": county(0.1, 0.3), "TINY": {"status": "NO_JOINED_ROWS", "permits": 0}})
        text, _, _ = frame(d)
        self.assertIn("TINY", text)
        self.assertIn("no result", text)

    def test_water_and_sand_are_separate_boards(self):
        text, _, _ = frame(make_dir())
        self.assertIn("BOARD · WATER", text)
        self.assertIn("BOARD · SAND", text)

    def test_empty_board_says_it_is_waiting(self):
        d = make_dir()
        for p in (d / "runs").rglob("*.json"):
            p.unlink()
        text, _, _ = frame(d)
        self.assertIn("waiting for the first completed run", text)


class StatusAndRisk(unittest.TestCase):
    def test_live_and_healthy_exit_zero(self):
        text, health, risk = frame(make_dir())
        self.assertIn("● LIVE", text)
        self.assertIn("IDLE", text)
        self.assertIn("publish ON", text)
        self.assertEqual(desk.exit_code(health, risk), 0)

    def test_a_silent_service_is_a_feed_down_alarm(self):
        text, health, risk = frame(make_dir(heartbeat_ago=500))
        self.assertIn("FEED DOWN", text)
        self.assertEqual(desk.exit_code(health, risk), 3)

    def test_a_heartbeat_just_inside_the_limit_is_still_live(self):
        _, health, _ = frame(make_dir(heartbeat_ago=desk.STALE_SECONDS - 1))
        self.assertEqual(health, "up")
        _, health, _ = frame(make_dir(heartbeat_ago=desk.STALE_SECONDS + 1))
        self.assertEqual(health, "down")

    def test_a_stopped_service_is_down_even_with_a_fresh_heartbeat(self):
        text, health, risk = frame(make_dir(state="stopped_after_failures", heartbeat_ago=1))
        self.assertIn("STOPPED", text)
        self.assertEqual(desk.exit_code(health, risk), 3)

    def test_no_status_file_at_all_is_feed_down(self):
        d = Path(tempfile.mkdtemp())
        text, health, risk = frame(d)
        self.assertIn("FEED DOWN", text)
        self.assertEqual(desk.exit_code(health, risk), 3)

    def test_watchdog_warn_and_alert_set_exit_codes_and_show_the_reasons(self):
        warn = make_dir(watch="WARN", checks=[{"level": "WARN", "check": "bias", "message": "forecast runs high", "action": ""}])
        alert = make_dir(watch="ALERT", checks=[{"level": "ALERT", "check": "x", "message": "inputs 100 days old", "action": ""}])
        text, h, r = frame(warn)
        self.assertEqual(desk.exit_code(h, r), 1)
        self.assertIn("forecast runs high", text)
        text, h, r = frame(alert)
        self.assertEqual(desk.exit_code(h, r), 2)
        self.assertIn("inputs 100 days old", text)

    def test_a_failing_publish_is_a_warning_not_an_outage(self):
        text, h, r = frame(make_dir(publish="failed: git commit failed: Author identity unknown"))
        self.assertEqual(desk.exit_code(h, r), 1)
        self.assertIn("publish failing; results are complete on disk", text)
        self.assertIn("Author identity unknown", text)
        self.assertTrue(all(len(l) <= 110 for l in text.splitlines()))       # and nothing important is cut off
        self.assertIn("● LIVE", text)

    def test_a_failing_publish_never_hides_an_alert(self):
        d = make_dir(watch="ALERT", publish="failed: x", checks=[{"level": "ALERT", "check": "c", "message": "bad", "action": ""}])
        _, h, r = frame(d)
        self.assertEqual(desk.exit_code(h, r), 2)

    def test_publish_ok_or_off_adds_no_warning(self):
        for state in ("ok", "off"):
            text, h, r = frame(make_dir(publish=state))
            self.assertEqual(desk.exit_code(h, r), 0, state)
            self.assertNotIn("publish failing; results are complete on disk", text)

    def test_a_failing_publish_shows_even_before_the_watchdog_reports(self):
        text, h, r = frame(make_dir(publish="failed: nope", no_watch=True))
        self.assertEqual(desk.exit_code(h, r), 1)
        self.assertIn("publish failing; results are complete on disk", text)

    def test_down_outranks_an_alert_in_the_exit_code(self):
        self.assertEqual(desk.exit_code("down", "ALERT"), 3)

    def test_age_formatting(self):
        self.assertEqual([desk.age(s) for s in (5, 89, 90, 5399, 5400, 172799, 172800)],
                         ["5s", "89s", "1m", "89m", "1h", "47h", "2d"])


class TapeAndRecap(unittest.TestCase):
    EVENTS = [{"t": iso(3000), "event": "run_finished", "run": "a"}, {"t": iso(2000), "event": "run_failed", "error": "boom"},
              {"t": iso(1000), "event": "alert", "message": "watchdog ALERT"}, {"t": iso(100), "event": "run_finished", "run": "b"}]

    def test_tape_is_newest_first_and_limited(self):
        text, _, _ = frame(make_dir(events=self.EVENTS), event_rows=3)
        body = text.split("── TAPE")[1]
        self.assertLess(body.index("run=b"), body.index("watchdog ALERT"))
        self.assertNotIn("run=a", body)                                         # only the newest 3 rows

    def test_quiet_tape(self):
        text, _, _ = frame(make_dir())
        self.assertIn("quiet", text)

    def test_recap_counts_only_the_window(self):
        old = [{"t": iso(200000), "event": "run_failed"}]
        r = desk.recap(old + self.EVENTS, NOW, 1)                               # last hour: events 3000, 2000, 1000, 100 s ago
        self.assertEqual((r["runs_finished"], r["runs_failed"], r["alerts"]), (2, 1, 1))
        self.assertEqual(r["last_alert"], "watchdog ALERT")
        half = desk.recap(old + self.EVENTS, NOW, 0.5)                          # last 30 min: only 1000 s and 100 s ago
        self.assertEqual((half["runs_finished"], half["runs_failed"], half["alerts"]), (1, 0, 1))
        wide = desk.recap(old + self.EVENTS, NOW, 100)
        self.assertEqual(wide["runs_failed"], 2)

    def test_a_corrupt_event_line_is_skipped_not_fatal(self):
        d = make_dir(events=self.EVENTS)
        with open(d / "live_events.jsonl", "a") as h:
            h.write("{not json\n")
        self.assertEqual(len(desk.load_state(d)["events"]), 4)


class ClockIndependence(unittest.TestCase):
    def test_the_fixture_clock_is_the_real_clock(self):
        self.assertLess(abs(NOW - time.time()), 120)

    def test_a_heartbeat_far_in_the_future_is_not_treated_as_healthy(self):
        text, health, risk = frame(make_dir(heartbeat_ago=-(desk.STALE_SECONDS + 60)))
        self.assertEqual(health, "down")
        self.assertIn("clock", text.lower())
        self.assertEqual(desk.exit_code(health, risk), 3)

    def test_a_small_clock_difference_is_tolerated(self):
        _, health, _ = frame(make_dir(heartbeat_ago=-5))
        self.assertEqual(health, "up")


class Output(unittest.TestCase):
    def test_colour_adds_codes_and_no_colour_has_none(self):
        d = make_dir()
        coloured, _, _ = frame(d, color=True)
        plain, _, _ = frame(d, color=False)
        self.assertIn("\x1b[", coloured)
        self.assertNotIn("\x1b[", plain)
        self.assertEqual(ANSI.sub("", coloured).replace(" ", "")[:200], plain.replace(" ", "")[:200])   # same content

    def test_plain_lines_never_exceed_the_width(self):
        d = make_dir(events=[{"t": iso(5), "event": "run_failed", "error": "x" * 400}])
        text, _, _ = frame(d, width=80)
        self.assertTrue(all(len(line) <= 80 for line in text.splitlines()))

    def test_the_disclaimer_is_never_truncated_at_the_default_width(self):
        text, _, _ = frame(make_dir())
        self.assertIn("Backtest monitoring only: not a live forecast, not advice.", text)
        legend = [l for l in text.splitlines() if l.startswith("EDGE =")][0]
        self.assertIn("▼ worse", legend)

    def test_once_mode_prints_one_frame_and_returns_the_health_code(self):
        d = make_dir(heartbeat_ago=500)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = desk.main(["--dir", str(d), "--once", "--no-color"])
        self.assertEqual(rc, 3)
        self.assertEqual(out.getvalue().count("AMARA FRAC DESK"), 1)

    def test_recap_mode_prints_json(self):
        d = make_dir(events=TapeAndRecap.EVENTS)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = desk.main(["--dir", str(d), "--recap", "24"])
        self.assertEqual(rc, 0)
        self.assertIn("runs_finished", json.loads(out.getvalue()))

    def test_it_changes_nothing_on_disk(self):
        d = make_dir(events=TapeAndRecap.EVENTS)
        before = {p.relative_to(d).as_posix(): p.read_bytes() for p in d.rglob("*") if p.is_file()}
        frame(d)
        with contextlib.redirect_stdout(io.StringIO()):
            desk.main(["--dir", str(d), "--once", "--no-color"])
        self.assertEqual({p.relative_to(d).as_posix(): p.read_bytes() for p in d.rglob("*") if p.is_file()}, before)


if __name__ == "__main__":
    unittest.main()
