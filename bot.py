"""
Vera bot — HTTP service for the magicpin AI Challenge.

Endpoints (all JSON):
    GET  /v1/healthz     liveness + loaded-context counts
    GET  /v1/metadata    bot identity
    POST /v1/context     idempotent, versioned context push
    POST /v1/tick        proactive sends (restraint + dedup built in)
    POST /v1/reply       multi-turn handling (auto-reply / intent / hostile / off-topic)
    POST /v1/teardown    wipe all state

Run:  uvicorn bot:app --host 0.0.0.0 --port 8080

`compose(category, merchant, trigger, customer)` is re-exported here to satisfy
the submission contract (bot.py must expose compose()).
"""
from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from composer import compose as _compose, customer_consented, parse_dt
from conversation_handlers import respond
import llm

__all__ = ["app", "compose"]

VERSION = "1.0.0"
START = time.time()
VALID_SCOPES = ("category", "merchant", "customer", "trigger")
MAX_ACTIONS_PER_TICK = 20

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("vera")


def compose(category: dict, merchant: dict, trigger: dict, customer: Optional[dict] = None) -> dict:
    """Submission contract: returns body, cta, send_as, suppression_key, rationale."""
    out = _compose(category, merchant, trigger, customer)
    return {k: out[k] for k in ("body", "cta", "send_as", "suppression_key", "rationale")}


# --------------------------------------------------------------------------- #
# State (in-memory, thread-safe)
# --------------------------------------------------------------------------- #
class Store:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.reset()

    def reset(self) -> None:
        with getattr(self, "lock", threading.RLock()):
            self.contexts: Dict[tuple, Dict[str, Any]] = {}
            self.conversations: Dict[str, Dict[str, Any]] = {}
            self.merchant_memory: Dict[str, Dict[str, Any]] = {}
            self.sent_suppression: Dict[str, str] = {}   # suppression_key -> conversation_id
            self.conv_seq = 0
            self.conv_locks: Dict[str, threading.Lock] = {}

    def get(self, scope: str, cid: Optional[str]) -> Optional[dict]:
        if not cid:
            return None
        rec = self.contexts.get((scope, cid))
        return rec["payload"] if rec else None

    def memory(self, merchant_id: Optional[str]) -> Dict[str, Any]:
        return self.merchant_memory.setdefault(merchant_id or "_unknown", {})

    def counts(self) -> Dict[str, int]:
        c = {s: 0 for s in VALID_SCOPES}
        for (scope, _) in self.contexts:
            c[scope] = c.get(scope, 0) + 1
        return c


store = Store()


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# --------------------------------------------------------------------------- #
# App + error handling (every error is structured JSON, never a stack trace)
# --------------------------------------------------------------------------- #
app = FastAPI(title="Vera — magicpin merchant assistant", version=VERSION,
              docs_url="/docs", redoc_url=None)

ENDPOINTS = {
    "GET /v1/healthz": "Liveness probe + loaded context counts",
    "GET /v1/metadata": "Bot identity",
    "POST /v1/context": "Push a versioned context (category|merchant|customer|trigger)",
    "POST /v1/tick": "Periodic wake-up; returns proactive actions",
    "POST /v1/reply": "Deliver a merchant/customer reply; returns send|wait|end",
    "POST /v1/teardown": "Wipe all stored state",
}


@app.middleware("http")
async def timing_and_request_id(request: Request, call_next):
    rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
    t0 = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:  # pragma: no cover - last line of defence
        log.exception("unhandled error rid=%s path=%s", rid, request.url.path)
        response = JSONResponse(status_code=500, content={
            "error": "internal_error", "message": "Something went wrong on our side; the request was not processed.",
            "request_id": rid})
    ms = (time.perf_counter() - t0) * 1000
    response.headers["X-Request-ID"] = rid
    response.headers["X-Response-Time-ms"] = f"{ms:.1f}"
    log.info("%s %s -> %s %.1fms rid=%s", request.method, request.url.path, response.status_code, ms, rid)
    return response


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    if exc.status_code == 404:
        return JSONResponse(status_code=404, content={
            "error": "not_found",
            "message": f"No route for {request.method} {request.url.path}. Vera only speaks /v1/*.",
            "hint": "Check the path and HTTP method. Available endpoints are listed below.",
            "available_endpoints": ENDPOINTS,
            "docs": "/docs",
        })
    if exc.status_code == 405:
        allowed = [k for k in ENDPOINTS if k.split(" ", 1)[1] == request.url.path]
        return JSONResponse(status_code=405, content={
            "error": "method_not_allowed",
            "message": f"{request.method} is not supported on {request.url.path}.",
            "allowed": allowed,
        })
    return JSONResponse(status_code=exc.status_code, content={"error": "http_error", "message": str(exc.detail)})


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    details = [{"field": ".".join(str(x) for x in e.get("loc", [])[1:]), "issue": e.get("msg")} for e in exc.errors()]
    return JSONResponse(status_code=400, content={
        "accepted": False, "reason": "invalid_payload", "details": details})


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    log.exception("unhandled: %s", exc)
    return JSONResponse(status_code=500, content={
        "error": "internal_error", "message": "Unexpected error; request not processed."})


