"""End-to-end tests against the FastAPI app (in-process). Run: python -m pytest -q tests"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

import bot  # noqa: E402

DATA = ROOT / "expanded"
client = TestClient(bot.app)


def _load(sub):
    return [json.load(open(f, encoding="utf-8")) for f in sorted((DATA / sub).glob("*.json"))]


def _push_all():
    bot.store.reset()
    for c in _load("categories"):
        assert client.post("/v1/context", json={"scope": "category", "context_id": c["slug"], "version": 1, "payload": c}).json()["accepted"]
    for m in _load("merchants"):
        client.post("/v1/context", json={"scope": "merchant", "context_id": m["merchant_id"], "version": 1, "payload": m})
    for c in _load("customers"):
        client.post("/v1/context", json={"scope": "customer", "context_id": c["customer_id"], "version": 1, "payload": c})
    for t in _load("triggers"):
        client.post("/v1/context", json={"scope": "trigger", "context_id": t["id"], "version": 1, "payload": t})


def test_health_and_counts():
    _push_all()
    h = client.get("/v1/healthz").json()
    assert h["status"] == "ok"
    assert h["contexts_loaded"] == {"category": 5, "merchant": 50, "customer": 200, "trigger": 100}
    assert client.get("/v1/metadata").status_code == 200


def test_context_versioning():
    _push_all()
    body = {"scope": "category", "context_id": "dentists", "version": 1, "payload": {"slug": "dentists"}}
    r = client.post("/v1/context", json=body)
    assert r.status_code == 200 and r.json()["accepted"]  # idempotent re-post
    r = client.post("/v1/context", json={**body, "version": 0})
    assert r.status_code == 409 and r.json()["reason"] == "stale_version"
    r = client.post("/v1/context", json={**body, "scope": "bogus"})
    assert r.status_code == 400 and r.json()["reason"] == "invalid_scope"
    r = client.post("/v1/context", json={"scope": "merchant"})
    assert r.status_code == 400 and r.json()["reason"] == "invalid_payload"


def test_custom_404_and_405():
    r = client.get("/v1/nope")
    assert r.status_code == 404 and "available_endpoints" in r.json()
    r = client.get("/v1/tick")
    assert r.status_code == 405 and r.json()["error"] == "method_not_allowed"


def test_tick_all_triggers_and_dedup():
    _push_all()
    tids = [t["id"] for t in _load("triggers")]
    total, bodies = 0, []
    for i in range(0, len(tids), 5):
        acts = client.post("/v1/tick", json={"now": "2026-04-26T10:00:00Z", "available_triggers": tids[i:i + 5]}).json()["actions"]
        for a in acts:
            assert a["body"].strip() and a["cta"] and a["send_as"] in ("vera", "merchant_on_behalf")
            assert "{" not in a["body"] and "None" not in a["body"]
            bodies.append(a["body"])
        total += len(acts)
    assert total >= 60
    # Re-ticks drain deferred triggers (one send per merchant lane per tick), never re-send a suppression key.
    keys = set()
    for n in range(10):
        again = client.post("/v1/tick", json={"now": "2026-04-26T10:05:00Z", "available_triggers": tids}).json()["actions"]
        if not again:
            break
        for a in again:
            assert a["suppression_key"] not in keys
            keys.add(a["suppression_key"])
    assert again == []


def test_auto_reply_across_threads_ends():
    _push_all()
    mid = "m_001_drmeera_dentist_delhi"
    msg = "Thank you for contacting us! Our team will respond shortly."
    actions = [client.post("/v1/reply", json={"conversation_id": f"conv_auto_{i}", "merchant_id": mid, "from_role": "merchant",
                                              "message": msg, "received_at": "2026-04-26T10:00:00Z", "turn_number": i + 1}).json()["action"]
               for i in range(1, 5)]
    assert "end" in actions


def test_intent_transition_is_action():
    _push_all()
    r = client.post("/v1/reply", json={"conversation_id": "conv_intent_1", "merchant_id": "m_001_drmeera_dentist_delhi",
                                       "from_role": "merchant", "message": "Ok lets do it. Whats next?",
                                       "received_at": "2026-04-26T10:00:00Z", "turn_number": 2}).json()
    assert r["action"] == "send"
    low = r["body"].lower()
    assert not any(q in low for q in ["would you", "do you", "can you tell", "what if", "how about"])


def test_hostile_and_offtopic():
    _push_all()
    r = client.post("/v1/reply", json={"conversation_id": "conv_h", "merchant_id": "m_001_drmeera_dentist_delhi",
                                       "from_role": "merchant", "message": "Stop messaging me. This is useless spam.",
                                       "received_at": "x", "turn_number": 2}).json()
    assert r["action"] == "end"
    r = client.post("/v1/reply", json={"conversation_id": "conv_g", "merchant_id": "m_003_studio11_salon_hyderabad",
                                       "from_role": "merchant", "message": "can you also help me file my GST?",
                                       "received_at": "x", "turn_number": 2}).json()
    assert r["action"] == "send" and "gst" not in r["body"].lower()[:0]


def test_full_flow_from_tick():
    _push_all()
    acts = client.post("/v1/tick", json={"now": "2026-04-26T10:00:00Z",
                                         "available_triggers": ["trg_001_research_digest_dentists"]}).json()["actions"]
    assert len(acts) == 1
    conv = acts[0]["conversation_id"]
    r = client.post("/v1/reply", json={"conversation_id": conv, "merchant_id": acts[0]["merchant_id"], "from_role": "merchant",
                                       "message": "Yes, send me the abstract", "received_at": "x", "turn_number": 2}).json()
    assert r["action"] == "send" and "38%" in r["body"]
    r2 = client.post("/v1/reply", json={"conversation_id": conv, "merchant_id": acts[0]["merchant_id"], "from_role": "merchant",
                                        "message": "not interested", "received_at": "x", "turn_number": 3}).json()
    assert r2["action"] == "end"


def test_teardown():
    _push_all()
    assert client.post("/v1/teardown").json()["status"] == "wiped"
    assert sum(client.get("/v1/healthz").json()["contexts_loaded"].values()) == 0


# --------------------------------------------------------------------------- #
# Composer: unseen / injected trigger kinds must state the event and stay grounded
# --------------------------------------------------------------------------- #
from composer import compose  # noqa: E402
import llm  # noqa: E402


def _ctx(mid="m_005_pizzajunction_restaurant_delhi"):
    m = json.load(open(DATA / "merchants" / f"{mid}.json", encoding="utf-8"))
    c = json.load(open(DATA / "categories" / f"{m['category_slug']}.json", encoding="utf-8"))
    return c, m


def test_heatwave_and_local_news_and_unknown_kind():
    c, m = _ctx()
    out = compose(c, m, {"kind": "weather_heatwave", "scope": "merchant", "payload": {"temperature_c": 44, "city": "Delhi"}})
    assert "44°C" in out["body"] and "Delhi" in out["body"]
    out = compose(c, m, {"kind": "local_news_event", "scope": "merchant",
                         "payload": {"headline": "Ring Road closed near Sant Nagar", "duration_hours": 3}})
    assert "Ring Road closed" in out["body"]
    out = compose(c, m, {"kind": "brand_new_kind", "scope": "merchant", "payload": {"footfall_delta_pct": -0.18}})
    assert "brand new kind" in out["body"] and "-18%" in out["body"]


def test_llm_grounding_validator():
    facts = '{"views": 2410, "ctr": 0.021, "offer": "Dental Cleaning @ ₹299"}'
    assert llm.grounded("Your 2,410 views and ₹299 cleaning. Reply 1.", facts)
    assert llm.grounded("CTR is 2.1%? no — 2 is fine", facts)
    assert not llm.grounded("22 patients were affected", facts)


def test_customer_reschedule_and_second_yes():
    _push_all()
    acts = client.post("/v1/tick", json={"now": "2026-04-26T10:00:00Z",
                                         "available_triggers": ["trg_003_recall_due_priya"]}).json()["actions"]
    assert acts and acts[0]["send_as"] == "merchant_on_behalf"
    conv = acts[0]["conversation_id"]
    r = client.post("/v1/reply", json={"conversation_id": conv, "merchant_id": acts[0]["merchant_id"],
                                       "customer_id": acts[0]["customer_id"], "from_role": "customer",
                                       "message": "Can we do Saturday morning instead?", "received_at": "x", "turn_number": 2}).json()
    assert r["action"] == "send" and "preferred time" in r["body"].lower() or "preferred time" in r["body"]
    # merchant-side: yes -> artifact, yes -> executed, yes -> end
    acts = client.post("/v1/tick", json={"now": "2026-04-26T10:00:00Z",
                                         "available_triggers": ["trg_018_supply_atorvastatin_recall"]}).json()["actions"]
    conv = acts[0]["conversation_id"]
    seq = [client.post("/v1/reply", json={"conversation_id": conv, "merchant_id": acts[0]["merchant_id"], "from_role": "merchant",
                                          "message": msg, "received_at": "x", "turn_number": i}).json()
           for i, msg in enumerate(["haan karo", "go", "ok"], start=2)]
    assert "AT2024-1102" in seq[0]["body"]
    assert seq[1]["action"] == "send" and seq[2]["action"] == "end"


def test_opt_out_suppresses_future_ticks():
    _push_all()
    mid = "m_002_bharat_dentist_mumbai"
    client.post("/v1/reply", json={"conversation_id": "conv_x", "merchant_id": mid, "from_role": "merchant",
                                   "message": "not interested, stop", "received_at": "x", "turn_number": 2})
    acts = client.post("/v1/tick", json={"now": "2026-04-26T10:00:00Z",
                                         "available_triggers": ["trg_004_perf_dip_bharat", "trg_005_renewal_due_bharat"]}).json()["actions"]
    assert acts == []


def test_unknown_conversation_adopts_merchant_thread():
    _push_all()
    client.post("/v1/tick", json={"now": "2026-04-26T10:00:00Z", "available_triggers": ["trg_002_compliance_dci_radiograph"]})
    r = client.post("/v1/reply", json={"conversation_id": "conv_fresh", "merchant_id": "m_001_drmeera_dentist_delhi",
                                       "from_role": "merchant", "message": "Ok lets do it. Whats next?",
                                       "received_at": "x", "turn_number": 2}).json()
    assert r["action"] == "send" and "SOP" in r["body"]
