#!/usr/bin/env python3
"""The frac desk: a live terminal board for the frac-demand lane. Read-only; it never changes anything.

  python3 scripts/frac/desk.py --dir ~/frac-data/work/live              live board, refreshes every 2 s (Ctrl-C quits)
  python3 scripts/frac/desk.py --dir ~/frac-data/work/live --once       one frame, exit code = health (for scripts/bots)
  python3 scripts/frac/desk.py --dir ~/frac-data/work/live --recap 24   what happened in the last 24 hours

Panels: STATUS (is the live service alive, what it is doing), RISK (the watchdog), BOARD (per county: how much better
the 13-week forecast is than a trailing average, in water and sand, plus at turning points), TAPE (the event blotter).
Exit codes with --once: 0 OK, 1 WARN, 2 ALERT, 3 FEED DOWN (service stopped or silent).

This is a backtest-monitoring desk. Counties are not positions and nothing here is a live forecast or advice.
"""
import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

STALE_SECONDS = 120
EDGE_FLAT = 0.10      # within +-10% of the baseline counts as flat
CODES = {"green": "\x1b[32m", "red": "\x1b[31m", "yellow": "\x1b[33m", "cyan": "\x1b[36m", "dim": "\x1b[2m",
         "bold": "\x1b[1m", "inv": "\x1b[7m"}
RESET = "\x1b[0m"


def paint(text, name, color):
    return f"{CODES[name]}{text}{RESET}" if color else text


def age(seconds):
    s = max(0, int(seconds))
    if s < 90:
        return f"{s}s"
    if s < 5400:
        return f"{s // 60}m"
    if s < 172800:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def parse_iso(text):
    try:
        return datetime.fromisoformat(text).timestamp()
    except (TypeError, ValueError):
        return None


def read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def load_state(directory, events=200):
    d = Path(directory).expanduser()
    runs = sorted(p for p in (d / "runs").glob("*") if p.is_dir()) if (d / "runs").is_dir() else []
    latest = runs[-1] if runs else None
    evs = []
    log = d / "live_events.jsonl"
    if log.exists():
        for line in log.read_text().splitlines()[-events:]:
            try:
                evs.append(json.loads(line))
            except ValueError:
                pass
    return {"status": read_json(d / "live_status.json"), "events": evs, "run": latest.name if latest else None,
            "water": read_json(latest / "water_no_delay.json") if latest else None,
            "sand": read_json(latest / "sand_no_delay.json") if latest else None,
            "water_delay": read_json(latest / "water_with_delay.json") if latest else None,
            "watch": read_json(d / "watch" / "status.json")}


def edge(model, base):
    """Positive = the model's error is smaller than the baseline's, as a share of the baseline's."""
    if model is None or base is None or base <= 0:
        return None
    return (base - model) / base


def board_rows(result):
    rows, skipped = [], []
    for county, r in (result or {}).items():
        v2 = r.get("v2")
        if not v2:
            skipped.append(county)
            continue
        tp = v2.get("turning_point_windows") or {}
        total = edge(v2.get("median_abs_13wk_total_error"), v2.get("baseline_median_abs_13wk_total_error"))
        turn = edge(tp.get("model_median_abs_error"), tp.get("baseline_median_abs_error"))
        if total is None:
            signal = "·"
        elif total < -EDGE_FLAT or (turn is not None and turn < -EDGE_FLAT and total < EDGE_FLAT):
            signal = "▼"
        elif total > EDGE_FLAT and (turn is None or turn > 0):
            signal = "▲"
        else:
            signal = "●"
        rows.append({"county": county, "match": r.get("match_rate"), "model": v2.get("median_abs_13wk_total_error"),
                     "base": v2.get("baseline_median_abs_13wk_total_error"), "edge": total, "turn": turn,
                     "bias": v2.get("median_13wk_total_bias"), "signal": signal})
    rows.sort(key=lambda x: (x["edge"] is None, -(x["edge"] or 0)))
    return rows, skipped


def pct(x, signed=False):
    if x is None:
        return "   -"
    return f"{x * 100:+4.0f}%" if signed else f"{x * 100:4.0f}%"


def num(x):
    return "  -  " if x is None else f"{x:5.3f}"


def board(title, result, color):
    rows, skipped = board_rows(result)
    lines = [paint(f"── BOARD · {title} ", "bold", color) + "─" * 30,
             paint("COUNTY      MATCH  MODEL   BASE    EDGE   TURN-EDGE  BIAS   SIG", "dim", color)]
    if not rows:
        lines.append(paint("  waiting for the first completed run", "dim", color))
    for r in rows:
        tone = {"▲": "green", "▼": "red", "●": "yellow", "·": "dim"}[r["signal"]]
        edge_txt = paint(f"{pct(r['edge'], True):>6}", tone, color)         # pad first, then colour
        lines.append(f"{r['county'][:10]:<10}  {pct(r['match']):>5}  {num(r['model'])}  {num(r['base'])}  "
                     f"{edge_txt}  {pct(r['turn'], True):>9}  {pct(r['bias'], True):>5}   {paint(r['signal'], tone, color)}")
    if skipped:
        lines.append(paint(f"  no result (too little data or no matches): {', '.join(skipped)}", "dim", color))
    return lines


