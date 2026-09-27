"""
Optional LLM layer — OFF unless an API key is configured.

    LLM_PROVIDER = openai | anthropic | openrouter | groq | deepseek | gemini   (default: openai)
    LLM_API_KEY  = ...
    LLM_MODEL    = optional override
    LLM_TIMEOUT  = seconds per call (default 8)

Guarantees
* temperature 0 + in-process cache keyed on the prompt -> same input, same output.
* Grounding validator: any number / ₹ amount / % in the LLM output that does not
  appear in the source facts is treated as fabrication and the output is rejected
  (caller falls back to the deterministic text).
* Never raises. Any error/timeout returns None.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from typing import Optional
from urllib import request as urlrequest

log = logging.getLogger("vera.llm")

_CACHE: dict = {}
_LOCK = threading.Lock()

DEFAULT_MODELS = {
    "openai": "gpt-4o-mini",
    "anthropic": "claude-3-5-haiku-latest",
    "openrouter": "anthropic/claude-3.5-haiku",
    "groq": "llama-3.3-70b-versatile",
    "deepseek": "deepseek-chat",
    "gemini": "gemini-1.5-flash",
}
OPENAI_COMPAT = {
    "openai": "https://api.openai.com/v1/chat/completions",
    "openrouter": "https://openrouter.ai/api/v1/chat/completions",
    "groq": "https://api.groq.com/openai/v1/chat/completions",
    "deepseek": "https://api.deepseek.com/v1/chat/completions",
}


def enabled() -> bool:
    return bool(os.getenv("LLM_API_KEY"))


def provider_name() -> str:
    if not enabled():
        return "none"
    p = os.getenv("LLM_PROVIDER", "openai").lower()
    return f"{p}:{os.getenv('LLM_MODEL') or DEFAULT_MODELS.get(p, '?')}"


def _call(system: str, user: str, timeout: float, max_tokens: int) -> Optional[str]:
    p = os.getenv("LLM_PROVIDER", "openai").lower()
    key = os.getenv("LLM_API_KEY", "")
    model = os.getenv("LLM_MODEL") or DEFAULT_MODELS.get(p, "gpt-4o-mini")
    if p in OPENAI_COMPAT:
        body = {"model": model, "temperature": 0, "max_tokens": max_tokens,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        req = urlrequest.Request(OPENAI_COMPAT[p], data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        data = json.loads(urlrequest.urlopen(req, timeout=timeout).read().decode())
        return data["choices"][0]["message"]["content"]
    if p == "anthropic":
        body = {"model": model, "temperature": 0, "max_tokens": max_tokens, "system": system,
                "messages": [{"role": "user", "content": user}]}
        req = urlrequest.Request("https://api.anthropic.com/v1/messages", data=json.dumps(body).encode(),
                                 headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                                          "Content-Type": "application/json"})
        data = json.loads(urlrequest.urlopen(req, timeout=timeout).read().decode())
        return data["content"][0]["text"]
    if p == "gemini":
        body = {"contents": [{"parts": [{"text": f"{system}\n\n{user}"}]}],
                "generationConfig": {"temperature": 0, "maxOutputTokens": max_tokens}}
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
        req = urlrequest.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        data = json.loads(urlrequest.urlopen(req, timeout=timeout).read().decode())
        return data["candidates"][0]["content"]["parts"][0]["text"]
    return None


def complete(system: str, user: str, max_tokens: int = 300, timeout: Optional[float] = None) -> Optional[str]:
    if not enabled():
        return None
    k = hashlib.sha256((system + "\x00" + user).encode()).hexdigest()
    with _LOCK:
        if k in _CACHE:
            return _CACHE[k]
    try:
        out = _call(system, user, timeout or float(os.getenv("LLM_TIMEOUT", "8")), max_tokens)
    except Exception as exc:  # network, quota, parse — all non-fatal
        log.warning("llm call failed: %s", type(exc).__name__)
        return None
    if out:
        out = out.strip().strip('"').strip()
        with _LOCK:
            _CACHE[k] = out
    return out


_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _numbers(text: str) -> set:
    return {n.replace(",", "").rstrip(".") for n in _NUM.findall(text or "")}


def grounded(output: str, facts: str) -> bool:
    """Every number in `output` must appear in `facts` (single digits 0-9 are allowed: list markers, 'Reply 1')."""
    allowed = _numbers(facts)
    for n in _numbers(output):
        if len(n) == 1 or n in allowed:
            continue
        # allow percentages written from fractions: 0.38 -> 38
        try:
            if any(abs(float(a) * 100 - float(n)) < 0.51 for a in allowed if a.startswith("0.")):
                continue
        except ValueError:
            pass
        return False
    return True
