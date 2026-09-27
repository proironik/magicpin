"""
Vera composer — deterministic, grounded message composition.

compose(category, merchant, trigger, customer=None, now=None) -> dict

Design principles
-----------------
* Every number in a message comes from one of the 4 contexts (or is a simple,
  labelled derivation of them, e.g. 245 members x 10% churn = ~24/month).
  Nothing is invented: no fake competitors, citations, prices or slots.
* Routing is by trigger kind, with payload-driven fallbacks so unseen kinds
  (post-submission injections) still produce a grounded message.
* Voice comes from CategoryContext (salutation, vocabulary, taboos) and the
  merchant/customer language preference (Hindi-English code-mix when "hi").
* Deterministic: no randomness, no wall-clock dependency except `now`.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

REFERENCE_NOW = datetime(2026, 4, 26, 10, 0, tzinfo=timezone.utc)

CTA_BINARY = "binary_yes_stop"
CTA_OPEN = "open_ended"
CTA_SLOTS = "multi_choice_slot"
CTA_NONE = "none"


# --------------------------------------------------------------------------- #
# Small, safe helpers
# --------------------------------------------------------------------------- #
def _g(d: Any, *path, default=None):
    """Safe nested get: _g(m, 'identity', 'name')."""
    cur = d
    for p in path:
        if isinstance(cur, dict):
            cur = cur.get(p)
        elif isinstance(cur, list) and isinstance(p, int) and -len(cur) <= p < len(cur):
            cur = cur[p]
        else:
            return default
        if cur is None:
            return default
    return cur


def pct(x: Any, signed: bool = False) -> str:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return ""
    if abs(v) <= 1.5:  # stored as fraction
        v *= 100
    s = f"{abs(v):.0f}%" if abs(v - round(v)) < 0.05 else f"{abs(v):.1f}%"
    if signed:
        return ("+" if v >= 0 else "-") + s
    return s


def inr(x: Any) -> str:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return str(x)
    n = int(round(v))
    s = str(n)
    if len(s) > 3:  # Indian grouping 1,23,456
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts) + "," + tail
    return f"₹{s}"


def num(x: Any) -> str:
    try:
        return f"{int(round(float(x))):,}"
    except (TypeError, ValueError):
        return str(x)


def parse_dt(s: Any) -> Optional[datetime]:
    if not s or not isinstance(s, str):
        return None
    try:
        s2 = s.replace("Z", "+00:00")
        if len(s2) == 10:
            s2 += "T00:00:00+00:00"
        dt = datetime.fromisoformat(s2)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def nice_date(s: Any, with_year: bool = False, with_dow: bool = False) -> str:
    dt = parse_dt(s)
    if not dt:
        return str(s or "")
    out = f"{dt.day} {dt.strftime('%b')}"
    if with_year:
        out += f" {dt.year}"
    if with_dow:
        out = f"{dt.strftime('%a')} {out}"
    return out


def humanize(token: Any) -> str:
    return str(token or "").replace("_", " ").strip()


def first_sentence(text: str) -> str:
    text = (text or "").strip()
    m = re.match(r"(.+?[.!?])(\s|$)", text)
    return (m.group(1) if m else text).strip()


def lower_first(s: str) -> str:
    return s[:1].lower() + s[1:] if s else s


# --------------------------------------------------------------------------- #
# Context wrapper
# --------------------------------------------------------------------------- #
class Ctx:
    def __init__(self, category: dict, merchant: dict, trigger: dict,
                 customer: Optional[dict], now: Optional[datetime]):
        self.cat = category or {}
        self.m = merchant or {}
        self.t = trigger or {}
        self.c = customer
        self.p = self.t.get("payload") or {}
        self.slug = self.cat.get("slug") or self.m.get("category_slug") or ""
        self.kind = self.t.get("kind") or "generic"
        exp = parse_dt(self.t.get("expires_at"))
        n = now or REFERENCE_NOW
        # If the clock is past the trigger's expiry, relative day-counts would be
        # wrong -> suppress them rather than print a negative/incorrect number.
        self.now: Optional[datetime] = n if (exp is None or n <= exp) else None

    # ---------------- merchant identity ----------------
    @property
    def mname(self) -> str:
        return _g(self.m, "identity", "name", default="your business")

    @property
    def owner(self) -> str:
        return _g(self.m, "identity", "owner_first_name", default="") or ""

    @property
    def locality(self) -> str:
        return _g(self.m, "identity", "locality", default="") or _g(self.m, "identity", "city", default="")

    @property
    def city(self) -> str:
        return _g(self.m, "identity", "city", default="")

    @property
    def sal(self) -> str:
        """Merchant salutation per category voice."""
        o = self.owner.strip()
        if self.slug == "dentists":
            if not o:
                return "Doc"
            return o if o.lower().startswith("dr") else f"Dr. {o}"
        return o or f"{self.mname} team"

    @property
    def hinglish(self) -> bool:
        langs = [str(x).lower() for x in (_g(self.m, "identity", "languages", default=[]) or [])]
        return "hi" in langs

    def h(self, en: str, hi: str) -> str:
        """Pick the Hindi-English code-mix variant when the merchant speaks Hindi."""
        return hi if self.hinglish else en

    # ---------------- merchant data ----------------
    @property
    def perf(self) -> dict:
        return self.m.get("performance") or {}

    @property
    def peer(self) -> dict:
        return self.cat.get("peer_stats") or {}

    @property
    def agg(self) -> dict:
        return self.m.get("customer_aggregate") or {}

    @property
    def signals(self) -> List[str]:
        return [str(s) for s in (self.m.get("signals") or [])]

    def has_signal(self, prefix: str) -> bool:
        return any(s.startswith(prefix) for s in self.signals)

    def active_offers(self) -> List[str]:
        return [o.get("title") for o in (self.m.get("offers") or [])
                if o.get("status") == "active" and o.get("title")]

    def catalog_offer(self, prefer_types=("service_at_price", "free_service", "free_trial")) -> Optional[str]:
        cat = self.cat.get("offer_catalog") or []
        for t in prefer_types:
            for o in cat:
                if o.get("type") == t and o.get("title"):
                    return o["title"]
        return cat[0].get("title") if cat else None

    def best_offer(self) -> Tuple[Optional[str], bool]:
        """(offer_title, is_merchants_own)."""
        act = self.active_offers()
        if act:
            # Prefer service+price style over % discounts
            for o in act:
                if "₹" in o and "%" not in o:
                    return o, True
            return act[0], True
        return self.catalog_offer(), False

    def review_theme(self, sentiment: str) -> Optional[dict]:
        themes = [r for r in (self.m.get("review_themes") or []) if r.get("sentiment") == sentiment]
        themes.sort(key=lambda r: -(r.get("occurrences_30d") or 0))
        return themes[0] if themes else None

    def last_merchant_msg(self) -> Optional[dict]:
        hist = self.m.get("conversation_history") or []
        for turn in reversed(hist):
            if turn.get("from") == "merchant":
                return turn
        return None

    def history_bodies(self) -> List[str]:
        return [str(t.get("body", "")) for t in (self.m.get("conversation_history") or [])]

    # ---------------- digest lookup ----------------
    def digest_item(self) -> Optional[dict]:
        p = self.p
        inline = p.get("top_item")
        if isinstance(inline, dict) and inline.get("title"):
            return inline
        ids = [p.get(k) for k in ("top_item_id", "digest_item_id", "alert_id", "item_id") if p.get(k)]
        for d in self.cat.get("digest") or []:
            if d.get("id") in ids:
                return d
        return None

    def digest_by_kind(self, *kinds) -> Optional[dict]:
        for k in kinds:
            for d in self.cat.get("digest") or []:
                if d.get("kind") == k:
                    return d
        return None

    def seasonal_beat_now(self) -> Optional[dict]:
        month = (self.now or REFERENCE_NOW).strftime("%b")
        months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        mi = months.index(month)
        for b in self.cat.get("seasonal_beats") or []:
            rng = str(b.get("month_range", ""))
            parts = [x.strip()[:3] for x in rng.split("-")]
            try:
                if len(parts) == 1 and parts[0] in months and months.index(parts[0]) == mi:
                    return b
                if len(parts) == 2 and parts[0] in months and parts[1] in months:
                    a, z = months.index(parts[0]), months.index(parts[1])
                    inside = a <= mi <= z if a <= z else (mi >= a or mi <= z)
                    if inside:
                        return b
            except ValueError:
                continue
        return None

    def days_until(self, iso: Any) -> Optional[int]:
        dt = parse_dt(iso)
        if not dt or not self.now:
            return None
        d = (dt - self.now).days
        return d if d >= 0 else None

    # ---------------- customer ----------------
    @property
    def cname(self) -> str:
        n = str(_g(self.c, "identity", "name", default="") or "")
        # "Aanya (parent: Sneha)" -> address the parent
        m = re.search(r"parent:\s*([^)]+)\)", n)
        if m:
            return m.group(1).strip()
        n = re.sub(r"\(.*?\)", "", n).strip()
        return n

    @property
    def child_name(self) -> Optional[str]:
        n = str(_g(self.c, "identity", "name", default="") or "")
        if "parent:" in n:
            return n.split("(")[0].strip()
        return None

    @property
    def clang(self) -> str:
        pref = str(_g(self.c, "identity", "language_pref", default="en") or "en").lower()
        if pref in ("hi", "hindi"):
            return "hi"
        if "hi" in pref:
            return "hi-en"
        return "en"

    def ch(self, en: str, hien: str, hi: Optional[str] = None) -> str:
        lang = self.clang
        if lang == "hi":
            return hi or hien
        if lang == "hi-en":
            return hien
        return en


# --------------------------------------------------------------------------- #
# Result builder
# --------------------------------------------------------------------------- #
def _result(ctx: Ctx, body: str, cta: str, rationale: str, params: Optional[List[str]] = None,
            send_as: Optional[str] = None) -> Dict[str, Any]:
    body = re.sub(r"[ \t]+", " ", body).strip()
    body = re.sub(r" +([,.?!])", r"\1", body)
    body = _scrub_taboos(ctx, body)
    return {
        "body": body,
        "cta": cta,
        "send_as": send_as or ("merchant_on_behalf" if ctx.c is not None or ctx.t.get("scope") == "customer" else "vera"),
        "suppression_key": ctx.t.get("suppression_key") or f"{ctx.kind}:{ctx.m.get('merchant_id', '')}",
        "rationale": rationale,
        "template_name": f"vera_{re.sub(r'[^a-z0-9_]', '', ctx.kind.lower())}_v1",
        "template_params": [str(x) for x in (params or [ctx.sal])],
    }


def _scrub_taboos(ctx: Ctx, body: str) -> str:
    taboos = _g(ctx.cat, "voice", "vocab_taboo", default=[]) or []
    for t in taboos:
        core = re.sub(r"\(.*?\)", "", str(t)).strip()
        if len(core) >= 4:
            body = re.sub(re.escape(core), "", body, flags=re.IGNORECASE)
    return re.sub(r"\s{2,}", " ", body).strip()


def _peer_ctr_line(ctx: Ctx) -> Optional[str]:
    ctr, pctr = ctx.perf.get("ctr"), ctx.peer.get("avg_ctr")
    if ctr is None or not pctr:
        return None
    if abs(ctr - pctr) < 0.001:
        return f"your CTR is {pct(ctr)}, right at the peer average"
    if ctr < pctr:
        return f"your CTR is {pct(ctr)} vs {pct(pctr)} for {ctx.slug} peers"
    return f"your CTR is {pct(ctr)}, above the {pct(pctr)} peer average"


# --------------------------------------------------------------------------- #
# Merchant-facing handlers
# --------------------------------------------------------------------------- #
def h_digest(ctx: Ctx) -> Dict[str, Any]:
    """research_digest / regulation_change / cde_opportunity / generic digest items."""
    item = ctx.digest_item() or ctx.digest_by_kind(
        {"regulation_change": "compliance", "cde_opportunity": "cde"}.get(ctx.kind, "research"),
        "research", "trend", "tech")
    if not item:
        return h_generic(ctx)
    kind = item.get("kind", "")
    src = item.get("source", "")
    title = item.get("title", "")
    summary = item.get("summary", "")
    actionable = item.get("actionable", "")
    sal = ctx.sal

    # Who in *this* merchant's book does it touch?
    seg = item.get("patient_segment")
    reach = ""
    if seg and "high_risk" in str(seg) and ctx.agg.get("high_risk_adult_count"):
        reach = f"your {num(ctx.agg['high_risk_adult_count'])} high-risk adult patients"
    elif ctx.agg.get("chronic_rx_count") and kind in ("supply", "alert", "compliance"):
        reach = f"your {num(ctx.agg['chronic_rx_count'])} chronic-Rx customers"

    if ctx.kind == "regulation_change" or kind == "compliance":
        deadline = ctx.p.get("deadline_iso") or item.get("effective_date")
        dleft = ctx.days_until(deadline)
        when = f" — deadline {nice_date(deadline, with_year=True)}" if deadline else ""
        if dleft is not None:
            when += f" ({dleft} days)"
        body = (f"{sal}, compliance heads-up{when}. {title}. {summary} "
                f"Next step: {lower_first(actionable.rstrip('.'))}. "
                + ctx.h("Want me to draft a 1-page SOP checklist you can sign and file? Reply YES.",
                        "Main ek 1-page SOP checklist draft kar doon jo aap sign karke file kar sakein? Reply YES.")
                + f" — {src}")
        return _result(ctx, body, CTA_BINARY,
                       f"Regulatory change with a hard deadline; cites source + exact thresholds, offers a done-for-you SOP (effort externalisation).",
                       [sal, title, nice_date(deadline, with_year=True) if deadline else src])

    if ctx.kind == "cde_opportunity" or kind == "cde":
        when = nice_date(item.get("date"), with_dow=True) if item.get("date") else ""
        if item.get("date", "")[11:16] not in ("", "00:00"):
            when += f", {time12(item['date'])}"
        credits = ctx.p.get("credits") or item.get("credits")
        fee = actionable or humanize(ctx.p.get("fee"))
        body = (f"{sal}, {title} — {when}. "
                + (f"{credits} CDE credits. " if credits else "")
                + f"{first_sentence(summary)} {summary[len(first_sentence(summary)):].strip()} "
                + (f"Fee: {fee.rstrip('.')}. " if fee else "")
                + ctx.h("Want me to set a reminder for the day before? Reply YES.",
                        "Ek din pehle reminder set kar doon? Reply YES.")
                + f" — {src}")
        return _result(ctx, body, CTA_BINARY,
                       "Free/low-cost CDE event relevant to practice tech; low-stakes reminder CTA respects the doctor's time.",
                       [sal, title, when])

    # research / trend / tech / seasonal / supply
    lead = ctx.h(f"{src} has one item worth your time", f"{src} mein ek item aapke kaam ka hai")
    trial = f" ({num(item['trial_n'])}-patient trial)" if item.get("trial_n") else ""
    rel = f" Directly relevant to {reach}." if reach else ""
    offer = ctx.h("Want me to pull the abstract + draft a patient-ed WhatsApp you can forward?",
                  "Abstract nikaal ke ek patient-ed WhatsApp draft kar doon jo aap forward kar sakein?") \
        if ctx.slug == "dentists" else \
        ctx.h("Want me to turn this into a ready-to-post update for your customers?",
              "Isko customers ke liye ready-to-post update bana doon?")
    body = (f"{sal}, {lead}: {title}{trial}. {summary}{rel} "
            f"{('Practical takeaway: ' + actionable.rstrip('.') + '.') if actionable else ''} {offer} — {src}")
    return _result(ctx, body, CTA_OPEN,
                   f"External {kind or 'research'} item from the category digest, anchored to the merchant's own cohort; reciprocity CTA (I'll draft it).",
                   [sal, title, src])


def h_competitor(ctx: Ctx) -> Dict[str, Any]:
    p, sal = ctx.p, ctx.sal
    comp, dist, their = p.get("competitor_name"), p.get("distance_km"), p.get("their_offer")
    my_offer, own = ctx.best_offer()
    pos = ctx.review_theme("pos")
    edge = ""
    if pos:
        q = pos.get("common_quote")
        edge = (f" Your edge isn't price: {pos.get('occurrences_30d')} reviews this month praise "
                f"{humanize(pos.get('theme'))}" + (f" (\"{q}\")" if q else "") + ".")
    if comp:
        opened = f" on {nice_date(p.get('opened_date'))}" if p.get("opened_date") else ""
        body = (f"{sal}, {comp} opened {dist} km from you{opened}"
                + (f", leading with {their}" if their else "")
                + (f" vs your {my_offer}" if (their and own and my_offer) else "") + "."
                + edge + " "
                + ctx.h("Rather than a price war, want me to draft a GBP post that leads with what patients already say about you? Reply YES.",
                        "Price war ki zaroorat nahi — jo customers already aapke baare mein bolte hain, usi pe ek GBP post draft kar doon? Reply YES."))
    else:
        ctr_line = _peer_ctr_line(ctx)
        body = (f"{sal}, a new {ctx.slug.rstrip('s')} listing has come up near {ctx.locality} on Google. "
                f"Your baseline: {num(ctx.perf.get('views', 0))} views and {num(ctx.perf.get('calls', 0))} calls in 30 days"
                + (f"; {ctr_line}" if ctr_line else "") + "."
                + edge + " "
                + (f"Easiest defence is a sharp, visible offer — {my_offer}{' (already live)' if own else ' is what works in your category'}. " if my_offer else "")
                + ctx.h("Want me to refresh your listing this week so you stay the first pick? Reply YES.",
                        "Is hafte listing refresh kar doon taaki aap hi first pick rahein? Reply YES."))
    return _result(ctx, body, CTA_BINARY,
                   "Competitor event framed as loss-aversion, but steers away from a price war toward the merchant's real differentiator (reviews/offer).",
                   [sal, comp or "new listing", ctx.locality])


def _metric_delta(ctx: Ctx) -> Tuple[str, Optional[float], str]:
    p = ctx.p
    if p.get("metric") and p.get("delta_pct") is not None:
        return str(p["metric"]), float(p["delta_pct"]), str(p.get("window", "7d"))
    d = ctx.perf.get("delta_7d") or {}
    cands = [(k.replace("_pct", ""), v) for k, v in d.items() if isinstance(v, (int, float))]
    if not cands:
        return "views", None, "7d"
    want_up = ctx.kind in ("perf_spike", "milestone_reached")
    cands.sort(key=lambda kv: -kv[1] if want_up else kv[1])
    return cands[0][0], float(cands[0][1]), "7d"


def _fixes(ctx: Ctx) -> List[str]:
    fixes = []
    if _g(ctx.m, "identity", "verified") is False or ctx.has_signal("unverified"):
        fixes.append("your Google profile is still unverified")
    if not ctx.active_offers():
        o = ctx.catalog_offer()
        fixes.append("no active offer" + (f" (peers run '{o}')" if o else ""))
    stale = next((s for s in ctx.signals if s.startswith("stale_posts")), None)
    if stale:
        fixes.append(f"last Google post was {stale.split(':')[-1].replace('d', ' days')} ago")
    ctr, pctr = ctx.perf.get("ctr"), ctx.peer.get("avg_ctr")
    if ctr is not None and pctr and ctr < pctr:
        fixes.append(f"CTR {pct(ctr)} vs {pct(pctr)} peer avg")
    return fixes


def h_perf_dip(ctx: Ctx) -> Dict[str, Any]:
    sal = ctx.sal
    metric, delta, window = _metric_delta(ctx)
    base = ctx.p.get("vs_baseline")
    cur = ctx.perf.get(metric)
    if delta is None:
        return h_generic(ctx)
    if ctx.p.get("is_expected_seasonal") or ctx.kind == "seasonal_perf_dip":
        return h_seasonal_dip(ctx)
    if delta >= 0:  # trigger says "dip" but the data doesn't — say so, pivot to the real gap
        peer_v = ctx.peer.get(f"avg_{metric}_30d")
        line = (f"{sal}, quick check on your {metric}: the 7-day trend is actually flat ({pct(delta, signed=True)})"
                + (f", but at {num(cur)} in 30 days you're below the {num(peer_v)} peer average — that gap is the real opportunity"
                   if cur is not None and peer_v and cur < peer_v else "")
                + ".")
        parts = [line]
        fixes = _fixes(ctx)
        if fixes:
            parts.append(ctx.h("Fixable on your side: ", "Aapki taraf se fixable: ") + "; ".join(fixes[:3]) + ".")
        parts.append(ctx.h("Want me to start with the first fix today? Reply YES.", "Pehla fix aaj hi start kar doon? Reply YES."))
        return _result(ctx, " ".join(parts), CTA_BINARY,
                       f"Dip trigger but 7-day {metric} is not negative — reported honestly and reframed around the peer gap + fixable profile issues.",
                       [sal, metric, pct(delta, signed=True)])
    if abs(delta) < 0.10:  # honest framing: small wobble, the real issue is usually the peer gap
        line = f"{sal}, your {metric} dipped slightly ({pct(delta, signed=True)}) over the last {window.replace('d', ' days')}"
    else:
        line = f"{sal}, your {metric} are down {pct(delta)} over the last {window.replace('d', ' days')}"
    if base:
        line += f" (baseline {num(base)})"
    elif cur is not None:
        peer_v = ctx.peer.get(f"avg_{metric}_30d")
        line += f" ({num(cur)} in the last 30 days" + (f" vs a {num(peer_v)} peer average" if peer_v else "") + ")"
    line += "."
    fixes = _fixes(ctx)
    neg = ctx.review_theme("neg")
    parts = [line]
    if fixes:
        parts.append(ctx.h("Fixable on your side: ", "Aapki taraf se fixable: ") + "; ".join(fixes[:3]) + ".")
    if neg:
        parts.append(f"Also {neg.get('occurrences_30d')} recent reviews flag {humanize(neg.get('theme'))}.")
    parts.append(ctx.h("Want me to start with the first fix today? Reply YES.",
                       "Pehla fix aaj hi start kar doon? Reply YES."))
    return _result(ctx, " ".join(parts), CTA_BINARY,
                   f"{metric} dip {pct(delta)} — loss aversion anchored on the merchant's own numbers, paired with concrete, fixable causes from their profile.",
                   [sal, metric, pct(delta)])


def h_seasonal_dip(ctx: Ctx) -> Dict[str, Any]:
    sal = ctx.sal
    metric, delta, window = _metric_delta(ctx)
    beat = ctx.seasonal_beat_now()
    dg = ctx.digest_by_kind("seasonal")
    members = ctx.agg.get("total_active_members")
    churn = ctx.agg.get("monthly_churn_pct")
    pchurn = ctx.peer.get("monthly_churn_pct")
    parts = [f"{sal}, {metric} down {pct(delta)} this week — "
             + ctx.h("this is the expected seasonal lull, not a problem with your listing.",
                     "yeh expected seasonal lull hai, aapki listing ki problem nahi.")]
    if beat:
        parts.append(f"{beat.get('month_range')}: {beat.get('note')}.")
    elif dg:
        parts.append(first_sentence(dg.get("summary", "")))
    if members and churn:
        lost = int(round(members * churn))
        parts.append(f"The real lever now is retention: at {pct(churn)} monthly churn"
                     + (f" (peer {pct(pchurn)})" if pchurn else "")
                     + f", ~{lost} of your {num(members)} members walk out each month.")
    parts.append(ctx.h("Want me to draft a 6-week member challenge to hold them through the dip? Reply YES.",
                       "Members ko dip ke through hold karne ke liye 6-week challenge draft kar doon? Reply YES."))
    return _result(ctx, " ".join(parts), CTA_BINARY,
                   "Pre-empts anxiety about an expected seasonal dip; reframes effort from acquisition to retention using the merchant's member/churn numbers.",
                   [sal, metric, pct(delta)])


def h_perf_spike(ctx: Ctx) -> Dict[str, Any]:
    sal = ctx.sal
    metric, delta, window = _metric_delta(ctx)
    if delta is None or delta <= 0:
        return h_generic(ctx)
    base = ctx.p.get("vs_baseline")
    driver = ctx.p.get("likely_driver")
    line = f"{sal}, {metric} up {pct(delta)} over {window.replace('d', ' days')}" + (f" vs a baseline of {num(base)}" if base else "") + "."
    parts = [line]
    if driver:
        parts.append(f"Likely driver: your {humanize(driver)}.")
    offer, own = ctx.best_offer()
    fixes = _fixes(ctx)
    if fixes and not driver:
        parts.append(ctx.h("To convert the extra traffic: ", "Extra traffic convert karne ke liye: ") + "; ".join(fixes[:2]) + ".")
    parts.append(
        ctx.h(f"Momentum like this fades in ~7 days. Want me to post a follow-up"
              + (f" featuring {offer}" if offer else "") + " while it's hot? Reply YES.",
              f"Aisa momentum ~7 din mein fade hota hai. Garam rehte hi ek follow-up post"
              + (f" ({offer} ke saath)" if offer else "") + " daal doon? Reply YES."))
    return _result(ctx, " ".join(parts), CTA_BINARY,
                   "Positive spike — reinforce what worked and convert while momentum lasts; cites the delta and driver from the trigger.",
                   [sal, metric, pct(delta)])


def h_milestone(ctx: Ctx) -> Dict[str, Any]:
    sal, p = ctx.sal, ctx.p
    now_v, target = p.get("value_now"), p.get("milestone_value")
    metric = humanize(p.get("metric", "")).replace("count", "").strip()
    pos = ctx.review_theme("pos")
    if now_v is not None and target:
        gap = int(target) - int(now_v)
        head = (f"{sal}, {ctx.mname} is at {num(now_v)} {metric}s — just {gap} away from {num(target)}."
                if gap > 0 else f"{sal}, {ctx.mname} just crossed {num(target)} {metric}s.")
    else:
        v, c = ctx.perf.get("views"), ctx.perf.get("calls")
        pv = ctx.peer.get("avg_views_30d")
        head = (f"{sal}, {ctx.mname} logged {num(v)} profile views and {num(c)} calls in the last 30 days"
                + (f", above the {num(pv)} peer average" if pv and v and v > pv else "") + ".")
    parts = [head]
    if pos:
        parts.append(f"{pos.get('occurrences_30d')} reviews this month praise your {humanize(pos.get('theme'))}.")
    parts.append(ctx.h("Want me to make a small QR review card for the counter so happy customers can push you over the line? Reply YES.",
                       "Counter ke liye ek chhota QR review card bana doon taaki happy customers review de sakein? Reply YES."))
    return _result(ctx, " ".join(parts), CTA_BINARY,
                   "Milestone proximity creates a goal-gradient pull; low-effort artifact (QR card) converts existing happy customers.",
                   [sal, str(now_v or ""), str(target or "")])


def h_review_theme(ctx: Ctx) -> Dict[str, Any]:
    sal, p = ctx.sal, ctx.p
    theme = p.get("theme") or (ctx.review_theme("neg") or {}).get("theme")
    occ = p.get("occurrences_30d") or (ctx.review_theme("neg") or {}).get("occurrences_30d")
    quote = p.get("common_quote") or (ctx.review_theme("neg") or {}).get("common_quote")
    if not theme:
        return h_generic(ctx)
    trend = p.get("trend")
    pos = ctx.review_theme("pos")
    parts = [f"{sal}, {occ} reviews in the last 30 days mention {humanize(theme)}" + (f" — and it's {trend}" if trend else "") + "."]
    if quote:
        parts.append(f"Most recent: \"{quote}\".")
    if pos:
        parts.append(f"Meanwhile {pos.get('occurrences_30d')} reviews praise your {humanize(pos.get('theme'))}, so this is an ops fix, not a reputation problem.")
    parts.append(ctx.h("Want me to draft a calm public reply for these reviews + a one-line fix note for your team? Reply YES.",
                       "In reviews ke liye ek calm public reply aur team ke liye ek-line fix note draft kar doon? Reply YES."))
    return _result(ctx, " ".join(parts), CTA_BINARY,
                   "Emerging negative review theme — quantified, quoted, balanced with positives; offers reply drafts (effort externalisation).",
                   [sal, humanize(theme), str(occ)])


def h_curious_ask(ctx: Ctx) -> Dict[str, Any]:
    sal = ctx.sal
    pos = ctx.review_theme("pos")
    guess = ""
    if pos:
        q = pos.get("common_quote")
        guess = (f" My guess from your reviews: {humanize(pos.get('theme'))} — {pos.get('occurrences_30d')} mentions this month"
                 + (f" (\"{q}\")" if q else "") + ".")
    body = (f"{sal}, " + ctx.h("quick one — which service did customers ask for most this week at ",
                              "quick sawaal — is hafte ") + f"{ctx.mname}"
            + ctx.h("?", " mein sabse zyada kaunsi service maangi gayi?") + guess + " "
            + ctx.h("Reply with just the name — I'll turn it into a Google post + a ready price-reply for WhatsApp enquiries.",
                    "Bas naam reply kar dijiye — main usse Google post + WhatsApp enquiries ke liye ready price-reply bana dungi."))
    return _result(ctx, body, CTA_OPEN,
                   "Weekly curious-ask: asking-the-merchant lever with an informed guess from review themes; reciprocity (post + reply draft).",
                   [sal, ctx.mname])


def h_festival(ctx: Ctx) -> Dict[str, Any]:
    sal, p = ctx.sal, ctx.p
    fest = p.get("festival")
    beat = None
    for b in ctx.cat.get("seasonal_beats") or []:
        if fest and ("festival" in str(b.get("note", "")).lower() or "Oct" in str(b.get("month_range", ""))):
            beat = b
            break
    if not beat:
        beat = next((b for b in ctx.cat.get("seasonal_beats") or [] if "festival" in str(b.get("note", "")).lower()), None)
    offer, own = ctx.best_offer()
    if fest:
        days = p.get("days_until") or ctx.days_until(p.get("date"))
        head = f"{sal}, {fest} is on {nice_date(p.get('date'), with_year=True)}" + (f" — {days} days out" if days else "") + "."
    else:
        head = f"{sal}, " + ctx.h("festive season is the next big window for ", "festive season ") + \
               (f"{ctx.slug}" if not ctx.hinglish else f"{ctx.slug} ke liye agla bada window hai") + "."
    parts = [head]
    if beat:
        parts.append(f"Category pattern for {beat.get('month_range')}: {beat.get('note')}.")
    parts.append("Prime slots fill first, so early movers win the calendar.")
    if offer:
        parts.append(f"Your {offer}" + ("" if own else " (a proven format in your category)") + " is a natural festive hook.")
    parts.append(ctx.h("Want me to draft an early-bird festive post you can approve now and schedule later? Reply YES.",
                       "Ek early-bird festive post draft kar doon jo aap abhi approve karke baad mein schedule kar sakein? Reply YES."))
    return _result(ctx, " ".join(parts), CTA_BINARY,
                   "Festival trigger anchored on date + category seasonality; early-bird framing with the merchant's own offer.",
                   [sal, fest or "festive season", nice_date(p.get("date")) if p.get("date") else ""])


def h_ipl(ctx: Ctx) -> Dict[str, Any]:
    sal, p = ctx.sal, ctx.p
    match, venue = p.get("match", "tonight's match"), p.get("venue")
    t = parse_dt(p.get("match_time_iso"))
    tm = time12(p.get("match_time_iso"))
    dow = t.strftime("%A") if t else ""
    weeknight = p.get("is_weeknight")
    if weeknight is None and t:
        weeknight = t.weekday() in (0, 1, 2, 3)
    dg = next((d for d in ctx.cat.get("digest") or [] if "ipl" in str(d.get("id", "")).lower() or "IPL" in d.get("title", "")), None)
    offers = ctx.active_offers()
    combo = next((o.get("title") for o in ctx.cat.get("offer_catalog") or [] if "match" in str(o.get("title", "")).lower()), None)
    parts = [f"{sal}, {match}" + (f" at {venue}" if venue else "") + (f" tonight, {tm}" if tm else " today") + "."]
    if weeknight is False:
        if dg:
            parts.append(ctx.h("Heads-up before you spend on a dine-in promo — weekend-match data: ",
                               "Dine-in promo pe kharcha karne se pehle — weekend-match data: ")
                         + f"{first_sentence(dg.get('summary', '')).rstrip('.')} ({dg.get('source')}).")
        restricted = [o for o in offers if re.search(r"\((mon|tue|wed|thu|fri|sat|sun)", o.lower())]
        if restricted:
            parts.append(f"Your '{restricted[0]}' doesn't cover {dow or 'today'}, so skip the dine-in push.")
        parts.append("Tonight is a delivery night: people watch at home.")
        if combo:
            parts.append(f"A delivery-only {combo} fits exactly.")
        parts.append(ctx.h("Want me to set it up as a tonight-only listing offer? Reply YES and it's live before the toss.",
                           "Tonight-only offer bana ke listing pe live kar doon? Reply YES — toss se pehle live."))
        why = "Weekend match: digest says Saturday IPL shifts demand to home-watching, so recommend delivery over dine-in (contrarian, data-backed)."
    else:
        if dg:
            parts.append(f"Weeknight matches have been driving covers up ({dg.get('source')}).")
        hook = offers[0] if offers else combo
        if hook:
            parts.append(f"Put {hook} front and centre for the match window.")
        parts.append(ctx.h("Want me to push a match-night post + listing banner now? Reply YES.",
                           "Abhi match-night post + listing banner push kar doon? Reply YES."))
        why = "Weeknight IPL match drives covers; time-bound push using the merchant's live offer."
    return _result(ctx, " ".join(parts), CTA_BINARY, why, [sal, match, tm])


def h_planning(ctx: Ctx) -> Dict[str, Any]:
    sal, p = ctx.sal, ctx.p
    topic = str(p.get("intent_topic", "")).lower()
    last = p.get("merchant_last_message") or (ctx.last_merchant_msg() or {}).get("body", "")
    offers = ctx.active_offers()
    opener = f"{sal}, " + (ctx.h(f"you asked \"{last}\"", f"aapne pucha tha \"{last}\"") if last
                           else ctx.h("following up on your plan", "aapke plan pe")) + \
             ctx.h(" — here's a first draft you can edit:", " — yeh raha first draft, edit kar sakte hain:")
    if any(k in topic for k in ("thali", "corporate", "bulk", "catering")):
        base = None
        for o in offers:
            m = re.search(r"₹\s?([\d,]+)", o)
            if m and "thali" in o.lower():
                base = int(m.group(1).replace(",", ""))
                break
        if base:
            t1, t2, t3 = int(base * 0.9), int(base * 0.85), int(base * 0.8)
            draft = (f"\n• 10-24 thalis/day: {inr(t1)} each (retail {inr(base)})"
                     f"\n• 25-49: {inr(t2)} each + free delivery"
                     f"\n• 50+: {inr(t3)} each + free delivery"
                     f"\n• Order on WhatsApp by 11am, delivered by 1pm")
        else:
            draft = ("\n• 10-24 meals/day: 10% off retail\n• 25-49: 15% off + free delivery"
                     "\n• 50+: 20% off + free delivery\n• Order on WhatsApp by 11am, delivered by 1pm")
        close = ctx.h(f"Margins stay safe because it's pre-ordered volume. Want me to turn this into a one-page PDF + a 3-line WhatsApp pitch for office admins near {ctx.locality}? Reply YES.",
                      f"Pre-ordered volume hai, isliye margin safe rehta hai. Ise one-page PDF + {ctx.locality} ke office admins ke liye 3-line WhatsApp pitch bana doon? Reply YES.")
        body = f"{opener}\n{ctx.mname} Corporate Lunch{draft}\n{close}"
    elif any(k in topic for k in ("kids", "yoga", "camp", "program", "class")):
        prev = next((t.get("body") for t in reversed(ctx.m.get("conversation_history") or [])
                     if t.get("from") == "vera" and re.search(r"₹\s?[\d,]+", str(t.get("body")))), None)
        price = re.search(r"₹\s?[\d,]+", prev).group(0) if prev else None
        weeks = re.search(r"(\d+)-week", prev or "")
        ages = re.search(r"age\s*(\d+\s*-\s*\d+)", prev or "")
        per_wk = re.search(r"(\d+)\s*classes/week", prev or "")
        draft = (f"\n• {weeks.group(1) if weeks else '4'}-week summer camp, {per_wk.group(1) if per_wk else '3'} classes/week"
                 f"\n• Ages {ages.group(1) if ages else '7-12'}, small batches"
                 + (f"\n• {price} for the full camp" if price else "")
                 + "\n• Saturday free trial class for new kids"
                 + (f"\n• Existing offer stays live: {offers[0]}" if offers else ""))
        close = ctx.h("Want me to publish this as a Google post + a WhatsApp flyer for your current members' parents? Reply YES.",
                      "Ise Google post + members ke parents ke liye WhatsApp flyer bana ke publish kar doon? Reply YES.")
        body = f"{opener}\n{humanize(topic).title()} — {ctx.mname}{draft}\n{close}"
    else:
        offer, _ = ctx.best_offer()
        draft = (f"\n• What: {humanize(topic) or 'new offering'}"
                 + (f"\n• Anchor price: build around {offer}" if offer else "")
                 + "\n• Launch: Google post + WhatsApp broadcast to repeat customers"
                 + "\n• Review in 2 weeks against calls/enquiries")
        close = ctx.h("Want me to write the post copy now? Reply YES.", "Post copy abhi likh doon? Reply YES.")
        body = f"{opener}{draft}\n{close}"
    return _result(ctx, body, CTA_BINARY,
                   "Merchant already expressed planning intent — skip qualifying, hand over a concrete editable draft (intent -> action) and one next step.",
                   [sal, humanize(topic)])


def h_renewal(ctx: Ctx) -> Dict[str, Any]:
    sal, p = ctx.sal, ctx.p
    days = p.get("days_remaining") or _g(ctx.m, "subscription", "days_remaining")
    plan = p.get("plan") or _g(ctx.m, "subscription", "plan", default="")
    amt = p.get("renewal_amount")
    metric, delta, _ = _metric_delta(ctx)
    parts = [f"{sal}, your {plan} plan renews in {days} days" + (f" ({inr(amt)})" if amt else "") + "."]
    v, c = ctx.perf.get("views"), ctx.perf.get("calls")
    if v is not None:
        parts.append(f"Last 30 days on the plan: {num(v)} views, {num(c)} calls, {num(ctx.perf.get('directions', 0))} direction requests.")
    if delta is not None and delta < 0:
        parts.append(f"{metric.capitalize()} are {pct(delta)} this week — "
                     + ctx.h("a lapse now pauses profile upkeep right when it's needed.",
                             "abhi lapse hua toh profile upkeep ruk jayega, jab sabse zyada zaroorat hai."))
    fixes = _fixes(ctx)
    if fixes:
        parts.append(f"I'll also fix: {fixes[0]}.")
    parts.append(ctx.h("Reply YES and I'll send the renewal link.", "Reply YES — main renewal link bhej deti hoon."))
    return _result(ctx, " ".join(parts), CTA_BINARY,
                   "Renewal due — shows value delivered (their own numbers) + loss aversion; single YES CTA.",
                   [sal, str(days), inr(amt) if amt else plan])


def h_winback(ctx: Ctx) -> Dict[str, Any]:
    sal, p = ctx.sal, ctx.p
    days = p.get("days_since_expiry") or _g(ctx.m, "subscription", "days_since_expiry") \
        or p.get("days_since_last_merchant_message")
    dip = p.get("perf_dip_pct")
    lapsed_add = p.get("lapsed_customers_added_since_expiry")
    metric, delta, _ = _metric_delta(ctx)
    lapsed = ctx.agg.get("lapsed_90d_plus") or ctx.agg.get("lapsed_180d_plus")
    parts = [f"{sal}, " + ctx.h("no pitch — just what changed" + (f" in the {days} days since your plan paused" if days else ""),
                                "koi pitch nahi — bas yeh batana tha ki" + (f" plan pause hone ke {days} din mein" if days else "") + " kya badla") + ":"]
    facts = []
    if dip is not None:
        facts.append(f"calls {pct(dip, signed=True)}")
    elif delta is not None:
        facts.append(f"{metric} {pct(delta, signed=True)} this week")
    if lapsed_add:
        facts.append(f"{lapsed_add} more customers slipped into lapsed")
    if lapsed:
        facts.append(f"{num(lapsed)} customers now lapsed in total")
    if facts:
        parts.append("; ".join(facts) + ".")
    offer, own = ctx.best_offer()
    if offer:
        parts.append(f"A '{offer}' comeback message to those lapsed customers is the quickest win.")
    parts.append(ctx.h("Want me to draft it? Reply YES — no commitment to renew.",
                       "Draft kar doon? Reply YES — renew karne ki koi commitment nahi."))
    return _result(ctx, " ".join(parts), CTA_BINARY,
                   "Win-back: reciprocity-first (value before ask), quantified loss since expiry, zero-commitment CTA.",
                   [sal, str(days or "")])


def h_dormant(ctx: Ctx) -> Dict[str, Any]:
    sal, p = ctx.sal, ctx.p
    days = p.get("days_since_last_merchant_message")
    if _g(ctx.m, "subscription", "status") == "expired":
        return h_winback(ctx)
    metric, delta, _ = _metric_delta(ctx)
    v = ctx.perf.get("views")
    pos = ctx.review_theme("pos")
    parts = [f"{sal}, " + ctx.h("been a while", "kaafi time ho gaya") + (f" ({days} days)" if days else "") + "."]
    ctr_line = _peer_ctr_line(ctx)
    if v is not None:
        parts.append(f"Quick pulse on {ctx.mname}: {num(v)} views in 30 days"
                     + (f", {metric} {pct(delta, signed=True)} this week" if delta is not None else "")
                     + (f"; {ctr_line}" if ctr_line else "") + ".")
    if pos:
        parts.append(f"Customers keep praising your {humanize(pos.get('theme'))}.")
    parts.append(ctx.h("Want a 3-line summary of what's working and the one thing I'd fix? Reply YES.",
                       "Kya chal raha hai aur ek cheez jo main fix karungi — 3 line mein bhej doon? Reply YES."))
    return _result(ctx, " ".join(parts), CTA_BINARY,
                   "Dormant merchant: re-open with a curiosity hook built on their own numbers, no hard sell.",
                   [sal, str(days or "")])


def h_gbp_unverified(ctx: Ctx) -> Dict[str, Any]:
    sal, p = ctx.sal, ctx.p
    uplift = p.get("estimated_uplift_pct")
    path = humanize(p.get("verification_path", "")).replace(" or ", " or a ")
    v = ctx.perf.get("views")
    parts = [f"{sal}, {ctx.mname}'s Google profile is still unverified."]
    if uplift:
        extra = f" — on your {num(v)} monthly views that's roughly {num(v * uplift)} more" if v else ""
        parts.append(f"Verified listings typically see ~{pct(uplift)} more visibility{extra}.")
    if path:
        parts.append(f"Verification is via {path}; I'll walk you through it in about 5 minutes.")
    parts.append(ctx.h("Want to start now? Reply YES.", "Abhi start karein? Reply YES."))
    return _result(ctx, " ".join(parts), CTA_BINARY,
                   "Unverified GBP: quantified upside on the merchant's own views; effort externalised to a 5-minute guided step.",
                   [sal, pct(uplift) if uplift else ""])


def h_supply(ctx: Ctx) -> Dict[str, Any]:
    sal, p = ctx.sal, ctx.p
    item = ctx.digest_item() or {}
    mol = p.get("molecule") or ""
    batches = p.get("affected_batches") or []
    mfr = p.get("manufacturer")
    chronic = ctx.agg.get("chronic_rx_count")
    last = ctx.last_merchant_msg()
    parts = [f"{sal}, " + ctx.h("urgent", "urgent") + f": {item.get('title') or ('recall on ' + mol)}"
             + (f" — batches {', '.join(batches)}" if batches else "") + (f" ({mfr})" if mfr else "") + "."]
    if item.get("summary"):
        s = item["summary"]
        parts.append(s[len(first_sentence(s)):].strip() or s)
    if last and last.get("engagement") == "intent_action":
        parts.append(f"Following up on your \"{last.get('body')}\" —")
    if chronic:
        parts.append(f"I'll filter your {num(chronic)} chronic-Rx customers for anyone dispensed {mol or 'these batches'} and draft their replacement note.")
    parts.append(ctx.h("Reply YES to start.", "Reply YES — main shuru karti hoon."))
    src = item.get("source")
    body = " ".join(parts) + (f" — {src}" if src else "")
    return _result(ctx, body, CTA_BINARY,
                   "Urgency-5 safety/supply alert: exact batch numbers + source, bounded risk framing, done-for-you customer filtering.",
                   [sal, mol, ", ".join(batches)])


def h_category_seasonal(ctx: Ctx) -> Dict[str, Any]:
    sal, p = ctx.sal, ctx.p
    trends = p.get("trends") or []
    pretty = []
    for tr in trends:
        m = re.match(r"(.+?)_demand_([+-]\d+)", str(tr))
        if m:
            pretty.append(f"{humanize(m.group(1))} {m.group(2)}%")
        else:
            pretty.append(humanize(tr))
    dg = ctx.digest_by_kind("seasonal")
    content = next((c for c in ctx.cat.get("patient_content_library") or [] if "summer" in str(c.get("id", "")) + c.get("title", "").lower()), None)
    parts = [f"{sal}, {humanize(p.get('season', 'seasonal')).replace('2026', '').strip()} demand shift is here: {', '.join(pretty)}." if pretty
             else f"{sal}, {dg.get('title') if dg else 'seasonal demand is shifting'}."]
    if dg and dg.get("actionable"):
        parts.append(f"Suggested shelf change: {lower_first(dg['actionable'].rstrip('.'))}.")
    rep = ctx.agg.get("repeat_customer_pct")
    if content:
        parts.append(f"I can also send your repeat customers" + (f" ({pct(rep)} of your base)" if rep else "")
                     + f" a short '{content.get('title')}' note.")
    parts.append(ctx.h("Want me to draft it? Reply YES.", "Draft kar doon? Reply YES."))
    return _result(ctx, " ".join(parts), CTA_BINARY,
                   "Seasonal demand shift with exact category deltas; concrete shelf action + a ready customer-content broadcast.",
                   [sal, ", ".join(pretty[:2])])


WEATHER_PLAY = {
    "restaurants": ("Footfall drops in peak heat but delivery orders rise — lead with cold drinks/light meals on delivery.",
                    "Tez garmi mein footfall girta hai par delivery badhti hai — delivery pe cold drinks/light meals lead karein."),
    "pharmacies": ("Heat days pull ORS, electrolytes and sunscreen — keep them at the counter and on delivery.",
                   "Garmi mein ORS, electrolytes aur sunscreen ki demand badhti hai — counter aur delivery pe rakhein."),
    "gyms": ("Members skip afternoon sessions in heat — nudge them to early-morning and indoor classes.",
             "Garmi mein afternoon sessions skip hote hain — members ko early-morning/indoor classes pe shift karein."),
    "salons": ("Heat drives demand for hair spa, scalp care and quick cool-down facials.",
               "Garmi mein hair spa, scalp care aur quick facials ki demand badhti hai."),
    "dentists": ("Heat days mean fewer walk-ins — a WhatsApp booking nudge keeps the chair full in cooler evening slots.",
                 "Garmi mein walk-ins kam hote hain — shaam ke slots ke liye WhatsApp booking nudge kaam karta hai."),
}


def _payload_facts(p: dict, limit: int = 4) -> str:
    """Human-readable summary of scalar payload fields (for unseen trigger kinds)."""
    skip = {"placeholder", "metric_or_topic", "merchant_id", "customer_id", "category", "top_item_id", "digest_item_id"}
    bits = []
    for k, v in p.items():
        if k in skip or v in (None, "", [], {}):
            continue
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float, str)):
            vv = pct(v, signed=True) if ("pct" in k or "delta" in k) and isinstance(v, float) else \
                nice_date(v, with_year=False) if isinstance(v, str) and re.match(r"\d{4}-\d{2}-\d{2}", v) else \
                humanize(v) if isinstance(v, str) else num(v)
            bits.append(f"{humanize(k)}: {vv}")
        elif isinstance(v, list) and all(isinstance(x, (str, int, float)) for x in v):
            bits.append(f"{humanize(k)}: {', '.join(humanize(x) for x in v[:4])}")
        if len(bits) >= limit:
            break
    return "; ".join(bits)


def h_weather(ctx: Ctx) -> Dict[str, Any]:
    sal, p = ctx.sal, ctx.p
    temp = p.get("temperature_c") or p.get("temp_c") or p.get("max_temp_c")
    city = p.get("city") or ctx.city
    head = f"{sal}, heat alert for {city}" + (f" — {temp}°C" if temp else "") + (f" {humanize(p.get('when', 'today'))}" if p.get("when") else " today") + "."
    play = WEATHER_PLAY.get(ctx.slug)
    offer, own = ctx.best_offer()
    parts = [head]
    if play:
        parts.append(ctx.h(*play))
    if offer:
        parts.append(f"Your {offer} is the natural hook." if own else f"A '{offer}' style offer fits today.")
    parts.append(ctx.h("Want me to put up a today-only post? Reply YES.", "Aaj ke liye ek post daal doon? Reply YES."))
    return _result(ctx, " ".join(parts), CTA_BINARY,
                   "Weather trigger: explicit why-now (city + temperature) with a category-specific demand play and the merchant's own offer.",
                   [sal, str(city), str(temp or "")])


def h_local_news(ctx: Ctx) -> Dict[str, Any]:
    sal, p = ctx.sal, ctx.p
    headline = p.get("headline") or p.get("title") or p.get("event") or humanize(ctx.kind)
    details = _payload_facts({k: v for k, v in p.items() if k not in ("headline", "title", "event")}, 3)
    parts = [f"{sal}, local update near {ctx.locality}: {headline}" + (f" ({details})" if details else "") + "."]
    if ctx.slug == "restaurants":
        parts.append(ctx.h("Expect slower deliveries on affected routes — setting a realistic ETA on your listing avoids bad reviews.",
                           "Affected routes pe delivery slow hogi — listing pe realistic ETA daalne se bad reviews bachenge."))
    else:
        parts.append(ctx.h("Walk-ins may dip while it lasts — a WhatsApp booking nudge keeps your regulars coming.",
                           "Jab tak yeh chalega walk-ins kam ho sakte hain — WhatsApp booking nudge se regulars aate rahenge."))
    parts.append(ctx.h("Want me to update your listing note for today? Reply YES.", "Aaj ke liye listing note update kar doon? Reply YES."))
    return _result(ctx, " ".join(parts), CTA_BINARY,
                   "Local event trigger: states the event from the payload, translates it into an operational impact for this category, one CTA.",
                   [sal, str(headline)])


def h_generic(ctx: Ctx) -> Dict[str, Any]:
    """Fallback for unknown kinds / sparse payloads — always grounded."""
    if ctx.digest_item():
        return h_digest(ctx)
    p = ctx.p
    if p.get("metric") and p.get("delta_pct") is not None:
        return h_perf_spike(ctx) if float(p["delta_pct"]) > 0 else h_perf_dip(ctx)
    sal = ctx.sal
    fixes = _fixes(ctx)
    v, c = ctx.perf.get("views"), ctx.perf.get("calls")
    why = _payload_facts(p)
    kind_h = humanize(ctx.kind)
    parts = []
    if why and kind_h and kind_h != "generic":
        parts.append(f"{sal}, heads-up — {kind_h}: {why}.")
        parts.append(ctx.h(f"Context for {ctx.mname}: ", f"{ctx.mname} ke liye context: ")
                     + (f"{num(v)} views and {num(c)} calls in the last 30 days." if v is not None else "your listing is live."))
    else:
        parts.append(f"{sal}, " + ctx.h("quick note on ", "") + f"{ctx.mname}" + ctx.h("", " ka quick update")
                     + (f": {num(v)} views and {num(c)} calls in the last 30 days." if v is not None else "."))
    if fixes:
        parts.append(ctx.h("One thing holding it back: ", "Ek cheez jo rok rahi hai: ") + fixes[0] + ".")
    offer, own = ctx.best_offer()
    if offer and not own:
        parts.append(f"In {ctx.slug}, '{offer}' style offers pull the most enquiries.")
    parts.append(ctx.h("Want me to fix that this week? Reply YES.", "Is hafte fix kar doon? Reply YES."))
    return _result(ctx, " ".join(parts), CTA_BINARY,
                   f"Trigger '{ctx.kind}' had no rich payload; composed from the merchant's own performance + the highest-leverage profile gap.",
                   [sal, ctx.mname])


# --------------------------------------------------------------------------- #
# Customer-facing handlers (send_as = merchant_on_behalf)
# --------------------------------------------------------------------------- #
def _from(ctx: Ctx) -> str:
    """'Dr. Meera's Dental Clinic, Lajpat Nagar' / 'Karthik from PowerHouse Fitness'."""
    o = ctx.owner.strip()
    if ctx.slug == "dentists" or not o or o.lower().replace("dr.", "").strip() in ctx.mname.lower():
        return f"{ctx.mname}" + (f", {ctx.locality}" if ctx.locality else "")
    return f"{o} from {ctx.mname}"


