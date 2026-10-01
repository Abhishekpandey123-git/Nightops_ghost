"""The AI gateway. Every AI call in nightops goes through here.

Token-saving rules (the "oneKn8" idea, applied to everything):
  1. OFF by default. If disabled or no key, ask() returns None and callers
     fall back to their non-AI behaviour.
  2. Cache: identical prompt + model never costs twice.
  3. Daily budget: once today's tokens are spent, calls are skipped.
  4. Routing: simple classification uses the cheap model; only code
     changes use the strong model.
  5. Compression: inputs are trimmed (head + tail kept) to a char limit.

Providers:
  openai_compatible  -> Groq, OpenAI, Ollama (local), OpenRouter, etc.
  anthropic          -> Claude API
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Callable

import requests

from .util import log, now_iso, register_secret, today

Transport = Callable[[str, str | None, str, int], tuple[str, int, int]]


class AI:
    def __init__(self, cfg: dict, store, transport: Transport | None = None):
        self.c = cfg.get("ai", {}) or {}
        self.store = store
        self.provider = self.c.get("provider", "openai_compatible")
        self.base_url = (self.c.get("base_url") or "").rstrip("/")
        self.key = os.environ.get(self.c.get("api_key_env", "AI_API_KEY"))
        register_secret(self.key)
        local = "localhost" in self.base_url or "127.0.0.1" in self.base_url
        self.transport = transport
        self.enabled = bool(self.c.get("enabled")) and bool(self.key or local or transport)

    # ---------------------------------------------------------------- budget
    def used_today(self) -> int:
        row = self.store.one(
            "SELECT COALESCE(SUM(tokens_in+tokens_out),0) AS t FROM ai_usage "
            "WHERE day=? AND cached=0", (today(),))
        return int(row["t"])

    def budget(self) -> int:
        return int(self.c.get("daily_token_budget", 200000))

    # ----------------------------------------------------------- compression
    @staticmethod
    def compress(text: str, limit: int) -> str:
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(r"[ \t]+\n", "\n", text)
        if len(text) <= limit:
            return text
        head = int(limit * 0.7)
        tail = limit - head - 40
        return text[:head] + "\n\n[... trimmed by nightops ...]\n\n" + text[-tail:]

    # ------------------------------------------------------------------- ask
    def ask(self, prompt: str, *, system: str | None = None, tier: str = "cheap",
            max_tokens: int = 800, purpose: str = "general",
            max_chars: int | None = None) -> str | None:
        if not self.enabled:
            return None
        model = self.c.get("strong_model") if tier == "strong" else self.c.get("cheap_model")
        prompt = self.compress(prompt, max_chars or int(self.c.get("max_input_chars", 12000)))
        key = hashlib.sha256(json.dumps(
            [self.provider, model, system, prompt, max_tokens]).encode()).hexdigest()

        if self.c.get("cache", True):
            hit = self.store.one("SELECT response FROM ai_cache WHERE key=?", (key,))
            if hit:
                self._record(purpose, model, 0, 0, cached=True)
                return hit["response"]

        estimate = (len(prompt) + len(system or "")) // 4 + max_tokens
        if self.used_today() + estimate > self.budget():
            log("ai", f"daily budget reached, skipping '{purpose}'")
            return None

        try:
            text, tin, tout = self._call(model, system, prompt, max_tokens)
        except Exception as e:  # network, auth, quota... never crash a module
            log("ai", f"call failed for '{purpose}': {e}")
            return None

        self._record(purpose, model, tin, tout, cached=False)
        if self.c.get("cache", True):
            self.store.x("INSERT OR REPLACE INTO ai_cache(key,response,at) VALUES(?,?,?)",
                         (key, text, now_iso()))
        return text

    def ask_json(self, prompt: str, **kw) -> dict | None:
        text = self.ask(prompt, **kw)
        if not text:
            return None
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            return None
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None

    # ------------------------------------------------------------- internals
    def _record(self, purpose: str, model: str, tin: int, tout: int, cached: bool) -> None:
        self.store.x("INSERT INTO ai_usage(day,at,purpose,model,tokens_in,tokens_out,cached) "
                     "VALUES(?,?,?,?,?,?,?)",
                     (today(), now_iso(), purpose, model, tin, tout, int(cached)))

    def _call(self, model: str, system: str | None, prompt: str,
              max_tokens: int) -> tuple[str, int, int]:
        if self.transport:
            return self.transport(model, system, prompt, max_tokens)

        if self.provider == "anthropic":
            body = {"model": model, "max_tokens": max_tokens,
                    "messages": [{"role": "user", "content": prompt}]}
            if system:
                body["system"] = system
            r = requests.post("https://api.anthropic.com/v1/messages", json=body, timeout=180,
                              headers={"x-api-key": self.key or "",
                                       "anthropic-version": "2023-06-01"})
            r.raise_for_status()
            d = r.json()
            text = "".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text")
            u = d.get("usage", {})
            return text, int(u.get("input_tokens", 0)), int(u.get("output_tokens", 0))

        # openai-compatible (Groq / OpenAI / Ollama / OpenRouter ...)
        messages = ([{"role": "system", "content": system}] if system else []) + \
                   [{"role": "user", "content": prompt}]
        headers = {"Authorization": f"Bearer {self.key}"} if self.key else {}
        r = requests.post(f"{self.base_url}/chat/completions", headers=headers, timeout=180,
                          json={"model": model, "messages": messages,
                                "max_tokens": max_tokens, "temperature": 0.2})
        r.raise_for_status()
        d = r.json()
        text = d["choices"][0]["message"]["content"] or ""
        u = d.get("usage") or {}
        tin = int(u.get("prompt_tokens", len(prompt) // 4))
        tout = int(u.get("completion_tokens", len(text) // 4))
        return text, tin, tout
