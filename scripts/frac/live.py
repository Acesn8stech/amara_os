#!/usr/bin/env python3
"""Live service for the frac-demand lane. It stays running (no schedule), reacts when the input files change,
re-runs the evaluation and the watchdog, and keeps its state readable at all times.

  python3 scripts/frac/live.py --permits ~/frac-data/work/permits.csv --frac ~/frac-data/work/frac_v2.csv \\
      --out ~/frac-data/work/live --port 8765 [--publish]

What it does, forever, until stopped:
  * polls the two input files; when either changes and stays unchanged for --debounce seconds (so a file that is
    still being written is never read half-done), it runs run_suite then watchdog
  * writes live_status.json (state, last result, failures) and a JSON-lines event log live_events.jsonl; every
    event is also printed, so a Claude session can stream it with the Monitor tool
  * serves READ-ONLY status on http://127.0.0.1:PORT  (/health /status /summary /alert). Localhost only.
  * a failed run backs off (exponential) and is retried; after --max-failures in a row it stops and writes ALERT.md
  * stop it with Ctrl-C / SIGTERM, a file named STOP in the state folder (one-shot: it is renamed to STOP.handled,
    so the next start is not blocked), or FD_KILL_SWITCH / FD_KILL_LIVE=1

It NEVER downloads data, accepts website terms, deletes anything, or changes the model. Refreshing the FracFocus
data needs a person (the terms); drop the new files in place and this service picks them up on its own.
"""
import argparse
import contextlib
import http.server
import importlib.util
import io
import json
import os
import signal
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rs = _load("run_suite_for_live", "run_suite.py")
wd = _load("watchdog_for_live", "watchdog.py")

MAX_EVENTS_BYTES = 1_000_000
WATCH_EXIT = {0: "OK", 1: "WARN", 2: "ALERT"}


def iso(ts):
    return datetime.fromtimestamp(ts).isoformat(timespec="seconds")