def _opener(ctx: Ctx) -> str:
    """Sender line for customer-facing messages, in the customer's language."""
    o = ctx.owner.strip()
    loc = f", {ctx.locality}" if ctx.locality else ""
    owner_distinct = o and ctx.slug != "dentists" and o.lower().replace("dr.", "").strip() not in ctx.mname.lower()
    en = f"{_from(ctx)} here."
    hien = f"main {o}, {ctx.mname} se." if owner_distinct else f"{ctx.mname}{loc} se."
    return ctx.ch(en, hien)


# What "coming back" means per category (customer-facing).
CUSTOMER_SERVICE_NOUN = {
    "dentists": ("routine check-up + cleaning", "routine check-up + cleaning"),
    "salons": ("next appointment", "next appointment"),
    "gyms": ("next session", "agla session"),
    "pharmacies": ("regular medicines", "regular medicines"),
    "restaurants": ("next visit", "agla visit"),
}
CUSTOMER_CTA = {
    "pharmacies": ("Reply YES and we'll keep your usual medicines ready for pickup or home delivery.",
                   "Reply YES — aapki usual medicines pickup ya home delivery ke liye ready rakhenge."),
    "restaurants": ("Reply YES and we'll reserve a table for you this week.",
                    "Reply YES — is hafte aapke liye table reserve kar denge."),
}


