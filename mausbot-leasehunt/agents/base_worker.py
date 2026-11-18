"""Shared plumbing for every seat: settings, G4 evidence validator, hard gates, kill switch,
Slack transports (Bolt + in-memory for tests), rate limiter, handoff envelopes, BaseWorker."""
from __future__ import annotations

import json
import logging
import os
import re
import socket
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol
from urllib.parse import urlparse

from jsonschema import Draft7Validator

from db.lock_manager import LockManager
from db.store import Store, new_id, now_iso

log = logging.getLogger("leasehunt")
ROOT = Path(__file__).resolve().parent.parent

AGENTS = ["mausbot-cos", "docket-bot", "courthouse-bot", "abstractor-bot", "trace-bot", "cfo-bot"]
CHANNELS = {
    "dispatch": "dispatch-ops", "docket": "docket-feed", "title_exp": "title-expiration",
    "abstract": "abstract-title", "owner": "owner-intel", "cfo": "underwriting-cfo", "exec": "exec-briefings",
}
ALL_CHANNELS = list(CHANNELS.values())
GRADE_RANK = {"A": 4, "B": 3, "C": 2, "D": 1}


# ---------------------------------------------------------------------------- settings
def load_dotenv(path: str | os.PathLike = ".env") -> None:
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _f(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


@dataclass
class Settings:
    db_url: str = "sqlite:///leasehunt.db"
    min_spread_threshold: float = 50.0
    target_spread: float = 500.0
    wti_floor: float = 55.0
    henry_hub_floor: float = 2.0
    permit_drop_pct: float = 20.0
    tier1_score: int = 80
    single_tract_first: bool = True
    max_revisions: int = 2
    lock_ttl_seconds: int = 900
    slack_msgs_per_second: float = 1.0
    principal_user_ids: tuple[str, ...] = ()
    secondary_domains: tuple[str, ...] = ("zillow.com", "landwatch.com", "landsearch.com", "mineralanswers.com",
                                          "facebook.com", "reddit.com", "wikipedia.org")
    skip_trace_providers: tuple[str, ...] = ("tracers", "batchskiptracing")
    default_imaging_start_year: int = 1990
    llm_model: str = "claude-opus-5-5"
    anthropic_api_key: str | None = None
    eia_api_key: str | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        csv = lambda n, d: tuple(x.strip() for x in os.environ.get(n, ",".join(d)).split(",") if x.strip())
        return cls(
            db_url=os.environ.get("DATABASE_URL", cls.db_url),
            min_spread_threshold=_f("MIN_SPREAD_PER_NMA", 50), target_spread=_f("TARGET_SPREAD_PER_NMA", 500),
            wti_floor=_f("WTI_FLOOR_USD", 55), henry_hub_floor=_f("HENRY_HUB_FLOOR_USD", 2.0),
            permit_drop_pct=_f("PERMIT_DROP_PCT", 20), tier1_score=int(_f("TIER1_SCORE", 80)),
            single_tract_first=os.environ.get("SINGLE_TRACT_FIRST", "true").lower() == "true",
            max_revisions=int(_f("MAX_REVISIONS", 2)), lock_ttl_seconds=int(_f("LOCK_TTL_SECONDS", 900)),
            slack_msgs_per_second=_f("SLACK_MSGS_PER_SECOND", 1.0),
            principal_user_ids=csv("PRINCIPAL_USER_IDS", ()),
            secondary_domains=csv("SECONDARY_DOMAINS", cls.secondary_domains),
            skip_trace_providers=csv("SKIP_TRACE_PROVIDERS", cls.skip_trace_providers),
            default_imaging_start_year=int(_f("DEFAULT_IMAGING_START_YEAR", 1990)),
            llm_model=os.environ.get("LLM_MODEL", cls.llm_model),
            anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY"), eia_api_key=os.environ.get("EIA_API_KEY"),
        )


# ---------------------------------------------------------------------------- G4 Evidence-or-Null
def _present(v: Any) -> bool:
    return v is not None and v != "" and v != []


def evidence_problems(ev: Any, secondary_domains: Iterable[str] = ()) -> list[str]:
    """Return reasons `ev` is not an acceptable citation (empty list == OK)."""
    if not isinstance(ev, dict):
        return ["missing evidence"]
    problems = []
    if ev.get("grade") not in GRADE_RANK:
        problems.append("evidence grade must be A/B/C/D")
    url, bp, dn = ev.get("source_url"), ev.get("book_page"), ev.get("docket_no")
    cited = False
    if url:
        host = (urlparse(url).hostname or "").lower()
        if urlparse(url).scheme not in ("http", "https") or not host:
            problems.append(f"source_url is not an http(s) URL: {url!r}")
        else:
            cited = True
            if any(host == d or host.endswith("." + d) for d in secondary_domains) and ev.get("grade") in ("A", "B"):
                problems.append(f"{host} is a secondary source and cannot be grade A/B")
    if bp and re.search(r"\d", str(bp)):
        cited = True
    elif bp:
        problems.append("book_page must contain a Book/Page number")
    if dn and str(dn).strip():
        cited = True
    if not cited:
        problems.append("evidence needs a primary record URL, Book/Page, or Docket #")
    return problems


def validate_claims(claims: Iterable[dict], secondary_domains: Iterable[str] = ()) -> list[str]:
    """G4: every non-null claim value must carry acceptable evidence. Returns violation strings."""
    out = []
    for c in claims:
        if _present(c.get("value")):
            probs = evidence_problems(c.get("evidence"), secondary_domains)
            if probs:
                out.append(f"{c.get('field')}: {'; '.join(probs)}")
    return out


def sanitize_claims(claims: list[dict], secondary_domains: Iterable[str] = ()) -> tuple[list[dict], list[str]]:
    """Enforce Evidence-or-Null: null out uncited values. Returns (clean_claims, nulled_fields)."""
    clean, nulled = [], []
    for c in claims:
        c = dict(c)
        if _present(c.get("value")) and evidence_problems(c.get("evidence"), secondary_domains):
            c["value"], c["evidence"] = None, None
            nulled.append(c["field"])
        clean.append(c)
    return clean, nulled


def min_grade(grades: Iterable[str | None]) -> str | None:
    gs = [g for g in grades]
    if not gs or any(g not in GRADE_RANK for g in gs):
        return None if not gs else "D"
    return min(gs, key=lambda g: GRADE_RANK[g])


def grades_ok(grades: Iterable[str | None]) -> bool:
    gs = list(grades)
    return bool(gs) and all(g in ("A", "B") for g in gs)


# ---------------------------------------------------------------------------- kill switch & hard gates
def evaluate_kill_switch(wti: float | None, henry_hub: float | None, permits_now: int | None,
                         permits_base: int | None, s: Settings) -> tuple[bool, list[str]]:
    """Cyclicality kill-switch. Fails CLOSED (switch ON) if any input is missing."""
    reasons = []
    if None in (wti, henry_hub, permits_now, permits_base) or not permits_base:
        return True, ["MISSING_MARKET_DATA"]
    if wti < s.wti_floor:
        reasons.append(f"WTI ${wti:.2f} < floor ${s.wti_floor:.2f}")
    if henry_hub < s.henry_hub_floor:
        reasons.append(f"Henry Hub ${henry_hub:.2f} < floor ${s.henry_hub_floor:.2f}")
    drop = (permits_base - permits_now) / permits_base * 100
    if drop > s.permit_drop_pct:
        reasons.append(f"permit count down {drop:.1f}% vs baseline (limit {s.permit_drop_pct:.0f}%)")
    return bool(reasons), reasons


@dataclass
class GateResult:
    passed: bool
    gates: dict[str, bool]
    failures: list[str]


def evaluate_hard_gates(*, abstractor_status: str, curative_cost_priced: bool, curative_cost_usd: float,
                        comp_grades: Iterable[str | None], title_grades: Iterable[str | None],
                        net_spread_per_nma: float | None, min_spread: float,
                        kill_switch: bool) -> GateResult:
    """The four programmatic gates a tract must clear before #exec-briefings. Used by both
    CFOBot (before it reports) and MausBot (independently, before it escalates and again on approval)."""
    fails: list[str] = []
    clean = abstractor_status == "TITLE_VERIFIED_CLEAN"
    priced = (abstractor_status == "TITLE_DEFECTS_FOUND" and bool(curative_cost_priced) and curative_cost_usd > 0)
    g_title = clean or priced
    if not g_title:
        fails.append(f"G1 title: status {abstractor_status} and curative cost not priced into spread")
    g_grade = grades_ok(list(comp_grades)) and grades_ok(list(title_grades))
    if not g_grade:
        fails.append("G2 evidence: every comp and title record must be grade A or B (and at least one of each)")
    g_spread = net_spread_per_nma is not None and net_spread_per_nma >= min_spread
    if not g_spread:
        fails.append(f"G3 spread: net ${net_spread_per_nma}/NMA < floor ${min_spread}/NMA")
    g_kill = kill_switch is False
    if not g_kill:
        fails.append("G4 cyclicality kill-switch is ON")
    return GateResult(not fails, {"title": g_title, "evidence_grade": g_grade, "spread": g_spread,
                                  "kill_switch_clear": g_kill}, fails)


# ---------------------------------------------------------------------------- envelopes
_SCHEMA = Draft7Validator(json.loads((ROOT / "schemas" / "slack_handoff.schema.json").read_text()))
_FENCE = re.compile(r"```json\s*(\{.*?\})\s*```", re.S)


def envelope_errors(env: dict) -> list[str]:
    return [f"{'/'.join(map(str, e.path))}: {e.message}" for e in _SCHEMA.iter_errors(env)]


def render_envelope(env: dict, summary: str) -> str:
    return f"{summary}\n```json\n{json.dumps(env, indent=1, sort_keys=True, default=str)}\n```"


def parse_envelope(text_: str) -> dict | None:
    m = _FENCE.search(text_ or "")
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except ValueError:
        return None


def make_work_order(frm: str, to: str, tract_id: str, task_type: str, payload: dict, revision_of: str | None = None) -> dict:
    return {"type": "WORK_ORDER", "id": new_id("wo_"), "tract_id": tract_id, "task_type": task_type,
            "from_agent": frm, "to_agent": to, "payload": payload, "revision_of": revision_of, "created_at": now_iso()}


# ---------------------------------------------------------------------------- rate limiter
class RateLimiter:
    """Token bucket (default ~1 message/sec, matching Slack's per-channel posting guidance)."""

    def __init__(self, rate_per_sec: float = 1.0, burst: int = 3,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        self.rate, self.burst, self.clock, self.sleep = rate_per_sec, burst, clock, sleep
        self.tokens, self.last = float(burst), clock()

    def acquire(self) -> float:
        """Block until a token is available; return seconds waited."""
        waited = 0.0
        while True:
            now = self.clock()
            self.tokens = min(self.burst, self.tokens + (now - self.last) * self.rate)
            self.last = now
            if self.tokens >= 1:
                self.tokens -= 1
                return waited
            need = (1 - self.tokens) / self.rate
            self.sleep(need)
            waited += need


# ---------------------------------------------------------------------------- Slack transports
class Transport(Protocol):
    def post(self, channel: str, text: str, blocks: list | None = None) -> str: ...
    def update(self, channel: str, ts: str, text: str, blocks: list | None = None) -> None: ...
    def subscribe(self, handler: Callable[[str, str, dict], None]) -> None: ...


@dataclass
class Message:
    channel: str
    ts: str
    text: str
    blocks: list | None = None


class InMemoryTransport:
    """Deterministic stand-in for Slack used by tests/local simulation. Delivery is queued and
    pumped by `drain()`, mirroring Slack's asynchronous event delivery to other bots."""

    def __init__(self):
        self.messages: list[Message] = []
        self.handlers: list[Callable[[str, str, dict], None]] = []
        self._queue: deque[Message] = deque()
        self._n = 0

    def post(self, channel, text, blocks=None):
        self._n += 1
        m = Message(channel, f"{1700000000 + self._n}.{self._n:06d}", text, blocks)
        self.messages.append(m)
        self._queue.append(m)
        return m.ts

    def update(self, channel, ts, text, blocks=None):
        for m in self.messages:
            if m.channel == channel and m.ts == ts:
                m.text, m.blocks = text, blocks

    def subscribe(self, handler):
        self.handlers.append(handler)

    def drain(self, max_steps: int = 500) -> int:
        n = 0
        while self._queue and n < max_steps:
            m = self._queue.popleft()
            for h in list(self.handlers):
                h(m.channel, m.text, {"ts": m.ts})
            n += 1
        return n

    def in_channel(self, channel: str) -> list[Message]:
        return [m for m in self.messages if m.channel == channel]


class BoltTransport:
    """Production transport over Slack Bolt (Socket Mode). One instance per seat/machine."""

    def __init__(self, bot_token: str, app_token: str | None = None):
        from slack_bolt import App  # imported lazily so tests need no Slack packages
        self.app = App(token=bot_token)
        self.app_token = app_token
        self._ids: dict[str, str] = {}
        self._names: dict[str, str] = {}
        self._handlers: list[Callable[[str, str, dict], None]] = []
        self._resolve_channels()

        @self.app.event("message")
        def _on_message(event, logger):  # noqa: ANN001
            # other bots' messages arrive as subtype bot_message; our own are filtered by Bolt
            ch = self._names.get(event.get("channel"), event.get("channel"))
            for h in self._handlers:
                try:
                    h(ch, event.get("text", ""), event)
                except Exception:  # keep the listener alive
                    logger.exception("handler failed")

    def _resolve_channels(self) -> None:
        cursor = None
        while True:
            r = self.app.client.conversations_list(types="public_channel,private_channel", limit=200, cursor=cursor)
            for c in r["channels"]:
                self._ids[c["name"]], self._names[c["id"]] = c["id"], c["name"]
                if c["name"] in ALL_CHANNELS and not c.get("is_member"):
                    try:
                        self.app.client.conversations_join(channel=c["id"])
                    except Exception:  # private channels need an invite
                        log.warning("bot is not a member of #%s; invite it", c["name"])
            cursor = (r.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break

    def post(self, channel, text, blocks=None):
        r = self.app.client.chat_postMessage(channel=self._ids.get(channel, channel), text=text[:39000], blocks=blocks)
        return r["ts"]

    def update(self, channel, ts, text, blocks=None):
        self.app.client.chat_update(channel=self._ids.get(channel, channel), ts=ts, text=text, blocks=blocks or [])

    def subscribe(self, handler):
        self._handlers.append(handler)

    def run(self) -> None:
        from slack_bolt.adapter.socket_mode import SocketModeHandler
        SocketModeHandler(self.app, self.app_token).start()


# ---------------------------------------------------------------------------- optional LLM assistant
class LLMAssistant:
    """Answers human @mentions using the seat's system prompt (prompts/NN_*.md) + DB context.
    Deterministic engines do the actual work; the LLM only handles free-text conversation."""

    def __init__(self, prompt_file: str, settings: Settings):
        self.system = (ROOT / "prompts" / prompt_file).read_text()
        self.settings = settings

    def ask(self, question: str, context: str = "") -> str:
        if not self.settings.anthropic_api_key:
            return "LLM is not configured (set ANTHROPIC_API_KEY); use /leasehunt commands for state."
        import anthropic
        client = anthropic.Anthropic(api_key=self.settings.anthropic_api_key)
        try:
            resp = client.beta.messages.create(
                model=self.settings.llm_model, max_tokens=2000,
                betas=["server-side-fallback-2026-07-01"], fallbacks="default",
                system=self.system + "\n\n## Current state (database)\n" + context,
                messages=[{"role": "user", "content": question}],
            )
        except anthropic.APIError as e:  # network/auth/rate limit
            return f"LLM error: {e.__class__.__name__}"
        if resp.stop_reason == "refusal":
            return "I can't help with that request."
        return "".join(b.text for b in resp.content if b.type == "text") or "(no answer)"


# ---------------------------------------------------------------------------- BaseWorker
@dataclass
class WorkResult:
    status: str = "COMPLETE"            # COMPLETE | PARTIAL | BLOCKED | FAILED
    payload: dict[str, Any] = field(default_factory=dict)
    claims: list[dict[str, Any]] = field(default_factory=list)
    outcome: str | None = None          # worker-specific verdict, e.g. TITLE_VERIFIED_CLEAN


class BaseWorker:
    agent_id: str = ""
    channel: str = ""
    prompt_file: str = ""
    tasks: tuple[str, ...] = ()

    def __init__(self, transport: Transport, store: Store, settings: Settings | None = None,
                 locks: LockManager | None = None, limiter: RateLimiter | None = None):
        self.transport, self.store = transport, store
        self.settings = settings or Settings.from_env()
        self.locks = locks or LockManager(store, self.settings.lock_ttl_seconds)
        self.limiter = limiter or RateLimiter(self.settings.slack_msgs_per_second)
        self.owner = LockManager.owner_id(self.agent_id)
        transport.subscribe(self.on_message)

    # -- inbound
    def on_message(self, channel: str, text_: str, event: dict) -> None:
        env = parse_envelope(text_)
        if not env or env.get("to_agent") != self.agent_id:
            return
        errs = envelope_errors(env)
        if errs:
            self.store.audit(self.agent_id, "INVALID_ENVELOPE", env.get("tract_id"), errors=errs)
            return
        if env["type"] == "WORK_ORDER" and env["task_type"] in self.tasks:
            self.process_work_order(env)
        elif env["type"] == "REVISION_REQUEST":
            self.process_revision(env)

    def process_revision(self, req: dict) -> None:
        orig = self.store.get("work_orders", "work_order_id", req["work_order_id"])
        if not orig:
            return
        payload = dict(orig["payload"], revision_reasons=req["reasons"], revision_attempt=req["attempt"])
        wo = make_work_order("mausbot-cos", self.agent_id, orig["tract_id"], orig["task_type"], payload,
                             revision_of=orig["work_order_id"])
        self.store.insert("work_orders", work_order_id=wo["id"], tract_id=wo["tract_id"], task_type=wo["task_type"],
                          assigned_to=self.agent_id, status="OPEN", payload=payload, parent_id=orig["work_order_id"],
                          revision_count=req["attempt"], created_at=now_iso(), updated_at=now_iso())
        self.process_work_order(wo)

    # -- core loop
    def process_work_order(self, wo: dict) -> None:
        tract_id, task = wo["tract_id"], wo["task_type"]
        row = self.store.get("work_orders", "work_order_id", wo["id"])
        if row and row["status"] not in ("OPEN", "CLAIMED"):   # stale re-post / duplicate delivery
            self.store.audit(self.agent_id, "STALE_WORK_ORDER_IGNORED", tract_id, task=task, status=row["status"])
            return
        if not self.locks.acquire(tract_id, task, self.owner):
            self.store.audit(self.agent_id, "LOCK_CONTESTED", tract_id, task=task,
                             holder=self.locks.holder(tract_id, task))
            return
        try:
            if not self.store.get("work_orders", "work_order_id", wo["id"]):
                self.store.insert("work_orders", work_order_id=wo["id"], tract_id=tract_id, task_type=task,
                                  assigned_to=self.agent_id, status="CLAIMED", payload=wo["payload"],
                                  created_at=now_iso(), updated_at=now_iso())
            self.store.update("work_orders", "work_order_id", wo["id"], status="CLAIMED", updated_at=now_iso())
            try:
                result = self.execute(wo)
            except Exception as e:  # never die silently; report to COS
                log.exception("work order %s failed", wo["id"])
                result = WorkResult("FAILED", {"error": f"{type(e).__name__}: {e}"})
            claims, nulled = sanitize_claims(result.claims, self.settings.secondary_domains)
            status = result.status
            if nulled and status == "COMPLETE":
                status = "PARTIAL"
            self.store.update("work_orders", "work_order_id", wo["id"],
                              status="COMPLETE" if status != "FAILED" else "FAILED", updated_at=now_iso())
            self.store.audit(self.agent_id, "RECEIPT", tract_id, task=task, status=status, g4_nulled=nulled)
        finally:
            self.locks.release(tract_id, task, self.owner)
        receipt = {"type": "WORKER_RECEIPT", "id": new_id("rc_"), "work_order_id": wo["id"], "tract_id": tract_id,
                   "task_type": task, "from_agent": self.agent_id, "to_agent": "mausbot-cos", "status": status,
                   "outcome": result.outcome, "payload": result.payload, "claims": claims, "g4_nulled": nulled,
                   "created_at": now_iso()}
        self.post_envelope(self.channel, receipt, f"[{self.agent_id}] {task} {tract_id} → {status}"
                           + (f" ({result.outcome})" if result.outcome else ""))

    def post_envelope(self, channel: str, env: dict, summary: str, blocks: list | None = None) -> str:
        errs = envelope_errors(env)
        if errs:
            raise ValueError(f"refusing to post invalid envelope: {errs}")
        self.limiter.acquire()
        return self.transport.post(channel, render_envelope(env, summary), blocks)

    def execute(self, wo: dict) -> WorkResult:  # pragma: no cover - abstract
        raise NotImplementedError

    # -- helpers for subclasses
    def claim(self, field_: str, value: Any, evidence: dict | None) -> dict:
        return {"field": field_, "value": value, "evidence": evidence}


# ---------------------------------------------------------------------------- production runner
def run_agent(worker_cls: type[BaseWorker]) -> None:
    """`python -m agents.<seat>_worker` entry point: Socket Mode, one process per machine."""
    load_dotenv()
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    settings = Settings.from_env()
    store = Store(settings.db_url)
    store.init_schema()
    transport = BoltTransport(os.environ["SLACK_BOT_TOKEN"], os.environ["SLACK_APP_TOKEN"])
    worker = worker_cls(transport, store, settings)
    assistant = LLMAssistant(worker.prompt_file, settings)

    @transport.app.event("app_mention")
    def _mention(event, say):  # noqa: ANN001
        ctx = json.dumps(store.select("tracts", limit=20), default=str)[:6000]
        say(text=assistant.ask(event.get("text", ""), ctx), thread_ts=event.get("ts"))

    log.info("%s online on %s", worker.agent_id, socket.gethostname())
    transport.run()
