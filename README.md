# Vera++ — magicpin Merchant AI Challenge

## Approach

A **deterministic, grounded composer** (`composer.py`) plus a **stateful conversation engine** (`conversation_handlers.py`) behind a FastAPI service (`bot.py`).

- **Trigger-routed, payload-driven.** 23 merchant-facing + 7 customer-facing handlers. Each one pulls the trigger payload, looks up the referenced digest item, and anchors on this merchant's own numbers (views, calls, CTR vs peer, cohort sizes, review themes, active offers). Trigger kinds it hasn't seen still get a message: the fallback checks the payload (digest reference → digest framing, metric/delta → perf framing, otherwise the merchant's biggest profile gap).
- **Zero fabrication by construction.** Every number comes from a context or from a labelled derivation (e.g. 245 members × 10% churn ≈ 24/month). If a payload is sparse, the message says less rather than making things up: no invented competitors, citations, slots or prices. Relative day counts are dropped once the clock passes a trigger's expiry.
- **Voice.** Salutations follow the category voice (`Dr. Meera`, owner first names). Messages switch to Hindi-English code-mix for Hindi-speaking merchants and customers, and use pure Hindi for `hi` customers (e.g. the senior pharmacy refill). Category taboo words are scrubbed. Every message ends on a single call to action.
- **Judgment, not just templates.** A weekend IPL match gets a delivery push instead of dine-in, citing the digest. A BOGO offer limited to Tue-Thu is flagged as not valid on a Sunday. A seasonal gym dip is reframed as a retention problem. A competitor opening is steered away from a price war toward the merchant's review strengths. A non-pharmacy "refill" becomes a routine visit.
- **Multi-turn.** Auto-replies are detected by pattern and by verbatim repetition, including across conversation threads for the same merchant. The bot sends one owner-directed nudge, then exits. A commitment switches it straight to action mode: it delivers the actual artifact (SOP checklist, patient note, recall message, tiered B2B pricing), then executes on the second yes and closes. It also handles opt-out (ends and suppresses future sends), hostility (de-escalates once), off-topic asks like GST (honest boundary and redirect), "later" (waits), price and source questions (answered from context), and per-turn language switching. It never repeats a message verbatim.
- **Restraint in `/tick`.** Dedup by `suppression_key`. At most one merchant-facing and one customer-facing send per merchant per tick, highest urgency first. No customer is messaged without consent, and nothing is sent after an opt-out or unresolved auto-reply.

- **Optional LLM for open-ended replies** (`llm.py`). It's off unless `LLM_API_KEY` is set. It supports OpenAI, Anthropic, OpenRouter, Groq, DeepSeek and Gemini. Calls use temperature 0, and results are cached so the same input gives the same output. The model only gets a JSON sheet of facts from the contexts. A validator rejects any reply containing a number, ₹ amount or % that isn't in those facts, and also rejects taboo words. A rejected reply falls back to the deterministic answer. It is used only for free-form questions and engaged replies. First messages and action steps stay deterministic.
- **Trigger kinds it hasn't seen** (the post-submission injections). Heatwave and local-news triggers have dedicated handlers. Any other kind gets a generic handler that states the event from its payload fields, then connects it to the merchant's numbers.
- **Honest numbers.** If a "dip" trigger comes with data that isn't actually negative, the message says the trend is flat and pivots to the gap against peers.

## Tradeoffs

- **Deterministic by default.** Messages take under 10 ms and can't hallucinate. The cost is less variety in wording. The LLM layer is limited to replies, where it helps most and the validator can catch fabrication.
- **In-memory state.** This matches the brief, but a restart loses state, so run a single worker. Endpoints run in FastAPI's threadpool with a lock per conversation, so a slow LLM call never blocks `/healthz` or other threads.

## What would help most

Real slot availability and service menus with prices per merchant, owner gender (for Hindi verb agreement), locality-level peer stats, and the date of each merchant's last GBP post and review.

## Run

```bash
pip install -r requirements.txt
python dataset/generate_dataset.py --seed-dir dataset --out expanded   # full dataset + test_pairs.json
python generate_submission.py --show                                   # -> submission.jsonl (30 lines)
python -m pytest -q tests                                              # 14 end-to-end tests
uvicorn bot:app --host 0.0.0.0 --port 8080                             # or: docker build -t vera . && docker run -p 8080:8080 vera
```

Public URL: `ngrok http 8080` or deploy the Dockerfile (Render/Fly/Railway). Set `TEAM_NAME`, `TEAM_MEMBERS`, `CONTACT_EMAIL` env vars for `/v1/metadata`.

**Endpoints:** `GET /v1/healthz`, `GET /v1/metadata`, `POST /v1/context`, `POST /v1/tick`, `POST /v1/reply`, `POST /v1/teardown`. Every error comes back as structured JSON: 400 malformed, 409 stale version, a helpful 404 that lists the endpoints, 405, and a 500 that never leaks a stack trace. Every response carries `X-Request-ID` and `X-Response-Time-ms` headers.