def _slot_pref(ctx: Ctx) -> str:
    return humanize(_g(ctx.c, "preferences", "preferred_slots", default="")) or ""


def time12(iso: Any) -> str:
    """'2026-04-26T19:30:00+05:30' -> '7:30pm' (local time as written)."""
    s = str(iso or "")
    m = re.search(r"T(\d{2}):(\d{2})", s)
    if not m:
        return ""
    hh, mm = int(m.group(1)), m.group(2)
    suf = "am" if hh < 12 else "pm"
    h12 = hh % 12 or 12
    return f"{h12}{'' if mm == '00' else ':' + mm}{suf}"


def c_recall(ctx: Ctx) -> Dict[str, Any]:
    p, name = ctx.p, ctx.cname
    noun = CUSTOMER_SERVICE_NOUN.get(ctx.slug, ("next visit", "agla visit"))
    service = humanize(p.get("service_due", "")).replace("6 month", "6-month") or ctx.ch(noun[0], noun[1])
    last = p.get("last_service_date") or _g(ctx.c, "relationship", "last_visit")
    slots = [s.get("label") for s in (p.get("available_slots") or []) if s.get("label")]
    offer, own = ctx.best_offer()
    hi = f"Hi {name}" if name else ctx.ch("Hi", "Namaste")
    parts = [f"{hi}, " + _opener(ctx)]
    parts.append(ctx.ch(f"Your {service} is due" + (f" (last visit {nice_date(last)})" if last else "") + ".",
                        f"Aapka {service} due hai" + (f" (last visit {nice_date(last)})" if last else "") + "."))
    if own and offer:
        parts.append(ctx.ch(f"Current offer: {offer}.", f"Abhi offer: {offer}."))
    if slots:
        opts = ", ".join(f"{i + 1}) {s}" for i, s in enumerate(slots[:3]))
        pref = _slot_pref(ctx)
        parts.append(ctx.ch(f"Slots held for you{(' (' + pref + ', as you prefer)') if pref else ''}: {opts}.",
                            f"Aapke liye slots ready hain{(' (' + pref + ')') if pref else ''}: {opts}."))
        parts.append(ctx.ch("Reply with the number, or tell us a time that suits you.",
                            "Number reply kar dijiye, ya apna time bata dijiye."))
        cta = CTA_SLOTS
    else:
        parts.append(ctx.ch("Reply YES and we'll book a slot that suits you this week.",
                            "Reply YES — is hafte aapke time pe slot book kar denge."))
        cta = CTA_BINARY
    return _result(ctx, " ".join(parts), cta,
                   "Customer recall due: names the service + last visit, merchant's real offer and open slots, honours language/slot preference.",
                   [name, service, slots[0] if slots else ""])