class Live:
    def __init__(self, permits, frac, out, state_dir=None, poll=10.0, debounce=30.0, publish=False, port=None,
                 run_on_start=True, skip_tests=False, counties=None, from_date="2016-01-01", lag_days=45,
                 backoff_base=60.0, backoff_max=3600.0, max_failures=5, clock=time.time, run_fn=None, quiet=False,
                 publish_author=None):
        self.permits, self.frac = Path(permits).expanduser(), Path(frac).expanduser()
        self.out = Path(out).expanduser()
        self.state_dir = Path(state_dir).expanduser() if state_dir else self.out
        self.poll, self.debounce, self.publish_flag, self.port = poll, debounce, publish, port
        self.run_on_start, self.skip_tests, self.counties = run_on_start, skip_tests, counties
        self.from_date, self.lag_days, self.publish_author = from_date, lag_days, publish_author
        self.backoff_base, self.backoff_max, self.max_failures = backoff_base, backoff_max, max_failures
        self.clock, self.run_fn, self.quiet = clock, run_fn or default_run, quiet
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.out.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = False
        self._stop_reason = None
        self._seen_fp = self._done_fp = self._pending_since = self._retry_at = None
        self._server = None
        self.http_port = None
        self.status = {"pid": os.getpid(), "state": "starting", "started_at": iso(clock()), "last_check": None,
                       "last_run_started": None, "last_run_finished": None, "last_result": None,
                       "consecutive_failures": 0, "next_retry_at": None, "publish": publish,
                       "inputs": [str(self.permits), str(self.frac)], "http_port": None}

    # ---- events and status -------------------------------------------------------------------------
    def emit(self, event, **fields):
        rec = {"t": iso(self.clock()), "event": event, **fields}
        line = json.dumps(rec)
        path = self.state_dir / "live_events.jsonl"
        if path.exists() and path.stat().st_size > MAX_EVENTS_BYTES:
            path.replace(self.state_dir / "live_events.jsonl.1")
        with open(path, "a") as h:
            h.write(line + "\n")
        if not self.quiet:
            print(line, flush=True)

    def _set(self, state):
        with self._lock:
            self.status["state"] = state

    def snapshot(self):
        with self._lock:
            return json.loads(json.dumps(self.status))

    def write_status(self):
        tmp = self.state_dir / "live_status.json.tmp"
        tmp.write_text(json.dumps(self.snapshot(), indent=1))
        os.replace(tmp, self.state_dir / "live_status.json")

    # ---- control -----------------------------------------------------------------------------------
    def request_stop(self, reason="requested"):
        self._stop, self._stop_reason = True, self._stop_reason or reason
        self._wake.set()

    def stop_requested(self):
        if self._stop:
            return self._stop_reason
        if any(os.environ.get(k) for k in ("FD_KILL_SWITCH", "FD_KILL_LIVE")):
            return "kill switch"
        if (self.state_dir / "STOP").exists():
            return "STOP file"
        return None

    def acquire_lock(self):
        path = self.state_dir / "live.lock"
        if path.exists():
            try:
                pid = int(path.read_text().strip())
            except ValueError:
                pid = None                                  # unreadable lock: treat as stale
            if pid is not None:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    pid = None                              # that process is gone: stale lock
                except PermissionError:
                    pass                                    # exists, owned by someone else: still alive
                if pid is not None:
                    raise SystemExit(f"Another live service (pid {pid}) is running; refusing to start a second.")
        path.write_text(str(os.getpid()))

    def release_lock(self):
        (self.state_dir / "live.lock").unlink(missing_ok=True)

    # ---- the loop ----------------------------------------------------------------------------------
    def fingerprint(self):
        try:
            return tuple((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in (self.permits, self.frac))
        except FileNotFoundError:
            return None

    def _finalize_stop(self, reason):
        stop_file = self.state_dir / "STOP"
        if stop_file.exists():                            # one-shot: consume it so the next start is not blocked
            stop_file.replace(self.state_dir / "STOP.handled")
        self._set("stopped_after_failures" if self._stop_reason == "too many failures" else "stopped")
        self.emit("stopped", reason=reason)
        self.write_status()
        return "stop"

    def step(self):
        """One pass of the loop. Returns "continue" or "stop"."""
        now = self.clock()
        self.status["last_check"] = iso(now)
        reason = self.stop_requested()
        if reason:
            return self._finalize_stop(reason)
        fp = self.fingerprint()
        if fp is None:
            if self.status["state"] != "waiting_for_inputs":
                self.emit("waiting_for_inputs", inputs=self.status["inputs"])
            self._set("waiting_for_inputs")
            self._seen_fp = self._pending_since = None
            self.write_status()
            return "continue"
        if fp != self._seen_fp:
            first = self._seen_fp is None and self._done_fp is None
            self._seen_fp, self._pending_since = fp, now
            if first and not self.run_on_start:
                self._done_fp = fp                       # baseline: the files already there are not "new"
                self.emit("baseline_recorded")
            else:
                self.emit("inputs_changed" if not first else "inputs_found")
        needs_run = fp != self._done_fp
        debounced = self._pending_since is not None and now - self._pending_since >= self.debounce
        due = self._retry_at is None or now >= self._retry_at
        if needs_run and debounced and due:
            self._run_cycle(fp)
        else:
            self._set("debouncing" if needs_run and not debounced else "backoff" if needs_run and not due else "idle")
        if self._stop:                                    # e.g. too many failures during this very cycle
            return self._finalize_stop(self._stop_reason)
        self.write_status()
        return "continue"

    def _run_cycle(self, fp):
        self._set("running")
        self.status["last_run_started"] = iso(self.clock())
        self.emit("run_started")
        self.write_status()
        try:
            result = self.run_fn(self)
        except Exception as exc:
            self._failed(exc)
        except SystemExit as exc:
            self._failed(RuntimeError(f"tool exited: {exc}"))
        else:
            self.status["consecutive_failures"] = 0
            self._retry_at = None
            self.status["next_retry_at"] = None
            self._done_fp = fp                            # if the files changed mid-run, the next step re-runs
            self.status["last_run_finished"] = iso(self.clock())
            self.status["last_result"] = result
            (self.out / "ALERT.md").unlink(missing_ok=True)   # our own "stopped after failures" note is now out of date
            self.emit("run_finished", **result)
            if str(result.get("publish", "")).startswith("failed"):
                self.emit("publish_failed", message=result["publish"][:300],
                          note="results are complete on disk; only the upload failed, so the run still counts")
            if result.get("watchdog_status") == "ALERT":
                self.emit("alert", message="watchdog ALERT: see ALERT.md; a person must review", folder=str(self.out / "watch"))
            self._set("idle")

    def _failed(self, exc):
        n = self.status["consecutive_failures"] + 1
        self.status["consecutive_failures"] = n
        delay = min(self.backoff_base * 2 ** (n - 1), self.backoff_max)
        self._retry_at = self.clock() + delay
        self.status["next_retry_at"] = iso(self._retry_at)
        self.status["last_run_finished"] = iso(self.clock())
        self.emit("run_failed", error=str(exc)[:500], failures=n, retry_in_seconds=delay)
        if n >= self.max_failures:
            (self.out / "ALERT.md").write_text(
                f"# Frac live service stopped\n\nThe evaluation failed {n} times in a row. Last error: {str(exc)[:500]}\n\n"
                "A person must look at it. Bots must not retune the model or change data to clear this.\n")
            self.emit("alert", message="stopped after repeated failures", failures=n)
            self.request_stop("too many failures")
        else:
            self._set("backoff")

    # ---- HTTP (read only, localhost only) ----------------------------------------------------------
    def start_http(self):
        live = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body, ctype="application/json"):
                data = body.encode() if isinstance(body, str) else body
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                path = self.path.split("?")[0]
                if path == "/health":
                    self._send(200, "ok", "text/plain")
                elif path == "/status":
                    self._send(200, json.dumps(live.snapshot(), indent=1))
                elif path == "/summary":
                    runs = sorted(p for p in (live.out / "runs").glob("*/SUMMARY.md")) if (live.out / "runs").is_dir() else []
                    self._send(200, runs[-1].read_text(), "text/markdown") if runs else self._send(404, "no summary yet", "text/plain")
                elif path == "/alert":
                    a = live.out / "watch" / "ALERT.md"
                    b = live.out / "ALERT.md"
                    src = a if a.exists() else b if b.exists() else None
                    self._send(200, src.read_text(), "text/markdown") if src else self._send(404, "no alert", "text/plain")
                else:
                    self._send(404, "not found", "text/plain")

            def _no(self):
                self._send(405, "read only", "text/plain")

            do_POST = do_PUT = do_DELETE = do_PATCH = _no

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", self.port or 0), Handler)
        self.http_port = self._server.server_address[1]
        self.status["http_port"] = self.http_port
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def serve_forever(self):
        self.acquire_lock()
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: self.request_stop("signal"))
        try:
            if self.port is not None:
                self.start_http()
            self.emit("started", pid=os.getpid(), http_port=self.http_port, publish=self.publish_flag)
            while self.step() == "continue":
                self._wake.wait(self.poll)
                self._wake.clear()
        finally:
            self.close()
            self.release_lock()
        return 3 if self.status["state"] == "stopped_after_failures" else 0