def status_bar(state, now, color):
    st = state["status"]
    if not st:
        return paint(" FEED DOWN · no live_status.json: is the live service running? ", "inv", color), "down"
    last = parse_iso(st.get("last_check"))
    silent = None if last is None else now - last
    s = st.get("state", "?")
    if s.startswith("stopped"):
        return paint(f" STOPPED · {s} ", "inv", color) + f"  last check {age(silent) if silent is not None else '?'} ago", "down"
    if silent is not None and silent > STALE_SECONDS:
        return paint(f" FEED DOWN · no heartbeat for {age(silent)} ", "inv", color), "down"
    if silent is not None and silent < -STALE_SECONDS:
        return paint(f" FEED DOWN · heartbeat is {age(-silent)} in the future: clock mismatch ", "inv", color), "down"
    dot = paint("● LIVE", "green", color)
    started = parse_iso(st.get("started_at"))
    finished = parse_iso(st.get("last_run_finished"))
    res = st.get("last_result") or {}
    bits = [f"{dot}  {s.upper()}",
            f"up {age(now - started)}" if started else "",
            f"last run {age(now - finished)} ago ({res.get('watchdog_status', '?')})" if finished else "no run finished yet",
            f"failures {st.get('consecutive_failures', 0)}",
            f"publish {'ON' if st.get('publish') else 'off'}",
            f"port {st.get('http_port')}" if st.get("http_port") else ""]
    return "  ·  ".join(b for b in bits if b), "up"


def risk_panel(state, color):
    w = state["watch"]
    if not w:
        return [paint("── RISK · watchdog has not reported yet", "dim", color)], "UNKNOWN"
    status = w.get("status", "?")
    tone = {"OK": "green", "WARN": "yellow", "ALERT": "red"}.get(status, "dim")
    lines = [paint("── RISK ", "bold", color) + paint(f" {status} ", "inv" if status != "OK" else "green", color)
             + paint(f"  run {w.get('run')}", "dim", color)]
    for c in w.get("checks", []):
        if c.get("level") in ("ALERT", "WARN"):
            lines.append(f"  {paint(c['level'], 'red' if c['level'] == 'ALERT' else 'yellow', color)} {c['message']}")
    return lines, status


def tape(events, n, color):
    lines = [paint("── TAPE ", "bold", color) + "─" * 40]
    for e in reversed(events[-n:]):
        t = (e.get("t") or "")[11:19]
        kind = e.get("event", "?")
        tone = "red" if kind in ("run_failed", "alert", "stopped") else "green" if kind == "run_finished" else "dim"
        detail = " ".join(f"{k}={v}" for k, v in e.items() if k not in ("t", "event"))[:70]
        lines.append(f"{paint(t, 'dim', color)} {paint(f'{kind:<18}', tone, color)} {detail}")
    if len(lines) == 1:
        lines.append(paint("  quiet", "dim", color))
    return lines


def render(state, now=None, color=True, event_rows=12, width=110):
    now = now or time.time()
    bar, health = status_bar(state, now, color)
    risk, risk_status = risk_panel(state, color)
    out = [paint(" AMARA FRAC DESK ", "inv", color) + f"  {datetime.fromtimestamp(now):%Y-%m-%d %H:%M:%S}", bar, ""]
    out += risk + [""]
    run = state["run"] or "-"
    out += board(f"WATER, 13-week, run {run}", state["water"], color) + [""]
    out += board("SAND (recorded pounds), 13-week", state["sand"], color) + [""]
    out += tape(state["events"], event_rows, color)
    out.append(paint("EDGE = how much smaller the model's error is than a trailing average's.   ▲ clearly better  ● flat  ▼ worse", "dim", color))
    out.append(paint("Backtest monitoring only: not a live forecast, not advice.", "dim", color))
    text = "\n".join(out)
    if not color:
        text = "\n".join(line[:width] for line in text.splitlines())
    return text, health, risk_status


def exit_code(health, risk_status):
    if health == "down":
        return 3
    return {"ALERT": 2, "WARN": 1}.get(risk_status, 0)


def recap(events, now, hours):
    cutoff = now - hours * 3600
    window = [e for e in events if (parse_iso(e.get("t")) or 0) >= cutoff]
    count = lambda k: sum(1 for e in window if e.get("event") == k)
    alerts = [e for e in window if e.get("event") == "alert"]
    return {"window_hours": hours, "events": len(window), "runs_finished": count("run_finished"),
            "runs_failed": count("run_failed"), "alerts": len(alerts), "stops": count("stopped"),
            "last_alert": alerts[-1].get("message") if alerts else None}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", required=True, help="the live service's --out folder")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--refresh", type=float, default=2.0)
    ap.add_argument("--no-color", action="store_true")
    ap.add_argument("--recap", type=float, metavar="HOURS")
    ap.add_argument("--rows", type=int, default=12, help="tape rows")
    args = ap.parse_args(argv)
    color = (not args.no_color) and sys.stdout.isatty()
    state = load_state(args.dir)
    if args.recap:
        print(json.dumps(recap(load_state(args.dir, events=100000)["events"], time.time(), args.recap), indent=1))
        return 0
    if args.once:
        text, health, risk = render(state, color=color, event_rows=args.rows)
        print(text)
        return exit_code(health, risk)
    try:
        while True:
            text, health, risk = render(load_state(args.dir), color=color, event_rows=args.rows)
            sys.stdout.write("\x1b[2J\x1b[H" + text + "\n")
            sys.stdout.flush()
            time.sleep(args.refresh)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