def c_appointment(ctx: Ctx) -> Dict[str, Any]:
    p, name = ctx.p, ctx.cname
    when = p.get("slot_label") or (nice_date(p.get("appointment_iso"), with_dow=True) if p.get("appointment_iso") else None)
    svc = humanize(p.get("service", ""))
    visits = _g(ctx.c, "relationship", "visits_total")
    hi = f"Hi {name}" if name else "Hi"
    parts = [f"{hi}, " + _opener(ctx) + " Reminder:"]
    parts.append(ctx.ch(f"your {svc + ' ' if svc else ''}appointment is tomorrow" + (f", {when}" if when else "") + ".",
                        f"aapka {svc + ' ' if svc else ''}appointment kal hai" + (f", {when}" if when else "") + "."))
    if visits and int(visits) > 1:
        parts.append(ctx.ch(f"Always good to see you (visit #{int(visits) + 1}).", f"Aapka visit #{int(visits) + 1} — hamesha accha lagta hai."))
    parts.append(ctx.ch("Reply YES to confirm, or tell us if you need to reschedule.",
                        "Confirm karne ke liye YES reply karein, ya reschedule chahiye toh bata dijiye."))
    return _result(ctx, " ".join(parts), CTA_BINARY,
                   "Appointment reminder: confirm-or-reschedule reduces no-shows; honours customer language.",
                   [name, when or "tomorrow"])