def default_run(live):
    """One evaluation cycle: run_suite, then the watchdog. Raises on failure."""
    run_dir = live.out / "runs" / datetime.now().strftime("%Y%m%d-%H%M%S")
    args = ["--permits", str(live.permits), "--frac", str(live.frac), "--out", str(run_dir),
            "--from-date", live.from_date, "--lag-days", str(live.lag_days)]
    if live.counties:
        args += ["--counties", live.counties]
    if live.skip_tests:
        args.append("--skip-tests")
    if live.publish_flag:
        args.append("--publish")
        if live.publish_author:
            args += ["--publish-author", live.publish_author]
    quiet = contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO())
    with quiet[0], quiet[1]:
        rc = rs.main(args)
    if rc != 0:
        raise RuntimeError(f"run_suite exited {rc}; see {run_dir / 'SUMMARY.md'}")
    with contextlib.redirect_stdout(io.StringIO()):
        wrc = wd.main(["--results", str(live.out / "runs"), "--out", str(live.out / "watch"),
                       "--inputs", str(live.permits), str(live.frac)])
    pub = rs.LAST_PUBLISH
    publish_state = "off" if not pub["attempted"] else "ok" if pub["ok"] else f"failed: {pub['message']}"
    return {"run": run_dir.name, "watchdog_exit": wrc, "watchdog_status": WATCH_EXIT[wrc], "publish": publish_state}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--permits", required=True)
    ap.add_argument("--frac", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--state-dir")
    ap.add_argument("--poll", type=float, default=10.0, help="seconds between checks of the input files")
    ap.add_argument("--debounce", type=float, default=30.0, help="inputs must be unchanged this long before a run")
    ap.add_argument("--port", type=int, help="serve read-only status on 127.0.0.1:PORT (0 = pick a free port)")
    ap.add_argument("--publish", action="store_true", help="push summary files to claude/frac-results after each run")
    ap.add_argument("--no-run-on-start", action="store_true", help="treat the files already there as the baseline")
    ap.add_argument("--skip-tests", action="store_true")
    ap.add_argument("--counties")
    ap.add_argument("--from-date", default="2016-01-01")
    ap.add_argument("--lag-days", type=int, default=45)
    ap.add_argument("--max-failures", type=int, default=5)
    ap.add_argument("--publish-author", help='author for results commits, e.g. "Scott <you@example.com>"')
    args = ap.parse_args(argv)
    live = Live(args.permits, args.frac, args.out, args.state_dir, args.poll, args.debounce, args.publish, args.port,
                not args.no_run_on_start, args.skip_tests, args.counties, args.from_date, args.lag_days,
                max_failures=args.max_failures, publish_author=args.publish_author)
    return live.serve_forever()


if __name__ == "__main__":
    sys.exit(main())
