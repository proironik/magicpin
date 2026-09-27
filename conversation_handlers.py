"""
Multi-turn conversation handling for Vera.

respond(state, merchant_message) -> {"action": "send"|"wait"|"end", ...}

`state` is a plain dict maintained by the server:
    {
      "conversation_id": str, "merchant_id": str, "customer_id": str|None,
      "trigger_id": str|None, "kind": str,
      "turns": [{"from": "vera"|"merchant"|"customer", "msg": str}],
      "sent_bodies": [str],             # anti-repetition
      "auto_reply_count": int,
      "merchant_memory": dict,          # shared across ALL conversations of this merchant
      "ctx": {"category": {}, "merchant": {}, "trigger": {}, "customer": {}|None},
      "status": "open"|"ended",
    }

Classification order (first match wins):
    opt-out / hard no  -> end
    auto-reply         -> 1 owner-directed nudge, then end (also detected across conversations)
    abuse (no opt-out) -> apologise once + offer STOP; end if it repeats
    commitment         -> ACTION mode: deliver the artifact, no more qualifying
    later / busy       -> wait
    off-topic ask      -> polite boundary + redirect to the mission
    question           -> grounded answer from contexts
    objection          -> address once, keep single CTA
    engaged / other    -> advance one step
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from composer import Ctx, inr, num, pct, humanize, _g

# --------------------------------------------------------------------------- #
# Lexicons
# --------------------------------------------------------------------------- #
AUTO_REPLY_PATTERNS = [
    r"thank(s| you) for (contacting|reaching|your message|messaging)",
    r"(our|the) team will (get back|respond|contact|reach)",
    r"will (get back|respond) (to you )?(shortly|soon|within)",
    r"(we are|we're|i am|i'm) (currently )?(unavailable|away|out of (the )?office|closed)",
    r"automated (assistant|message|reply|response)",
    r"auto[- ]?reply",
    r"business hours",
    r"this is an automated",
    r"aapki (jaankari|madad) ke liye (bahut[- ]bahut )?shukriya",
    r"team tak pahuncha",
    r"hum jald hi (aapse )?(sampark|contact)",
    r"please (leave|drop) (a|your) message",
    r"for (urgent|immediate) (queries|assistance),? (please )?call",
]
OPT_OUT_PATTERNS = [
    r"\bstop\b", r"\bunsubscribe\b", r"don'?t (message|text|contact|msg)", r"do not (message|text|contact)",
    r"not interested", r"no interest", r"remove (me|my number)", r"\bspam\b", r"leave me alone",
    r"band karo", r"mat bhejo", r"message mat", r"nahi chahiye", r"interest nahi", r"mujhe nahi chahiye",
    r"block (kar|you)", r"never (message|contact)",
]
ABUSE_PATTERNS = [
    r"\b(idiot|stupid|useless|nonsense|bakwas|bekaar|pagal|fraud|scam|chor|shut up|bloody|damn|wtf|f+u+c+k+|bc|mc|chutiya)\b",
]
COMMIT_PATTERNS = [
    r"\b(yes|yeah|yep|yup|ok(ay)?|sure|done|confirm(ed)?|go ahead|proceed|let'?s (do|go|start)|do it|sounds good|perfect|great|please do|send( it| me)?|go)\b",
    r"\b(haan|haa|han|ha ji|haanji|ji haan|theek hai|thik hai|chalo|chalega|kar do|karo|bhej do|bhejo|shuru karo|zaroor|bilkul)\b",
    r"^\s*(1|2|3)\s*$", r"i want to join", r"judna hai", r"judrna hai", r"sign me up",
]
LATER_PATTERNS = [
    r"\b(later|busy|in a meeting|call (me )?(later|tomorrow)|tomorrow|next week|not now|abhi nahi|baad mein|kal|thodi der)\b",
]
OFF_TOPIC_PATTERNS = [
    r"\bgst\b", r"income tax", r"\bitr\b", r"\bloan\b", r"\bvisa\b", r"\binsurance\b", r"\blawyer\b|legal notice",
    r"cricket score", r"\bstock(s)? (tip|market)\b", r"\bcrypto\b", r"\bpassport\b", r"electricity bill",
    r"\baccountant\b", r"\bca\b (help|ka kaam)",
]
QUESTION_PATTERNS = [r"\?", r"^(what|how|why|when|where|which|who|can|could|is|are|will|does|do)\b",
                     r"\b(kya|kaise|kitna|kitne|kab|kahan|kyun|kaun)\b"]
PRICE_PATTERNS = [r"\b(price|cost|charge|fee|kitna|kitne|paisa|paise|rate|how much)\b"]
OBJECTION_PATTERNS = [r"too (expensive|costly)|mehenga|mahenga|not (worth|useful)|doesn'?t work|no (time|budget)|already (have|using)"]

HINGLISH_TOKENS = {"hai", "hain", "kya", "nahi", "nahin", "karo", "kar", "mujhe", "aap", "aapka", "haan", "ji",
                   "chahiye", "kaise", "kitna", "theek", "thik", "bhai", "acha", "accha", "abhi", "baad", "mein",
                   "hoga", "karna", "bhej", "do", "dijiye", "batao", "bataiye", "chalo", "kal", "wala", "raha", "rahi"}


def _any(patterns: List[str], text: str) -> bool:
    return any(re.search(p, text, flags=re.IGNORECASE) for p in patterns)


def detect_lang(text: str, default_hinglish: bool) -> bool:
    """True -> reply in Hindi-English code-mix."""
    if re.search(r"[\u0900-\u097F]", text):
        return True
    words = re.findall(r"[a-zA-Z]+", text.lower())
    if not words:
        return default_hinglish
    hits = sum(1 for w in words if w in HINGLISH_TOKENS and w != "do")
    if hits >= 2 or (hits >= 1 and len(words) <= 4):
        return True
    if len(words) >= 3 and hits == 0:
        return False
    return default_hinglish


def classify(text: str) -> str:
    t = (text or "").strip()
    low = t.lower()
    if not t:
        return "empty"
    if _any(AUTO_REPLY_PATTERNS, low):
        return "auto_reply"
    if _any(OPT_OUT_PATTERNS, low):
        return "opt_out"
    if _any(ABUSE_PATTERNS, low):
        return "abuse"
    if _any(OFF_TOPIC_PATTERNS, low):
        return "off_topic"
    if _any(LATER_PATTERNS, low) and not re.search(r"\b(yes|haan|ok)\b", low):
        return "later"
    is_q = _any(QUESTION_PATTERNS, low)
    if _any(COMMIT_PATTERNS, low) and not (is_q and not re.search(r"what'?s next|aage kya|next\??$", low)):
        return "commit"
    if re.search(r"what'?s next|aage kya", low):
        return "commit"
    if _any(OBJECTION_PATTERNS, low):
        return "objection"
    if is_q:
        return "question"
    if re.search(r"^\s*(no|nope|nah|nahi|na)\b", low):
        return "soft_no"
    return "engaged"


# --------------------------------------------------------------------------- #
# Artifacts delivered in ACTION mode (after commitment)
# --------------------------------------------------------------------------- #
def _artifact(ctx: Ctx, hing: bool, text: str = "") -> str:
    k, p, sal = ctx.kind, ctx.p, ctx.sal
    H = (lambda en, hi: hi if hing else en)
    offer, own = ctx.best_offer()
    item = ctx.digest_item() or {}

    if ctx.c is not None:  # customer said yes
        slots = [s.get("label") for s in (p.get("available_slots") or p.get("next_session_options") or []) if s.get("label")]
        if k == "chronic_refill_due":
            return H("Confirmed — dispatching the same prescription to your saved address. You'll get a message when it's out for delivery.",
                     "Confirm ho gaya — same prescription saved address pe dispatch kar rahe hain. Out for delivery hote hi message aayega.")
        pick = re.match(r"^\s*([1-9])\s*$", text or "")
        idx = int(pick.group(1)) - 1 if pick else 0
        slot = slots[idx] if slots and 0 <= idx < len(slots) else (slots[0] if slots else None)
        return H(f"Booked{(' for ' + slot) if slot else ''}. We'll send a reminder the day before. Reply CHANGE anytime to move it.",
                 f"Booking ho gayi{(' — ' + slot) if slot else ''}. Ek din pehle reminder bhejenge. Time badalna ho toh CHANGE reply karein.")

    if k in ("research_digest", "category_research_digest_release") or item.get("kind") in ("research", "trend", "tech"):
        from composer import first_sentence
        draft = (f"\"{ctx.mname} update: {first_sentence(item.get('summary', ''))} "
                 f"Ask us at your next visit whether this applies to you.\"") if item else ""
        return H(f"Done. Abstract summary: {item.get('summary', '')} Patient WhatsApp draft: {draft} Reply GO to use it as-is, or tell me what to change.",
                 f"Ho gaya. Abstract summary: {item.get('summary', '')} Patient WhatsApp draft: {draft} Reply GO to use as-is, ya bataiye kya badalna hai.")
    if k in ("regulation_change", "compliance_update") or item.get("kind") == "compliance":
        return H(f"Here's the SOP checklist: 1) List every X-ray unit + film/sensor type. 2) Mark each against the new rule: {item.get('summary', '')} "
                 f"3) {item.get('actionable', 'Document compliance in your SOP')}. 4) Sign, date, file. Reply GO and I'll send it as a printable PDF.",
                 f"SOP checklist ready: 1) Har X-ray unit + film/sensor type list karein. 2) Naye rule se match karein: {item.get('summary', '')} "
                 f"3) {item.get('actionable', 'SOP mein document karein')}. 4) Sign, date, file. Reply GO — printable PDF bhej deti hoon.")
    if k == "cde_opportunity" or item.get("kind") == "cde":
        return H(f"Reminder set for the day before {item.get('title', 'the session')}. I'll also send a 3-point prep note that morning.",
                 f"{item.get('title', 'Session')} se ek din pehle reminder set. Us subah 3-point prep note bhi bhej dungi.")
    if k == "supply_alert":
        mol = p.get("molecule", "the affected molecule")
        return H(f"Starting now: filtering your repeat-Rx list for {mol} batches {', '.join(p.get('affected_batches') or [])}. "
                 f"Customer note draft: \"Namaste, {ctx.mname} se. Aapki {mol} ki ek batch manufacturer ne voluntary recall ki hai (safety risk nahi). "
                 f"Free replacement ke liye strip counter pe le aayein ya reply karein.\" Reply GO to send it to the filtered list.",
                 f"Shuru kar diya: repeat-Rx list mein {mol} batches {', '.join(p.get('affected_batches') or [])} filter ho rahe hain. "
                 f"Customer note draft: \"Namaste, {ctx.mname} se. Aapki {mol} ki ek batch manufacturer ne voluntary recall ki hai (safety risk nahi). "
                 f"Free replacement ke liye strip counter pe le aayein ya reply karein.\" Filtered list ko bhejne ke liye GO reply karein.")
    if k == "active_planning_intent":
        return H(f"Locked. Next: I'm turning the draft into a Google post + WhatsApp flyer for {ctx.mname}. You'll get both for approval in 10 minutes — reply GO to publish once you've seen them.",
                 f"Lock ho gaya. Ab draft ko Google post + WhatsApp flyer bana rahi hoon. 10 min mein approval ke liye dono aa jayenge — dekh ke GO reply karein.")
    if k in ("renewal_due", "winback_eligible"):
        plan = _g(ctx.m, "subscription", "plan", default="your")
        amt = p.get("renewal_amount")
        return H(f"Sending the {plan} renewal link now" + (f" ({inr(amt)})" if amt else "") + ". The moment it's paid I'll restart profile upkeep and draft the comeback message for your lapsed customers.",
                 f"{plan} renewal link abhi bhej rahi hoon" + (f" ({inr(amt)})" if amt else "") + ". Payment hote hi profile upkeep restart + lapsed customers ke liye comeback message draft.")
    if k == "gbp_unverified":
        return H("Step 1 of 2: open Google Business Profile → 'Get verified' → choose phone call. Share the 6-digit code here when it arrives and I'll finish the rest.",
                 "Step 1 of 2: Google Business Profile kholiye → 'Get verified' → phone call choose karein. 6-digit code aate hi yahan bhej dijiye, baaki main kar dungi.")
    if k == "review_theme_emerged":
        theme = humanize(p.get("theme", "this issue"))
        return H(f"Public reply draft: \"Thank you for the feedback — you're right about the {theme}. We've changed how we handle it this week and would love to make it up to you on your next order.\" "
                 f"Team note: \"{theme.capitalize()} came up {p.get('occurrences_30d', 'several')} times this month — let's fix it this week.\" Reply GO to post the replies.",
                 f"Public reply draft: \"Feedback ke liye shukriya — {theme} ke baare mein aap sahi hain. Is hafte process badal diya hai, next order pe zaroor fark dikhega.\" "
                 f"Team note: \"{theme.capitalize()} is mahine {p.get('occurrences_30d', 'kai')} baar aaya — is hafte fix karte hain.\" Replies post karne ke liye GO.")
    if k == "ipl_match_today":
        combo = next((o.get("title") for o in ctx.cat.get("offer_catalog") or [] if "match" in str(o.get("title", "")).lower()), offer)
        return H(f"Setting up '{combo}' as a tonight-only offer on your listing now, plus a short post: \"{p.get('match', 'Match')} tonight — {combo}, delivered hot before the first over.\" Live in 10 minutes; reply STOP to pull it.",
                 f"'{combo}' ko tonight-only offer bana ke listing pe daal rahi hoon, saath mein post: \"{p.get('match', 'Match')} aaj raat — {combo}, first over se pehle garam delivery.\" 10 min mein live; hatana ho toh STOP.")
    if k in ("milestone_reached",):
        return H(f"QR review card ready for {ctx.mname}: 'Loved it? 30 seconds on Google helps us a lot' + your review QR. Sending the print-ready PDF now.",
                 f"{ctx.mname} ke liye QR review card ready: 'Accha laga? Google pe 30 second ka review bahut madad karta hai' + QR. Print-ready PDF bhej rahi hoon.")
    if k in ("seasonal_perf_dip",):
        members = ctx.agg.get("total_active_members")
        return H(f"6-week challenge draft: 3 check-ins/week, weekly leaderboard, small prize for top 10" + (f" out of your {num(members)} members" if members else "") + ". WhatsApp announcement is ready — reply GO to send it.",
                 f"6-week challenge draft: 3 check-ins/week, weekly leaderboard, top 10 ke liye chhota prize" + (f" ({num(members)} members mein se)" if members else "") + ". WhatsApp announcement ready — bhejne ke liye GO.")
    if k == "curious_ask_due":
        return H("Got it — drafting the Google post + price-reply now. Both arrive here in 10 minutes for a quick approve.",
                 "Samajh gayi — Google post + price-reply abhi draft kar rahi hoon. 10 min mein approval ke liye yahin aa jayenge.")
    if k == "category_seasonal":
        return H("Here's the customer note: \"Summer care checklist from " + ctx.mname + ": ORS, SPF 50+ sunscreen, anti-fungal cream — all in stock, home delivery available.\" Reply GO to send it to your repeat customers.",
                 "Customer note: \"" + ctx.mname + " se summer care checklist: ORS, SPF 50+ sunscreen, anti-fungal cream — sab stock mein, home delivery available.\" Repeat customers ko bhejne ke liye GO.")
    # perf_dip / perf_spike / competitor / festival / dormant / generic
    if not ctx.m:  # no merchant context pushed for this thread — don't pretend we have data
        return H("On it. I'm preparing the next step now and will send it here for a one-tap approval in 10 minutes. Reply STOP anytime to cancel.",
                 "Kar rahi hoon. Agla step ready karke 10 min mein yahin bhejti hoon, ek reply se approve kar dijiye. Cancel karna ho toh STOP.")
    where = f"{ctx.mname}" + (f", {ctx.locality}" if ctx.locality else "")
    post = f"\"{where}: {offer} — walk in or call to book.\"" if offer else f"\"{where} — open today, call to book.\""
    return H(f"On it. Draft Google post: {post} I'll also refresh your listing photos order and description. Reply GO to publish.",
             f"Kar rahi hoon. Google post draft: {post} Listing ka description aur photo order bhi refresh kar dungi. Publish ke liye GO.")


def _answer_question(ctx: Ctx, text: str, hing: bool) -> str:
    H = (lambda en, hi: hi if hing else en)
    low = text.lower()
    p = ctx.p
    if _any(PRICE_PATTERNS, low):
        amt = p.get("renewal_amount")
        plan = _g(ctx.m, "subscription", "plan")
        offers = ctx.active_offers()
        if amt:
            return H(f"The {plan} plan renewal is {inr(amt)}. Nothing extra for the drafts I'm offering — they're included. Reply YES and I'll start.",
                     f"{plan} plan renewal {inr(amt)} hai. Drafts ka koi extra charge nahi — included hai. Reply YES, main shuru karti hoon.")
        if offers:
            return H(f"Your live offer is {offers[0]}. The post/draft work I do is included in your plan — no extra cost. Reply YES to go ahead.",
                     f"Aapka live offer {offers[0]} hai. Post/draft ka kaam plan mein included hai — koi extra cost nahi. Reply YES.")
        return H("There's no extra cost for this — it's part of what I do on your listing. Reply YES to go ahead.",
                 "Iska koi extra cost nahi — yeh listing ke kaam ka hissa hai. Reply YES.")
    item = ctx.digest_item()
    if item and re.search(r"source|where|kahan se|link|proof|study|trial", low):
        return H(f"Source: {item.get('source')}. Summary: {item.get('summary')} Reply YES and I'll send the full breakdown.",
                 f"Source: {item.get('source')}. Summary: {item.get('summary')} Reply YES — poora breakdown bhej deti hoon.")
    if re.search(r"how long|kitna time|kab tak|when", low):
        return H("About 10 minutes from your YES for the draft; Google changes can take 24-48 hours to show. Reply YES to start the clock.",
                 "YES ke 10 min mein draft; Google pe changes dikhne mein 24-48 ghante lag sakte hain. Reply YES.")
    if re.search(r"how|kaise|what (will|would) you|kya karogi|kya karoge", low):
        return H("Simple: I draft it, you approve with one reply, I publish. You never need to log in. Reply YES to start.",
                 "Simple hai: main draft karti hoon, aap ek reply se approve karte hain, main publish kar deti hoon. Login ki zaroorat nahi. Reply YES.")
    if re.search(r"who are you|kaun|what is (this|vera)|magicpin", low):
        return H("I'm Vera, magicpin's assistant for your Google listing and campaigns. I only act when you approve. Reply YES to continue.",
                 "Main Vera hoon, magicpin ki taraf se aapki Google listing aur campaigns ke liye. Aapke approve kiye bina kuch nahi karti. Reply YES.")
    v, c = ctx.perf.get("views"), ctx.perf.get("calls")
    if v is None or c is None:
        return H("Good question — I'll only answer with your actual listing data, so let me pull it and reply here shortly. Reply YES if I should also prepare the next step meanwhile.",
                 "Accha sawaal — main sirf aapke actual listing data se jawab dungi, thodi der mein yahin bhejti hoon. Tab tak agla step ready karoon? Reply YES.")
    return H(f"Good question. From your data: {num(v)} views and {num(c)} calls in the last 30 days. The step I suggested is the quickest lift from there. Reply YES and I'll handle it.",
             f"Accha sawaal. Aapke data se: pichhle 30 din mein {num(v)} views aur {num(c)} calls. Maine jo step bataya, wahi sabse fast lift hai. Reply YES, main sambhal leti hoon.")


TOPIC_LABELS = {
    "research_digest": ("the research summary + patient note", "research summary + patient note ready"),
    "regulation_change": ("the compliance checklist", "compliance checklist ready"),
    "cde_opportunity": ("the webinar reminder", "webinar reminder set"),
    "competitor_opened": ("a listing refresh against the new competitor", "naye competitor ke against listing refresh"),
    "perf_dip": ("the fix for your drop in calls", "calls ki drop ka fix"),
    "perf_spike": ("a follow-up post while momentum lasts", "momentum ke rehte follow-up post"),
    "milestone_reached": ("the review QR card", "review QR card ready"),
    "review_theme_emerged": ("replies to the recent reviews", "recent reviews ke replies"),
    "curious_ask_due": ("this week's Google post", "is hafte ka Google post"),
    "festival_upcoming": ("the festive early-bird post", "festive early-bird post"),
    "ipl_match_today": ("tonight's match-day offer", "aaj raat ka match-day offer"),
    "active_planning_intent": ("the draft plan you asked for", "aapka maanga hua draft plan"),
    "renewal_due": ("your renewal", "aapka renewal"),
    "winback_eligible": ("a comeback message to lapsed customers", "lapsed customers ke liye comeback message"),
    "dormant_with_vera": ("a 3-line profile summary", "3-line profile summary"),
    "gbp_unverified": ("your Google verification", "aapka Google verification"),
    "supply_alert": ("the recall customer list + note", "recall customer list + note"),
    "category_seasonal": ("the seasonal customer note", "seasonal customer note"),
}


def _topic(ctx: Ctx):
    return TOPIC_LABELS.get(ctx.kind, ("your listing and offers", "aapki listing aur offers ka kaam"))


def _key_fact(ctx: Ctx) -> Optional[str]:
    """The single most useful verifiable fact for this thread (used to advance engaged replies)."""
    from composer import first_sentence
    item = ctx.digest_item()
    if item and item.get("summary"):
        return first_sentence(item["summary"]) + (f" ({item.get('source')})" if item.get("source") else "")
    p = ctx.p
    if p.get("metric") and p.get("delta_pct") is not None:
        return f"{humanize(p['metric']).capitalize()} moved {pct(p['delta_pct'], signed=True)} over {humanize(p.get('window', '7d'))}."
    pos = ctx.review_theme("pos")
    if pos:
        return f"{pos.get('occurrences_30d')} reviews this month praise your {humanize(pos.get('theme'))}."
    v, c = ctx.perf.get("views"), ctx.perf.get("calls")
    if v is not None and c is not None:
        return f"{num(v)} views and {num(c)} calls in the last 30 days."
    return None


def _facts_blob(ctx: Ctx) -> str:
    """Compact, JSON fact sheet given to the LLM — the ONLY facts it may use."""
    m = ctx.m or {}
    facts = {
        "merchant": {k: m.get(k) for k in ("identity", "subscription", "performance", "offers",
                                             "customer_aggregate", "signals", "review_themes")},
        "category": {"slug": ctx.slug, "voice": _g(ctx.cat, "voice", "tone"),
                     "taboos": _g(ctx.cat, "voice", "vocab_taboo"),
                     "offer_catalog": [o.get("title") for o in (ctx.cat.get("offer_catalog") or [])],
                     "peer_stats": ctx.cat.get("peer_stats")},
        "trigger": {"kind": ctx.kind, "payload": ctx.p},
        "digest_item": ctx.digest_item(),
        "customer": ctx.c,
    }
    return json.dumps(facts, ensure_ascii=False, default=str)[:6000]


LLM_SYSTEM = """You are Vera, magicpin's WhatsApp assistant for Indian local merchants. Write ONE WhatsApp reply.
Hard rules:
- Use ONLY facts in FACTS. Never invent numbers, prices, dates, names, links, studies or competitors. If the answer isn't in FACTS, say you'll check and come back.
- Peer/colleague tone matching the category voice; no hype, no taboo words; no self-introduction; no preamble.
- Match the language of the latest message (Hindi-English code-mix if they wrote Hinglish).
- If the request is outside local marketing/listing/customer outreach (tax, GST, loans, legal), politely say it's outside your scope and steer back.
- Max 60 words. End with exactly one clear next step (e.g. "Reply YES ..."). Output only the message text."""


def _llm_reply(ctx: Ctx, state: Dict[str, Any], text: str) -> Optional[str]:
    import llm
    if not llm.enabled():
        return None
    facts = _facts_blob(ctx)
    history = "\n".join(f"{t.get('from')}: {t.get('msg')}" for t in (state.get("turns") or [])[-6:])
    audience = "the merchant's CUSTOMER (you write on the merchant's behalf)" if ctx.c is not None else "the MERCHANT"
    user = f"FACTS:\n{facts}\n\nAUDIENCE: {audience}\n\nCONVERSATION SO FAR:\n{history}\n\nLATEST MESSAGE:\n{text}\n\nReply:"
    out = llm.complete(LLM_SYSTEM, user, max_tokens=220)
    if not out or len(out) > 700:
        return None
    if not llm.grounded(out, facts + " " + history + " " + text):
        return None  # a number not in context -> treat as fabrication, fall back
    taboos = [str(t).lower() for t in (_g(ctx.cat, "voice", "vocab_taboo", default=[]) or [])]
    if any(t and t.split("(")[0].strip() in out.lower() for t in taboos):
        return None
    return out


def _customer_reply(ctx: Ctx, text: str, label: str, hing: bool) -> Optional[str]:
    """Customer-side (merchant_on_behalf) non-commit replies."""
    H = (lambda en, hi: hi if hing else en)
    low = text.lower()
    if re.search(r"(mon|tue|wed|thu|fri|sat|sun)(day)?|morning|evening|afternoon|\d{1,2}\s?(am|pm)|resched|another time|shaam|subah", low):
        return H(f"Noted — I've passed your preferred time to {ctx.mname}. We'll confirm the exact slot here shortly.",
                 f"Note kar liya — aapka preferred time {ctx.mname} ko bhej diya. Exact slot yahin confirm karenge.")
    if _any(PRICE_PATTERNS, low):
        offers = ctx.active_offers()
        if offers:
            return H(f"Current offer at {ctx.mname}: {offers[0]}. Reply YES and we'll hold a slot for you.",
                     f"{ctx.mname} ka abhi offer: {offers[0]}. Reply YES — aapke liye slot hold kar denge.")
        return H(f"The team at {ctx.mname} will share the exact price for your visit. Reply YES and we'll hold a slot meanwhile.",
                 f"{ctx.mname} ki team exact price bata degi. Tab tak slot hold karein? Reply YES.")
    return None


# --------------------------------------------------------------------------- #
# Main entry
# --------------------------------------------------------------------------- #
def _send(state: Dict[str, Any], body: str, rationale: str, cta: str = "binary_yes_stop") -> Dict[str, Any]:
    sent = state.setdefault("sent_bodies", [])
    if body in sent:  # never repeat verbatim
        body = body + " (Just reply YES or STOP — either is fine.)"
        if body in sent:
            return _end(state, "Would have repeated an earlier message verbatim; exiting instead.")
    sent.append(body)
    state.setdefault("turns", []).append({"from": "vera", "msg": body})
    return {"action": "send", "body": body, "cta": cta, "rationale": rationale}


def _end(state: Dict[str, Any], rationale: str) -> Dict[str, Any]:
    state["status"] = "ended"
    return {"action": "end", "rationale": rationale}


def _wait(state: Dict[str, Any], seconds: int, rationale: str) -> Dict[str, Any]:
    return {"action": "wait", "wait_seconds": int(seconds), "rationale": rationale}


def respond(state: Dict[str, Any], merchant_message: str) -> Dict[str, Any]:
    ctxd = state.get("ctx") or {}
    ctx = Ctx(ctxd.get("category") or {}, ctxd.get("merchant") or {}, ctxd.get("trigger") or {},
              ctxd.get("customer"), None)
    mem = state.setdefault("merchant_memory", {})
    turns = state.setdefault("turns", [])
    text = (merchant_message or "").strip()
    prior_inbound = [t["msg"] for t in turns if t.get("from") != "vera"]
    turns.append({"from": "customer" if ctx.c is not None else "merchant", "msg": text})

    hing = detect_lang(text, ctx.hinglish if ctx.c is None else ctx.clang != "en")
    H = (lambda en, hi: hi if hing else en)
    label = classify(text)

    # Verbatim repetition = auto-reply, even if the wording isn't in our lexicon.
    norm = re.sub(r"\W+", " ", text.lower()).strip()
    seen = mem.setdefault("inbound_norm_counts", {})
    seen[norm] = seen.get(norm, 0) + 1
    # Only long-ish canned texts count (short "yes"/"ok" repeats are genuine).
    if len(norm) >= 25 and (seen[norm] >= 2 or prior_inbound.count(text) >= 1) \
            and label not in ("opt_out", "commit"):
        label = "auto_reply"

    if state.get("status") == "ended" and label not in ("commit", "question"):
        return _end(state, "Conversation already closed; not re-engaging.")

    if label == "empty":
        return _wait(state, 1800, "Empty inbound; waiting 30 min instead of guessing.")

    if label == "opt_out":
        mem["opted_out"] = True
        return _end(state, "Merchant opted out / not interested — exiting immediately and suppressing further sends to this merchant.")

    if label == "auto_reply":
        state["auto_reply_count"] = state.get("auto_reply_count", 0) + 1
        mem["auto_reply_total"] = mem.get("auto_reply_total", 0) + 1
        if state["auto_reply_count"] == 1 and mem["auto_reply_total"] <= 1:
            return _send(state,
                         H("Looks like an auto-reply, no worries. For the owner when they're free: it's a 1-line approval, nothing to fill in. Reply YES whenever you see this.",
                           "Lagta hai yeh auto-reply hai, koi baat nahi. Owner ke liye: bas 1-line approval chahiye, kuch bharna nahi hai. Jab dekhein, YES reply kar dijiye."),
                         "Detected WhatsApp Business auto-reply; one short owner-directed nudge, no repeat of the pitch.")
        if mem["auto_reply_total"] == 2 and state["auto_reply_count"] == 1:
            return _wait(state, 86400, "Auto-reply seen again for this merchant (another thread); backing off 24h instead of burning turns.")
        mem["suppress_until_human"] = True
        return _end(state, "Repeated auto-reply — no human on the line. Exiting gracefully; will only resume on a genuine merchant message.")

    # A genuine human reply clears auto-reply suppression.
    mem.pop("suppress_until_human", None)

    if label == "abuse":
        mem["abuse_count"] = mem.get("abuse_count", 0) + 1
        if mem["abuse_count"] >= 2:
            return _end(state, "Repeated hostility — exiting politely without further messages.")
        return _send(state,
                     H("Sorry if these messages have been a bother. I'll keep it to things that directly help your listing. Reply STOP anytime and I won't message again.",
                       "Sorry agar messages se pareshani hui. Sirf wahi bhejungi jo seedha aapki listing mein madad kare. Kabhi bhi STOP reply karein, phir message nahi aayega."),
                     "Hostile tone without explicit opt-out: de-escalate once, give an explicit STOP path.", "binary_yes_stop")

    if label == "off_topic":
        return _send(state,
                     H(f"That one's outside what I can help with — a CA/specialist is the right person for it. What I can do today is {_topic(ctx)[0]} for {ctx.mname}. Reply YES and I'll take it forward.",
                       f"Yeh mere scope se bahar hai — iske liye CA/specialist sahi rahenge. Main aaj {ctx.mname} ke liye {_topic(ctx)[1]} kar sakti hoon. Reply YES, main aage badhati hoon."),
                     "Off-topic request: honest boundary, no fake help, redirect to the mission with one CTA.")

    if label == "commit":
        steps = state.get("action_steps", 0)
        state["mode"] = "action"
        state["action_steps"] = steps + 1
        if steps == 1:  # artifact already delivered -> this is the final go-ahead
            return _send(state,
                         H("Done — it's live. I'll check the numbers in 7 days and send you a 2-line result here. Nothing else needed from you.",
                           "Ho gaya — live hai. 7 din baad numbers check karke yahin 2-line result bhejungi. Aapko aur kuch nahi karna."),
                         "Second confirmation after artifact delivery: execute and close the loop with a follow-up promise.", "none")
        if steps >= 2:
            return _end(state, "Task completed and confirmed; closing the thread cleanly.")
        return _send(state, _artifact(ctx, hing, text),
                     "Explicit commitment detected — switched to action mode and delivered the concrete artifact (no further qualifying).",
                     "binary_yes_stop")

    if label == "later":
        secs = 86400 if re.search(r"tomorrow|kal|next week", text.lower()) else 3600
        return _wait(state, secs, f"Merchant asked for time; backing off {secs // 3600}h.")

    if ctx.c is not None and label in ("question", "engaged", "objection"):
        cr = _customer_reply(ctx, text, label, hing)
        if cr:
            return _send(state, cr, "Customer-side reply: preference/price handled on the merchant's behalf using only real offers.", "open_ended")

    if label in ("question", "engaged"):
        smart = _llm_reply(ctx, state, text)
        if smart:
            return _send(state, smart,
                         f"Open-ended {label}: LLM reply constrained to the context fact sheet and passed the number-grounding validator.",
                         "open_ended")

    if label == "question":
        return _send(state, _answer_question(ctx, text, hing),
                     "Merchant asked a question — answered from context only, then restated the single CTA.", "open_ended")

    if label == "objection":
        return _send(state,
                     H("Fair point. No cost and no commitment on this one — I draft, you decide. If it doesn't help, reply STOP and I'll drop it.",
                       "Sahi baat. Isme koi cost ya commitment nahi — main draft karti hoon, faisla aapka. Kaam ka na lage toh STOP reply kar dijiye."),
                     "Objection handled once with risk-reversal; keeps single binary CTA.")

    if label == "soft_no":
        return _end(state, "Merchant declined; exiting gracefully without pushing.")

    # engaged / free text
    vera_turns = sum(1 for t in turns if t.get("from") == "vera")
    if vera_turns >= 4:
        return _end(state, "Conversation has run 4+ bot turns without commitment; closing to avoid fatigue.")
    fact = _key_fact(ctx)
    return _send(state,
                 H("Got it, thanks." + (f" The number that matters here: {fact}" if fact else "")
                   + " Next step is quick: I prepare it, you approve with one reply. Reply YES to go ahead.",
                   "Samajh gayi, shukriya." + (f" Yahan kaam ka number: {fact}" if fact else "")
                   + " Agla step simple hai: main ready karti hoon, aap ek reply se approve. Reply YES."),
                 "Engaged reply without explicit commitment — acknowledge, re-anchor on the key verifiable fact, advance with a single binary CTA.")