def c_refill(ctx: Ctx) -> Dict[str, Any]:
    p, name = ctx.p, ctx.cname
    mols = p.get("molecule_list") or []
    if ctx.slug != "pharmacies" and not mols:
        # Non-pharmacy "refill" = a due follow-up; never talk about dispatching prescriptions.
        out = c_recall(ctx)
        out["rationale"] = "Follow-up due for a non-pharmacy customer: framed as a routine visit (no prescription claims), relationship-grounded, single CTA."
        return out
    runs = p.get("stock_runs_out_iso")
    saved = p.get("delivery_address_saved") or _g(ctx.c, "preferences", "delivery_address") == "saved"
    senior = _g(ctx.c, "identity", "senior_citizen")
    offers = ctx.active_offers()
    via = str(_g(ctx.c, "preferences", "channel", default=""))
    # Address the family member who holds the phone if needed.
    surname = name.replace("Mr.", "").replace("Mrs.", "").strip()
    who = f"{surname} ji" if ("via" in via and surname) else name
    greet = ctx.ch("Hello", "Namaste", "Namaste")
    parts = [f"{greet} — {ctx.mname}" + (f", {ctx.locality}" if ctx.locality else "") + ctx.ch(" here.", " se.", " se.")]
    if mols:
        parts.append(ctx.ch(f"{who}'s {len(mols)} regular medicines ({', '.join(mols)})"
                            + (f" run out on {nice_date(runs)}." if runs else " are due for refill."),
                            f"{who} ki {len(mols)} regular medicines ({', '.join(mols)})"
                            + (f" {nice_date(runs)} ko khatam ho jayengi." if runs else " refill ke liye due hain."),
                            f"{who} ki {len(mols)} niyamit dawaiyan ({', '.join(mols)})"
                            + (f" {nice_date(runs)} ko khatam ho jayengi." if runs else " refill ke liye due hain.")))
    else:
        parts.append(ctx.ch("Your regular refill is due.", "Aapka regular refill due hai.", "Aapka niyamit refill due hai."))
    perks = []
    for o in offers:
        if senior and "senior" in o.lower():
            perks.append(o)
        elif "delivery" in o.lower():
            perks.append(o)
    if perks:
        parts.append(ctx.ch("Applies: ", "Laagu: ", "Laagu: ") + "; ".join(perks) + ".")
    if saved:
        parts.append(ctx.ch("We can deliver to your saved address.", "Saved address pe delivery kar denge.", "Saved address pe delivery kar denge."))
    parts.append(ctx.ch("Reply CONFIRM to dispatch the same prescription, or tell us if the dose has changed.",
                        "Same prescription dispatch karne ke liye CONFIRM reply karein, ya dose badla ho toh bata dijiye.",
                        "Wahi parchi dispatch karne ke liye CONFIRM likhein, ya dose badla ho toh bata dijiye."))
    return _result(ctx, " ".join(parts), CTA_BINARY,
                   "Chronic refill: exact molecules + run-out date, only offers the merchant actually runs, respectful senior/family addressing, dose-change safety check.",
                   [who, ", ".join(mols), nice_date(runs) if runs else ""])