@app.get("/", include_in_schema=False)
async def root():
    return {"service": "vera", "version": VERSION, "endpoints": ENDPOINTS}


# --------------------------------------------------------------------------- #
# Health + metadata
# --------------------------------------------------------------------------- #
@app.get("/v1/healthz")
async def healthz():
    with store.lock:
        counts = store.counts()
        convs = len(store.conversations)
    return {"status": "ok", "uptime_seconds": int(time.time() - START),
            "contexts_loaded": counts, "conversations_active": convs, "version": VERSION,
            "llm": llm.provider_name()}


@app.get("/v1/metadata")
async def metadata():
    return {
        "team_name": os.getenv("TEAM_NAME", "Vera++"),
        "team_members": [m.strip() for m in os.getenv("TEAM_MEMBERS", "Your Name").split(",")],
        "model": os.getenv("MODEL_NAME", "deterministic-grounded-composer" + (f" + {llm.provider_name()} (grounded replies)" if llm.enabled() else "")),
        "approach": ("Trigger-routed, payload-driven composer over the 4 contexts; every fact traced to context "
                     "(zero fabrication), category voice + Hindi-English code-mix, taboo scrubbing; stateful "
                     "multi-turn handler with cross-thread auto-reply detection, intent->action switch, "
                     "hostile/off-topic handling and graceful exits."),
        "contact_email": os.getenv("CONTACT_EMAIL", "you@example.com"),
        "version": VERSION,
        "submitted_at": os.getenv("SUBMITTED_AT", "2026-09-27T00:00:00Z"),
    }


# --------------------------------------------------------------------------- #
# Context push
# --------------------------------------------------------------------------- #
class CtxBody(BaseModel):
    scope: str
    context_id: str = Field(min_length=1)
    version: int
    payload: Dict[str, Any]
    delivered_at: Optional[str] = None


@app.post("/v1/context")
def push_context(body: CtxBody):
    if body.scope not in VALID_SCOPES:
        return JSONResponse(status_code=400, content={
            "accepted": False, "reason": "invalid_scope",
            "details": f"scope must be one of {list(VALID_SCOPES)}, got '{body.scope}'"})
    key = (body.scope, body.context_id)
    with store.lock:
        cur = store.contexts.get(key)
        if cur:
            if cur["version"] > body.version:
                return JSONResponse(status_code=409, content={
                    "accepted": False, "reason": "stale_version", "current_version": cur["version"]})
            if cur["version"] == body.version:  # idempotent re-post
                return {"accepted": True, "ack_id": cur["ack_id"], "stored_at": cur["stored_at"], "idempotent": True}
        ack = f"ack_{uuid.uuid4().hex[:10]}"
        stored_at = utcnow_iso()
        store.contexts[key] = {"version": body.version, "payload": body.payload,
                               "ack_id": ack, "stored_at": stored_at}
    return {"accepted": True, "ack_id": ack, "stored_at": stored_at}


# --------------------------------------------------------------------------- #
# Tick — proactive sends
# --------------------------------------------------------------------------- #
class TickBody(BaseModel):
    now: Optional[str] = None
    available_triggers: List[str] = []


def _resolve(trigger: dict):
    merchant_id = trigger.get("merchant_id") or (trigger.get("payload") or {}).get("merchant_id")
    merchant = store.get("merchant", merchant_id)
    slug = (merchant or {}).get("category_slug") or (trigger.get("payload") or {}).get("category")
    category = store.get("category", slug)
    customer_id = trigger.get("customer_id") or (trigger.get("payload") or {}).get("customer_id")
    customer = store.get("customer", customer_id)
    return merchant_id, merchant, category, customer_id, customer


