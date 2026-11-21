"""Tests for live.py. Run: python3 -m unittest scripts/frac/test_live.py -v
Time is faked and runs are faked unless a test says otherwise, so nothing here sleeps or waits."""
import contextlib, csv, importlib.util, io, json, os, subprocess, sys, tempfile, threading, unittest, urllib.error, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

lv = load("lv", "live.py")
tf = load("tf2", "test_frac_forecast.py")


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


def make_inputs(root):
    root = Path(root)
    (root / "permits.csv").write_text("api\n1\n")
    (root / "frac.csv").write_text("api\n1\n")
    return root / "permits.csv", root / "frac.csv"


def touch_change(path, text):
    path.write_text(text)
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))      # guarantee a different mtime


class Base(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.permits, self.frac = make_inputs(self.root)
        self.clock = Clock()
        self.runs = []
        self.fail_next = []
        os.environ.pop("FD_KILL_SWITCH", None)
        os.environ.pop("FD_KILL_LIVE", None)

    def fake_run(self, live):
        self.runs.append(self.clock())
        if self.fail_next:
            raise self.fail_next.pop(0)
        return {"run": f"r{len(self.runs)}", "watchdog_exit": 0, "watchdog_status": "OK"}

    def live(self, **kw):
        kw.setdefault("debounce", 30)
        kw.setdefault("run_fn", self.fake_run)
        return lv.Live(self.permits, self.frac, self.root / "out", clock=self.clock, quiet=True, **kw)

    def events(self, live):
        return [json.loads(x) for x in (live.state_dir / "live_events.jsonl").read_text().splitlines()]

    def kinds(self, live):
        return [e["event"] for e in self.events(live)]


class Loop(Base):
    def test_runs_once_after_the_debounce_and_not_again_while_nothing_changes(self):
        live = self.live()
        self.assertEqual(live.step(), "continue")
        self.assertEqual(self.runs, [])                                  # files just seen: still debouncing
        self.assertEqual(live.snapshot()["state"], "debouncing")
        self.clock.advance(29); live.step()
        self.assertEqual(self.runs, [])
        self.clock.advance(2); live.step()
        self.assertEqual(len(self.runs), 1)
        for _ in range(5):
            self.clock.advance(60); live.step()
        self.assertEqual(len(self.runs), 1)                              # idle: nothing changed
        self.assertEqual(live.snapshot()["state"], "idle")

    def test_a_change_triggers_exactly_one_more_run(self):
        live = self.live(debounce=0)
        live.step()
        self.assertEqual(len(self.runs), 1)
        touch_change(self.frac, "api\n1\n2\n")
        self.clock.advance(5); live.step()
        self.assertEqual(len(self.runs), 2)
        self.clock.advance(5); live.step()
        self.assertEqual(len(self.runs), 2)

    def test_a_file_that_keeps_changing_is_never_read_half_written(self):
        live = self.live()
        live.step()
        for i in range(4):                                               # being written: changes every 20 s
            self.clock.advance(20)
            touch_change(self.frac, f"api\n{i}\n")
            live.step()
        self.assertEqual(self.runs, [])                                  # the debounce timer keeps resetting
        self.clock.advance(31); live.step()
        self.assertEqual(len(self.runs), 1)

    def test_a_change_during_a_run_is_picked_up_afterwards(self):
        def run_and_change(live):
            self.runs.append(1)
            touch_change(self.frac, "api\nchanged-mid-run\n")
            return {"run": "x", "watchdog_exit": 0, "watchdog_status": "OK"}
        live = self.live(debounce=0, run_fn=run_and_change)
        live.step()
        self.assertEqual(len(self.runs), 1)
        self.clock.advance(1); live.step()
        self.assertEqual(len(self.runs), 2)

    def test_missing_inputs_wait_without_crashing_then_proceed(self):
        self.frac.unlink()
        live = self.live(debounce=0)
        self.assertEqual(live.step(), "continue")
        self.assertEqual(live.snapshot()["state"], "waiting_for_inputs")
        live.step()
        self.assertEqual(self.kinds(live).count("waiting_for_inputs"), 1)  # logged once, not every poll
        self.frac.write_text("api\n1\n")
        live.step()
        self.assertEqual(len(self.runs), 1)

    def test_no_run_on_start_treats_existing_files_as_the_baseline(self):
        live = self.live(debounce=0, run_on_start=False)
        live.step()
        self.assertEqual(self.runs, [])
        self.assertIn("baseline_recorded", self.kinds(live))
        touch_change(self.permits, "api\nnew\n")
        self.clock.advance(1); live.step()
        self.assertEqual(len(self.runs), 1)

    def test_a_watchdog_alert_is_surfaced_as_an_event_but_the_service_keeps_running(self):
        live = self.live(debounce=0, run_fn=lambda l: {"run": "r", "watchdog_exit": 2, "watchdog_status": "ALERT"})
        self.assertEqual(live.step(), "continue")
        self.assertIn("alert", self.kinds(live))


class Failures(Base):
    def test_backoff_doubles_and_retries_only_when_due(self):
        self.fail_next = [RuntimeError("boom"), RuntimeError("boom2")]
        live = self.live(debounce=0, backoff_base=60, backoff_max=3600)
        live.step()
        self.assertEqual(live.snapshot()["consecutive_failures"], 1)
        self.assertEqual(live.snapshot()["state"], "backoff")
        self.clock.advance(59); live.step()
        self.assertEqual(len(self.runs), 1)                              # not due yet
        self.clock.advance(2); live.step()
        self.assertEqual(len(self.runs), 2)                              # retried
        self.assertEqual(live.snapshot()["consecutive_failures"], 2)
        retry = [e for e in self.events(live) if e["event"] == "run_failed"]
        self.assertEqual([e["retry_in_seconds"] for e in retry], [60, 120])
        self.clock.advance(121); live.step()
        self.assertEqual(len(self.runs), 3)
        self.assertEqual(live.snapshot()["consecutive_failures"], 0)     # success resets
        self.assertEqual(live.snapshot()["state"], "idle")

    def test_backoff_is_capped(self):
        live = self.live(debounce=0, backoff_base=60, backoff_max=100, max_failures=99)
        self.fail_next = [RuntimeError("x")] * 4
        for _ in range(4):
            live.step()
            self.clock.advance(1000)
        delays = [e["retry_in_seconds"] for e in self.events(live) if e["event"] == "run_failed"]
        self.assertEqual(delays, [60, 100, 100, 100])

    def test_too_many_failures_stop_the_service_and_leave_an_alert(self):
        live = self.live(debounce=0, backoff_base=1, max_failures=3)
        self.fail_next = [RuntimeError("bad")] * 5
        results = []
        for _ in range(6):
            results.append(live.step())
            self.clock.advance(100)
            if results[-1] == "stop":
                break
        self.assertEqual(results, ["continue", "continue", "stop"])      # stops on the failing cycle itself, not a poll later
        self.assertEqual(live.snapshot()["state"], "stopped_after_failures")
        self.assertEqual(json.loads((live.state_dir / "live_status.json").read_text())["state"], "stopped_after_failures")
        self.assertIn("A person must look", (live.out / "ALERT.md").read_text())
        self.assertEqual(len(self.runs), 3)

    def test_a_tool_that_exits_is_a_failure_not_a_crash(self):
        def exits(live):
            raise SystemExit("header mismatch")
        live = self.live(debounce=0, run_fn=exits)
        self.assertEqual(live.step(), "continue")
        self.assertEqual(live.snapshot()["consecutive_failures"], 1)
        self.assertIn("header mismatch", [e for e in self.events(live) if e["event"] == "run_failed"][0]["error"])


class Control(Base):
    def test_stop_file_stops_without_running(self):
        live = self.live(debounce=0)
        (live.state_dir / "STOP").write_text("")
        self.assertEqual(live.step(), "stop")
        self.assertEqual(self.runs, [])
        self.assertEqual(live.snapshot()["state"], "stopped")

    def test_stop_file_is_one_shot_so_a_restart_is_not_blocked(self):
        live = self.live(debounce=0)
        (live.state_dir / "STOP").write_text("")
        self.assertEqual(live.step(), "stop")
        self.assertFalse((live.state_dir / "STOP").exists())
        self.assertTrue((live.state_dir / "STOP.handled").exists())
        again = self.live(debounce=0)                                    # a fresh start in the same folder
        self.assertEqual(again.step(), "continue")
        self.assertEqual(len(self.runs), 1)

    def test_a_successful_run_clears_the_old_stopped_after_failures_note(self):
        live = self.live(debounce=0)
        (live.out / "ALERT.md").write_text("# Frac live service stopped")
        live.step()
        self.assertFalse((live.out / "ALERT.md").exists())

    def test_a_failed_run_leaves_an_existing_note_alone(self):
        self.fail_next = [RuntimeError("x")]
        live = self.live(debounce=0)
        (live.out / "ALERT.md").write_text("# earlier")
        live.step()
        self.assertTrue((live.out / "ALERT.md").exists())

    def test_kill_switch_env_stops(self):
        live = self.live(debounce=0)
        os.environ["FD_KILL_LIVE"] = "1"
        try:
            self.assertEqual(live.step(), "stop")
        finally:
            del os.environ["FD_KILL_LIVE"]
        self.assertEqual(self.runs, [])

    def test_request_stop_is_honoured_at_the_next_step(self):
        live = self.live(debounce=0)
        live.request_stop("signal")
        self.assertEqual(live.step(), "stop")
        self.assertIn("signal", [e.get("reason") for e in self.events(live)])

    def test_status_file_is_complete_and_atomic(self):
        live = self.live(debounce=0)
        live.step()
        status = json.loads((live.state_dir / "live_status.json").read_text())
        for key in ("pid", "state", "started_at", "last_check", "last_run_started", "last_run_finished",
                    "last_result", "consecutive_failures", "publish", "inputs"):
            self.assertIn(key, status)
        self.assertFalse((live.state_dir / "live_status.json.tmp").exists())
        self.assertEqual(status["last_result"]["watchdog_status"], "OK")

    def test_event_log_rotates_instead_of_growing_forever(self):
        live = self.live(debounce=0)
        path = live.state_dir / "live_events.jsonl"
        path.write_text("x" * (lv.MAX_EVENTS_BYTES + 10))
        live.emit("ping")
        self.assertTrue((live.state_dir / "live_events.jsonl.1").exists())
        self.assertLess(path.stat().st_size, 1000)

    def test_second_instance_is_refused_and_a_stale_lock_is_replaced(self):
        a, b = self.live(), self.live()
        a.acquire_lock()
        with self.assertRaises(SystemExit):
            b.acquire_lock()
        a.release_lock()
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        (a.state_dir / "live.lock").write_text(str(dead.pid))            # a process that no longer exists
        b.acquire_lock()
        self.assertEqual((b.state_dir / "live.lock").read_text(), str(os.getpid()))
        b.release_lock()
        (a.state_dir / "live.lock").write_text("garbage")                # unreadable: stale
        a.acquire_lock()
        a.release_lock()


class Http(Base):
    def get(self, live, path, method="GET"):
        req = urllib.request.Request(f"http://127.0.0.1:{live.http_port}{path}", method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def setUp(self):
        super().setUp()
        self.l = self.live(debounce=0, port=0)
        self.l.start_http()
        self.addCleanup(self.l.close)

    def test_binds_to_localhost_only(self):
        self.assertEqual(self.l._server.server_address[0], "127.0.0.1")

    def test_health_and_status(self):
        self.assertEqual(self.get(self.l, "/health"), (200, "ok"))
        self.l.step()
        code, body = self.get(self.l, "/status")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["last_result"]["watchdog_status"], "OK")

    def test_summary_and_alert_are_404_until_they_exist(self):
        self.assertEqual(self.get(self.l, "/summary")[0], 404)
        self.assertEqual(self.get(self.l, "/alert")[0], 404)
        run = self.l.out / "runs" / "20261121-010101"
        run.mkdir(parents=True)
        (run / "SUMMARY.md").write_text("# hello")
        (self.l.out / "watch").mkdir()
        (self.l.out / "watch" / "ALERT.md").write_text("# careful")
        self.assertEqual(self.get(self.l, "/summary"), (200, "# hello"))
        self.assertEqual(self.get(self.l, "/alert"), (200, "# careful"))

    def test_newest_summary_wins(self):
        for stamp, text in (("20261101-000000", "old"), ("20261103-000000", "newest"), ("20261102-000000", "mid")):
            (self.l.out / "runs" / stamp).mkdir(parents=True)
            (self.l.out / "runs" / stamp / "SUMMARY.md").write_text(text)
        self.assertEqual(self.get(self.l, "/summary")[1], "newest")

    def test_it_is_read_only(self):
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            self.assertEqual(self.get(self.l, "/status", method)[0], 405, method)

    def test_unknown_paths_and_traversal_attempts_are_404(self):
        for path in ("/nope", "/../../etc/passwd", "/summary/../../x", "/%2e%2e/etc/passwd"):
            self.assertEqual(self.get(self.l, path)[0], 404, path)


class RealCycle(Base):
    def test_a_real_evaluation_cycle_writes_summaries_only(self):
        a_p, a_f = tf.world(seed=7, county="REEVES")
        permits_csv, frac_csv = self.root / "p.csv", self.root / "f.csv"
        for rows, path in ((a_p, permits_csv), (a_f, frac_csv)):
            with open(path, "w", newline="") as h:
                w = csv.DictWriter(h, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
        live = lv.Live(permits_csv, frac_csv, self.root / "out", debounce=0, clock=self.clock, quiet=True,
                       skip_tests=True, counties="REEVES", from_date="2024-01-01", lag_days=30)
        live.step()
        status = live.snapshot()
        self.assertEqual(status["consecutive_failures"], 0, self.events(live))
        self.assertIn(status["last_result"]["watchdog_status"], ("OK", "WARN", "ALERT"))
        run_dirs = list((live.out / "runs").iterdir())
        self.assertEqual(len(run_dirs), 1)
        self.assertTrue((run_dirs[0] / "SUMMARY.md").exists())
        self.assertTrue((live.out / "watch" / "status.json").exists())
        for p in live.out.rglob("*"):
            if p.is_file():
                self.assertIn(p.suffix, {".json", ".md", ".jsonl", ".1", ".lock", ".tmp", ""}, p)   # no raw CSV copies anywhere

    def test_publish_flag_is_passed_through_to_the_suite(self):
        seen = {}
        original = lv.rs.publish
        lv.rs.publish = lambda out, root, branch="x", **kw: seen.setdefault("called", str(out)) or "ok"
        try:
            a_p, a_f = tf.world(seed=7, county="REEVES")
            permits_csv, frac_csv = self.root / "p.csv", self.root / "f.csv"
            for rows, path in ((a_p, permits_csv), (a_f, frac_csv)):
                with open(path, "w", newline="") as h:
                    w = csv.DictWriter(h, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
            live = lv.Live(permits_csv, frac_csv, self.root / "out2", debounce=0, clock=self.clock, quiet=True,
                           skip_tests=True, counties="REEVES", from_date="2024-01-01", publish=True)
            live.step()
            self.assertIn("called", seen)
            off = lv.Live(permits_csv, frac_csv, self.root / "out3", debounce=0, clock=self.clock, quiet=True,
                          skip_tests=True, counties="REEVES", from_date="2024-01-01", publish=False)
            seen.clear()
            off.step()
            self.assertNotIn("called", seen)
        finally:
            lv.rs.publish = original


class PublishFailure(Base):
    def real_inputs(self):
        a_p, a_f = tf.world(seed=7, county="REEVES")
        permits_csv, frac_csv = self.root / "p.csv", self.root / "f.csv"
        for rows, path in ((a_p, permits_csv), (a_f, frac_csv)):
            with open(path, "w", newline="") as h:
                w = csv.DictWriter(h, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
        return permits_csv, frac_csv

    def test_a_failing_publish_does_not_fail_or_repeat_the_evaluation(self):
        permits_csv, frac_csv = self.real_inputs()
        original = lv.rs.publish
        def boom(*a, **kw):
            raise SystemExit("git commit failed: Author identity unknown")
        lv.rs.publish = boom
        try:
            live = lv.Live(permits_csv, frac_csv, self.root / "out", debounce=0, clock=self.clock, quiet=True,
                           skip_tests=True, counties="REEVES", from_date="2024-01-01", publish=True)
            self.assertEqual(live.step(), "continue")
            st = live.snapshot()
            self.assertEqual(st["consecutive_failures"], 0)                  # the run COUNTS
            self.assertEqual(st["state"], "idle")
            self.assertTrue(st["last_result"]["publish"].startswith("failed"))
            self.assertIn(st["last_result"]["watchdog_status"], ("OK", "WARN", "ALERT"))   # the watchdog did run
            kinds = self.kinds(live)
            self.assertIn("publish_failed", kinds)
            self.assertNotIn("run_failed", kinds)
            for _ in range(3):                                               # and it is not redone over and over
                self.clock.advance(4000); live.step()
            self.assertEqual(len(list((live.out / "runs").iterdir())), 1)
        finally:
            lv.rs.publish = original

    def test_publish_state_is_reported_ok_and_off(self):
        permits_csv, frac_csv = self.real_inputs()
        original = lv.rs.publish
        seen = {}
        lv.rs.publish = lambda out, root, branch="b", author=None: seen.setdefault("author", author) or "pushed"
        try:
            on = lv.Live(permits_csv, frac_csv, self.root / "o1", debounce=0, clock=self.clock, quiet=True, skip_tests=True,
                         counties="REEVES", from_date="2024-01-01", publish=True, publish_author="A B <a@b.co>")
            on.step()
            self.assertEqual(on.snapshot()["last_result"]["publish"], "ok")
            self.assertEqual(seen["author"], "A B <a@b.co>")
            off = lv.Live(permits_csv, frac_csv, self.root / "o2", debounce=0, clock=self.clock, quiet=True, skip_tests=True,
                          counties="REEVES", from_date="2024-01-01")
            off.step()
            self.assertEqual(off.snapshot()["last_result"]["publish"], "off")
        finally:
            lv.rs.publish = original


class Process(unittest.TestCase):
    def test_the_real_process_starts_serves_status_and_stops_on_the_stop_file(self):
        root = Path(tempfile.mkdtemp())
        permits, frac = make_inputs(root)
        out = root / "out"
        env = dict(os.environ, PYTHONUNBUFFERED="1")
        proc = subprocess.Popen([sys.executable, str(HERE / "live.py"), "--permits", str(permits), "--frac", str(frac),
                                 "--out", str(out), "--port", "0", "--poll", "0.2", "--debounce", "0", "--no-run-on-start"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        try:
            first = json.loads(proc.stdout.readline())                   # "started" event, streamed to stdout
            self.assertEqual(first["event"], "started")
            port = first["http_port"]
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as r:
                self.assertEqual(r.read().decode(), "ok")
            second = subprocess.run([sys.executable, str(HERE / "live.py"), "--permits", str(permits), "--frac", str(frac),
                                     "--out", str(out)], capture_output=True, text=True, timeout=30)
            self.assertNotEqual(second.returncode, 0)                    # a second instance is refused
            self.assertIn("refusing", second.stderr)
            (out / "STOP").write_text("")
            self.assertEqual(proc.wait(timeout=15), 0)
            self.assertFalse((out / "live.lock").exists())               # lock released on exit
            self.assertEqual(json.loads((out / "live_status.json").read_text())["state"], "stopped")
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
            proc.stdout.close()
            proc.stderr.close()


if __name__ == "__main__":
    unittest.main()