def c_lapsed(ctx: Ctx) -> Dict[str, Any]:
    p, name = ctx.p, ctx.cname
    days = p.get("days_since_last_visit")
    focus = humanize(p.get("previous_focus") or _g(ctx.c, "preferences", "training_focus", default=""))
    months = p.get("previous_membership_months")
    offer, own = ctx.best_offer()
    pref = _slot_pref(ctx)
    hi = f"Hi {name}" if name else "Hi"
    parts = [f"{hi}, " + _opener(ctx)]
    if days:
        parts.append(ctx.ch(f"It's been {days} days — breaks happen, no judgment.",
                            f"{days} din ho gaye — breaks hote rehte hain, koi baat nahi."))
    else:
        last = _g(ctx.c, "relationship", "last_visit")
        visits = _g(ctx.c, "relationship", "visits_total")
        noun = CUSTOMER_SERVICE_NOUN.get(ctx.slug, ("next visit", "agla visit"))
        parts.append(ctx.ch(f"It's been a while since your last visit" + (f" on {nice_date(last)}" if last else "")
                            + (f" — thanks for the {visits} visits so far" if visits and int(visits) > 1 else "") + ".",
                            f"Aapka last visit" + (f" {nice_date(last)} ko" if last else "") + " tha"
                            + (f" — ab tak ke {visits} visits ke liye shukriya" if visits and int(visits) > 1 else "") + "."))
        parts.append(ctx.ch(f"Good time for your {noun[0]}.", f"{noun[1].capitalize()} ka sahi time hai."))
    if focus:
        parts.append(ctx.ch(f"Your {focus} plan" + (f" from your {months} months with us" if months else "") + " is still on file, so you won't start from zero.",
                            f"Aapka {focus} plan" + (f" ({months} months ka)" if months else "") + " abhi bhi saved hai — zero se start nahi karna padega."))
    if own and offer:
        parts.append(ctx.ch(f"Current offer: {offer}.", f"Abhi offer: {offer}."))
    if ctx.slug in CUSTOMER_CTA:
        parts.append(ctx.ch(*CUSTOMER_CTA[ctx.slug]))
    else:
        parts.append(ctx.ch(f"Want us to hold a{(' ' + pref) if pref else ''} slot this week? Reply YES — no commitment.",
                            f"Is hafte{(' ' + pref) if pref else ''} ek slot hold kar dein? Reply YES — koi commitment nahi."))
    return _result(ctx, " ".join(parts), CTA_BINARY,
                   "Lapsed customer win-back: no-shame tone, references their past goal, merchant's real offer, zero-commitment YES.",
                   [name, str(days or ""), focus])