@app.post("/v1/tick")
def tick(body: TickBody):
    now = parse_dt(body.now) or datetime.now(timezone.utc)
    actions: List[Dict[str, Any]] = []
    with store.lock:
        candidates = []
        for tid in dict.fromkeys(body.available_triggers):  # dedupe, keep order
            trg = store.get("trigger", tid)
            if not trg:
                continue
            skey = trg.get("suppression_key") or f"trg:{tid}"
            if skey in store.sent_suppression:
                continue
            mid, merchant, category, cust_id, customer = _resolve(trg)
            if not merchant or not category:
                continue
            mem = store.memory(mid)
            if mem.get("opted_out") or mem.get("suppress_until_human"):
                continue
            if trg.get("scope") == "customer":
                if not customer or not customer_consented(customer):
                    continue  # never message a customer without consent
            candidates.append((int(trg.get("urgency") or 0), tid, trg, mid, merchant, category, cust_id, customer, skey))

        # Highest urgency first; at most one merchant-facing + one customer-facing send per merchant per tick.
        candidates.sort(key=lambda x: (-x[0], x[1]))
        used = set()
        for urgency, tid, trg, mid, merchant, category, cust_id, customer, skey in candidates:
            lane = (mid, "customer" if trg.get("scope") == "customer" else "merchant")
            if lane in used or len(actions) >= MAX_ACTIONS_PER_TICK:
                continue
            try:
                msg = _compose(category, merchant, trg, customer if trg.get("scope") == "customer" else None, now=now)
            except Exception:
                log.exception("compose failed for %s", tid)
                continue
            if not msg.get("body"):
                continue
            used.add(lane)
            store.conv_seq += 1
            conv_id = f"conv_{mid}_{trg.get('kind', 'msg')}_{store.conv_seq:04d}"
            store.conversations[conv_id] = {
                "conversation_id": conv_id, "merchant_id": mid, "customer_id": cust_id if trg.get("scope") == "customer" else None,
                "trigger_id": tid, "kind": trg.get("kind"), "status": "open",
                "turns": [{"from": "vera", "msg": msg["body"]}], "sent_bodies": [msg["body"]],
                "auto_reply_count": 0, "started_at": now.isoformat(),
            }
            store.sent_suppression[skey] = conv_id
            actions.append({
                "conversation_id": conv_id,
                "merchant_id": mid,
                "customer_id": cust_id if trg.get("scope") == "customer" else None,
                "send_as": msg["send_as"],
                "trigger_id": tid,
                "template_name": msg["template_name"],
                "template_params": msg["template_params"],
                "body": msg["body"],
                "cta": msg["cta"],
                "suppression_key": msg["suppression_key"],
                "rationale": msg["rationale"],
            })
    return {"actions": actions}


# --------------------------------------------------------------------------- #
# Reply — multi-turn
# --------------------------------------------------------------------------- #
class ReplyBody(BaseModel):
    conversation_id: str = Field(min_length=1)
    merchant_id: Optional[str] = None
    customer_id: Optional[str] = None
    from_role: str = "merchant"
    message: str = ""
    received_at: Optional[str] = None
    turn_number: Optional[int] = None


@app.post("/v1/reply")
def reply(body: ReplyBody):
    # 1) Resolve state + latest contexts under the global lock (fast, no I/O).
    with store.lock:
        state = store.conversations.get(body.conversation_id)
        if state is None:  # judge may open a thread we didn't start — handle it gracefully
            state = {"conversation_id": body.conversation_id, "merchant_id": body.merchant_id,
                     "customer_id": body.customer_id, "trigger_id": None, "kind": "generic",
                     "status": "open", "turns": [], "sent_bodies": [], "auto_reply_count": 0}
            # Adopt the most recent bot-initiated thread for this merchant so replies stay on-topic.
            prior = [c for c in store.conversations.values()
                     if c.get("merchant_id") == body.merchant_id and c.get("trigger_id")
                     and not c.get("customer_id") and c.get("started_at")]
            if prior:
                last = max(prior, key=lambda c: c["started_at"])
                state.update(trigger_id=last["trigger_id"], kind=last["kind"],
                             sent_bodies=list(last.get("sent_bodies", [])))
            store.conversations[body.conversation_id] = state
        conv_lock = store.conv_locks.setdefault(body.conversation_id, threading.Lock())
        mid = state.get("merchant_id") or body.merchant_id
        merchant = store.get("merchant", mid) or {}
        category = store.get("category", merchant.get("category_slug")) or {}
        trigger = store.get("trigger", state.get("trigger_id")) or {"kind": state.get("kind") or "generic", "payload": {}}
        cust_id = state.get("customer_id") or body.customer_id
        customer = store.get("customer", cust_id) if (cust_id and (body.from_role == "customer" or state.get("customer_id"))) else None
        memory = store.memory(mid)

    # 2) Handle the turn under a per-conversation lock, so a slow LLM call never blocks other traffic.
    with conv_lock:
        state["ctx"] = {"category": category, "merchant": merchant, "trigger": trigger, "customer": customer}
        state["merchant_memory"] = memory
        try:
            out = respond(state, body.message)
        except Exception:
            log.exception("respond failed conv=%s", body.conversation_id)
            out = {"action": "wait", "wait_seconds": 1800,
                   "rationale": "Internal handling error; backing off rather than sending a low-quality reply."}
        finally:
            state.pop("ctx", None)
            state.pop("merchant_memory", None)
    if out.get("action") == "send" and not str(out.get("body", "")).strip():  # contract: never send empty
        out = {"action": "wait", "wait_seconds": 1800, "rationale": "Nothing useful to add yet; backing off."}
    return out


# --------------------------------------------------------------------------- #
# Teardown
# --------------------------------------------------------------------------- #
@app.post("/v1/teardown")
def teardown():
    with store.lock:
        store.reset()
    return {"status": "wiped", "at": utcnow_iso()}


if __name__ == "__main__":  # pragma: no cover
    import uvicorn
    uvicorn.run("bot:app", host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
