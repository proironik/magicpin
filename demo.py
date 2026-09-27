"""
Demo console backend — a sandbox for humans, fully isolated from the judge.

* Reads the seed dataset from ./dataset (never the judge-pushed store).
* Sessions live in their own bounded LRU dict; nothing here touches
  suppression keys, merchant memory or conversations used by /v1/*.
"""
from __future__ import annotations

import json
import threading
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from composer import compose
from conversation_handlers import respond

ROOT = Path(__file__).parent
DATA = ROOT / "dataset"
STATIC = ROOT / "static"
MAX_SESSIONS = 300

router = APIRouter()
_lock = threading.Lock()
_sessions: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
_seed: Dict[str, Dict[str, dict]] = {}


def _load() -> Dict[str, Dict[str, dict]]:
    if _seed:
        return _seed
    cats = {}
    for f in (DATA / "categories").glob("*.json"):
        d = json.load(open(f, encoding="utf-8"))
        cats[d["slug"]] = d
    rd = lambda name, key: {x[key]: x for x in json.load(open(DATA / name, encoding="utf-8"))[name.split("_")[0]]}
    _seed.update(categories=cats,
                 merchants=rd("merchants_seed.json", "merchant_id"),
                 customers=rd("customers_seed.json", "customer_id"),
                 triggers=rd("triggers_seed.json", "id"))
    return _seed


def _merchant_card(m: dict) -> dict:
    idn, perf = m.get("identity", {}), m.get("performance", {})
    return {"name": idn.get("name"), "owner": idn.get("owner_first_name"), "locality": idn.get("locality"),
            "city": idn.get("city"), "category": m.get("category_slug"), "languages": idn.get("languages", []),
            "verified": idn.get("verified"), "plan": (m.get("subscription") or {}).get("plan"),
            "views": perf.get("views"), "calls": perf.get("calls"), "ctr": perf.get("ctr"),
            "offers": [o["title"] for o in m.get("offers", []) if o.get("status") == "active"]}


@router.get("/", include_in_schema=False)
def console():
    return FileResponse(STATIC / "index.html", media_type="text/html")


@router.get("/demo/api/scenarios")
def scenarios():
    s = _load()
    out = []
    for t in s["triggers"].values():
        m = s["merchants"].get(t.get("merchant_id"), {})
        c = s["customers"].get(t.get("customer_id") or "")
        out.append({"trigger_id": t["id"], "kind": t.get("kind"), "scope": t.get("scope"), "urgency": t.get("urgency"),
                    "merchant": (m.get("identity") or {}).get("name"), "category": m.get("category_slug"),
                    "customer": (c or {}).get("identity", {}).get("name")})
    out.sort(key=lambda x: (x["category"] or "", -(x["urgency"] or 0)))
    return {"scenarios": out}


class StartBody(BaseModel):
    trigger_id: str = Field(min_length=1, max_length=120)


@router.post("/demo/api/session")
def start(body: StartBody):
    s = _load()
    t = s["triggers"].get(body.trigger_id)
    if not t:
        return JSONResponse(status_code=404, content={"error": "unknown_scenario", "trigger_id": body.trigger_id})
    m = s["merchants"][t["merchant_id"]]
    cat = s["categories"][m["category_slug"]]
    cust = s["customers"].get(t.get("customer_id") or "") if t.get("scope") == "customer" else None
    msg = compose(cat, m, t, cust)
    sid = uuid.uuid4().hex[:16]
    state = {"conversation_id": f"demo_{sid}", "merchant_id": m["merchant_id"], "customer_id": (cust or {}).get("customer_id"),
             "trigger_id": t["id"], "kind": t.get("kind"), "status": "open",
             "turns": [{"from": "vera", "msg": msg["body"]}], "sent_bodies": [msg["body"]], "auto_reply_count": 0,
             "_ctx": {"category": cat, "merchant": m, "trigger": t, "customer": cust}, "_mem": {}}
    with _lock:
        _sessions[sid] = state
        while len(_sessions) > MAX_SESSIONS:
            _sessions.popitem(last=False)
    return {"session_id": sid, "message": msg, "merchant": _merchant_card(m),
            "customer": (cust or {}).get("identity"), "trigger": {"kind": t.get("kind"), "urgency": t.get("urgency"),
                                                                  "scope": t.get("scope"), "payload": t.get("payload")}}


class ReplyBody(BaseModel):
    message: str = Field(max_length=1000)


@router.post("/demo/api/session/{sid}/reply")
def demo_reply(sid: str, body: ReplyBody):
    with _lock:
        state: Optional[Dict[str, Any]] = _sessions.get(sid)
        if state is not None:
            _sessions.move_to_end(sid)
    if state is None:
        return JSONResponse(status_code=404, content={"error": "session_expired", "hint": "Start a new scenario."})
    state["ctx"], state["merchant_memory"] = state["_ctx"], state["_mem"]
    try:
        out = respond(state, body.message)
    finally:
        state.pop("ctx", None)
        state.pop("merchant_memory", None)
    return out