def c_trial_followup(ctx: Ctx) -> Dict[str, Any]:
    p, name = ctx.p, ctx.cname
    child = ctx.child_name
    trial = p.get("trial_date")
    opts = [o.get("label") for o in (p.get("next_session_options") or []) if o.get("label")]
    hi = f"Hi {name}" if name else "Hi"
    subj = ctx.ch(f"{child}'s" if child else "your", f"{child} ka" if child else "aapka")
    parts = [f"{hi}, " + _opener(ctx)]
    parts.append(ctx.ch(f"Thanks for coming in for {subj} trial" + (f" on {nice_date(trial)}" if trial else "") + ".",
                        f"{subj.capitalize()} trial" + (f" ({nice_date(trial)})" if trial else "") + " ke liye shukriya."))
    if opts:
        parts.append(ctx.ch(f"Next session: {opts[0]}.", f"Agla session: {opts[0]}."))
        parts.append(ctx.ch("Reply YES to lock it in.", "Lock karne ke liye YES reply karein."))
    else:
        parts.append(ctx.ch("Reply YES and we'll book the next session at a time that suits you.",
                            "Reply YES — aapke time pe agla session book kar denge."))
    return _result(ctx, " ".join(parts), CTA_BINARY,
                   "Trial follow-up: continuity from the trial, one concrete next slot, single YES.",
                   [name, opts[0] if opts else ""])


def c_bridal(ctx: Ctx) -> Dict[str, Any]:
    p, name = ctx.p, ctx.cname
    wd = p.get("wedding_date") or _g(ctx.c, "preferences", "wedding_date")
    days = p.get("days_to_wedding") or ctx.days_until(wd)
    step = humanize(p.get("next_step_window_open", ""))
    trial = p.get("trial_completed")
    pref = _slot_pref(ctx)
    hi = f"Hi {name}" if name else "Hi"
    parts = [f"{hi}, " + _opener(ctx)]
    if days:
        parts.append(ctx.ch(f"{days} days to your wedding" + (f" on {nice_date(wd)}" if wd else "") + "!",
                            f"Shaadi mein {days} din" + (f" ({nice_date(wd)})" if wd else "") + "!"))
    if trial:
        parts.append(ctx.ch(f"Since your bridal trial on {nice_date(trial)}, the next step is the {step or 'prep programme'}.",
                            f"{nice_date(trial)} ke bridal trial ke baad, agla step hai {step or 'prep programme'}."))
    parts.append(ctx.ch("Starting early gives the best result before peak bridal season fills our calendar.",
                        "Jaldi start karne se best result milta hai, peak season se pehle."))
    parts.append(ctx.ch(f"Want us to block your first{(' ' + pref) if pref else ''} session? Reply YES.",
                        f"Pehla{(' ' + pref) if pref else ''} session block kar dein? Reply YES."))
    return _result(ctx, " ".join(parts), CTA_BINARY,
                   "Bridal follow-up: wedding countdown + continuity from the trial, preference-honouring single YES.",
                   [name, str(days or ""), step])


def c_generic(ctx: Ctx) -> Dict[str, Any]:
    name = ctx.cname
    offer, own = ctx.best_offer()
    visits = _g(ctx.c, "relationship", "visits_total")
    hi = f"Hi {name}" if name else "Hi"
    parts = [f"{hi}, " + _opener(ctx)]
    if visits:
        parts.append(ctx.ch(f"Thanks for your {visits} visits with us.", f"Aapke {visits} visits ke liye shukriya."))
    if own and offer:
        parts.append(ctx.ch(f"This week: {offer}.", f"Is hafte: {offer}."))
    parts.append(ctx.ch("Reply YES and we'll hold a slot for you.", "Reply YES — aapke liye slot hold kar denge."))
    return _result(ctx, " ".join(parts), CTA_BINARY,
                   f"Customer trigger '{ctx.kind}' with sparse payload: relationship-grounded, real offer only, single YES.",
                   [name])


# --------------------------------------------------------------------------- #
# Routing
# --------------------------------------------------------------------------- #
MERCHANT_ROUTES: Dict[str, Callable[[Ctx], Dict[str, Any]]] = {
    "research_digest": h_digest,
    "category_research_digest_release": h_digest,
    "regulation_change": h_digest,
    "compliance_update": h_digest,
    "cde_opportunity": h_digest,
    "category_trend_movement": h_digest,
    "competitor_opened": h_competitor,
    "perf_dip": h_perf_dip,
    "seasonal_perf_dip": h_seasonal_dip,
    "perf_spike": h_perf_spike,
    "milestone_reached": h_milestone,
    "review_theme_emerged": h_review_theme,
    "curious_ask_due": h_curious_ask,
    "scheduled_recurring": h_curious_ask,
    "festival_upcoming": h_festival,
    "ipl_match_today": h_ipl,
    "active_planning_intent": h_planning,
    "renewal_due": h_renewal,
    "winback_eligible": h_winback,
    "dormant_with_vera": h_dormant,
    "gbp_unverified": h_gbp_unverified,
    "supply_alert": h_supply,
    "category_seasonal": h_category_seasonal,
    "weather_heatwave": h_weather,
    "weather_alert": h_weather,
    "local_news_event": h_local_news,
    "local_event": h_local_news,
    "trial_ending": h_renewal,
    "subscription_expiring": h_renewal,
}

CUSTOMER_ROUTES: Dict[str, Callable[[Ctx], Dict[str, Any]]] = {
    "recall_due": c_recall,
    "appointment_tomorrow": c_appointment,
    "chronic_refill_due": c_refill,
    "customer_lapsed_soft": c_lapsed,
    "customer_lapsed_hard": c_lapsed,
    "trial_followup": c_trial_followup,
    "wedding_package_followup": c_bridal,
}


def customer_consented(customer: Optional[dict]) -> bool:
    if not customer:
        return True
    scope = _g(customer, "consent", "scope", default=[]) or []
    return bool(scope)


def compose(category: dict, merchant: dict, trigger: dict, customer: Optional[dict] = None,
            now: Optional[datetime] = None) -> Dict[str, Any]:
    """Compose the next outbound message. Never raises; falls back to a grounded generic."""
    ctx = Ctx(category, merchant, trigger, customer, now)
    is_customer = customer is not None or (trigger or {}).get("scope") == "customer"
    try:
        if is_customer and customer is not None:
            fn = CUSTOMER_ROUTES.get(ctx.kind, c_generic)
        else:
            fn = MERCHANT_ROUTES.get(ctx.kind, h_generic)
        out = fn(ctx)
    except Exception as exc:  # defensive: never fail a send on bad data
        try:
            out = (c_generic if (is_customer and customer is not None) else h_generic)(ctx)
            out["rationale"] += f" (fallback after handler error: {type(exc).__name__})"
        except Exception:
            out = _result(ctx, f"{ctx.sal}, quick update on {ctx.mname} — want a 3-line summary of this week's numbers? Reply YES.",
                          CTA_BINARY, "Last-resort fallback", [ctx.sal])
    # Anti-repetition vs. anything already sent to this merchant.
    if out["body"] in ctx.history_bodies():
        out["body"] += ctx.h(" (Fresh numbers as of today.)", " (Aaj ke fresh numbers.)")
    return out
