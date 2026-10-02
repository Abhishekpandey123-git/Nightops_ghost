"""The AI gateway. Every AI call in nightops goes through here.

  * Several providers at once: Gemini, Groq, OpenRouter, local Ollama, Claude.
  * A route per task (job-fit notes, issue triage, code fixes): an ordered
    list of provider+model. If one is out of quota, the next one is tried.
  * Local limits per provider/model (requests/min, tokens/min, requests/day)
    so nightops pauses itself BEFORE a provider refuses.
  * Learns from "429 / quota exceeded" replies: a per-minute limit pauses that
    model for the few seconds asked; a daily quota pauses it until the
    provider's daily reset (Gemini: midnight Pacific).
  * When every model on a route is paused, `ai.deferred` says until when.
    The queue (modules/aiq.py) then stops and resumes from that same task.
  * Token saving: off by default, one cache shared by all providers, input
    trimming, per-provider daily token caps.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import time
from typing import Callable

import requests

from .util import ISO, log, now_iso, register_secret, today

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
Transport = Callable[[str, str, str | None, str, int], tuple[str, int, int]]


class AILimit(Exception):
    """A provider refused because of a limit. `until` = when to try again."""

    def __init__(self, message: str = "", until: float | None = None, daily: bool | None = None):
        super().__init__(message)
        self.message, self.until, self.daily = message, until, daily


# ------------------------------------------------------------------- time
def next_midnight(tz_name: str) -> float:
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tz_name)
    except Exception:  # no tz database (bare Windows): close-enough fallbacks
        tz = dt.timezone(dt.timedelta(hours=-8)) if "Los_Angeles" in tz_name else dt.timezone.utc
    now = dt.datetime.now(tz)
    nxt = (now + dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return nxt.timestamp() + 60


def last_midnight_iso(tz_name: str) -> str:
    """Start of the provider's current 'day', as a UTC ISO string."""
    return dt.datetime.fromtimestamp(next_midnight(tz_name) - 60 - 86400, dt.timezone.utc).strftime(ISO)


def parse_duration(text: str | None) -> float | None:
    """'37s', '7m12.4s', '1h2m', '500ms' -> seconds."""
    parts = re.findall(r"(\d+(?:\.\d+)?)\s*(ms|h|m|s)(?![a-z])", str(text or ""), re.I)
    mult = {"h": 3600, "m": 60, "s": 1, "ms": 0.001}
    return sum(float(n) * mult[u.lower()] for n, u in parts) if parts else None


def parse_retry(body: str, headers: dict | None = None) -> tuple[float | None, bool]:
    """From a 429 reply: (seconds to wait or None, is it a daily quota?)."""
    headers = {k.lower(): v for k, v in (headers or {}).items()}
    daily = bool(re.search(r"PerDay|per day|daily|\bRPD\b|\bTPD\b", body or "", re.I))
    secs = None
    if headers.get("retry-after"):
        try:
            secs = float(headers["retry-after"])
        except ValueError:
            pass
    if secs is None:
        m = re.search(r'"retryDelay"\s*:\s*"([^"]+)"', body or "") or \
            re.search(r"(?:retry|try again) in\s+([0-9hms.]+)", body or "", re.I)
        secs = parse_duration(m.group(1)) if m else None
    return secs, daily


# ---------------------------------------------------------------- gateway
class AI:
    def __init__(self, cfg: dict, store, transport: Transport | None = None):
        self.c = cfg.get("ai", {}) or {}
        self.store = store
        self.transport = transport
        self.deferred: dict | None = None
        self._last_call: dict[str, float] = {}
        self.providers, self.routes = self._load(self.c)
        usable = any(p["usable"] for p in self.providers.values())
        self.enabled = bool(self.c.get("enabled")) and usable

    # ------------------------------------------------------------- config
    def _load(self, c: dict):
        provs, routes = c.get("providers"), c.get("routes") or {}
        if not provs:  # older single-provider config keeps working
            ptype = c.get("provider", "openai_compatible")
            provs = {"default": {
                "type": ptype, "base_url": c.get("base_url"),
                "api_key_env": c.get("api_key_env") or ("GEMINI_API_KEY" if ptype == "gemini" else "AI_API_KEY"),
                "daily_tokens": c.get("daily_token_budget", 200000),
                "daily_reset": c.get("daily_reset")}}
            lim = c.get("limits") or {}
            routes = {"cheap": [{"provider": "default", "model": c.get("cheap_model"), "limits": lim.get("cheap")}],
                      "strong": [{"provider": "default", "model": c.get("strong_model"), "limits": lim.get("strong")}]}
        out = {}
        for name, p in provs.items():
            p = dict(p or {})
            ptype = p.get("type", "openai_compatible")
            key = os.environ.get(p["api_key_env"]) if p.get("api_key_env") else None
            register_secret(key)
            base = (p.get("base_url") or (GEMINI_BASE if ptype == "gemini" else "")).rstrip("/")
            local = "localhost" in base or "127.0.0.1" in base
            p.update(name=name, type=ptype, key=key, base_url=base,
                     daily_reset=p.get("daily_reset") or ("America/Los_Angeles" if ptype == "gemini" else "UTC"),
                     usable=bool(p.get("enabled", True)) and bool(key or local or self.transport))
            out[name] = p
        return out, routes

    def route(self, purpose: str, tier: str = "cheap") -> list[dict]:
        r = self.routes.get(purpose) or self.routes.get(tier) or self.routes.get("default") or []
        return [s for s in r if s and s.get("provider") in self.providers and s.get("model")]

    # ------------------------------------------------------------- limits
    def _slot(self, provider: str, model: str) -> str:
        return f"{provider}/{model}"

    def paused_until(self, provider: str, model: str) -> tuple[float, str]:
        raw = self.store.kv_get(f"ai:pause:{self._slot(provider, model)}")
        if not raw:
            return 0.0, ""
        until, _, reason = raw.partition("|")
        return float(until), reason

    def _pause(self, provider: str, model: str, until: float, reason: str) -> None:
        self.store.kv_set(f"ai:pause:{self._slot(provider, model)}", f"{until}|{reason}")
        when = dt.datetime.fromtimestamp(until, dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        log("ai", f"{provider}/{model}: {reason}; paused until {when}")

    def used_today(self, provider: str | None = None) -> int:
        sql, args = "SELECT COALESCE(SUM(tokens_in+tokens_out),0) t FROM ai_usage WHERE day=? AND cached=0", (today(),)
        if provider:
            sql += " AND provider=?"; args += (provider,)
        return int(self.store.one(sql, args)["t"])

    def budget(self) -> int:
        return sum(int(p.get("daily_tokens") or 0) for p in self.providers.values() if p["usable"])

    def _window(self, provider: str, model: str, since_iso: str) -> tuple[int, int]:
        row = self.store.one("SELECT COUNT(*) n, COALESCE(SUM(tokens_in+tokens_out),0) t FROM ai_usage "
                             "WHERE cached=0 AND provider=? AND model=? AND at>=?", (provider, model, since_iso))
        return int(row["n"]), int(row["t"])

    def _blocked(self, p: dict, step: dict, est: int) -> tuple[float, str] | None:
        """(until, reason) if this provider/model must not be called now."""
        name, model = p["name"], step["model"]
        until, reason = self.paused_until(name, model)
        if until > time.time():
            return until, reason
        lim = {**(p.get("limits") or {}), **(step.get("limits") or {})}
        minute_ago = dt.datetime.fromtimestamp(time.time() - 60, dt.timezone.utc).strftime(ISO)
        n_min, t_min = self._window(name, model, minute_ago)
        if lim.get("rpm") and n_min >= int(lim["rpm"]):
            return time.time() + 61, "per-minute request limit (local)"
        if lim.get("tpm") and t_min + est > int(lim["tpm"]):
            return time.time() + 61, "per-minute token limit (local)"
        if lim.get("rpd"):
            n_day, _ = self._window(name, model, last_midnight_iso(p["daily_reset"]))
            if n_day >= int(lim["rpd"]):
                return next_midnight(p["daily_reset"]), "daily request limit (local)"
        cap = int(p.get("daily_tokens") or 0)
        if cap and self.used_today(name) + est > cap:
            return next_midnight("UTC"), f"{name} daily token cap (your setting)"
        return None

    def available(self, purpose: str, tier: str = "cheap") -> bool:
        return any(self.providers[s["provider"]]["usable"] and not self._blocked(self.providers[s["provider"]], s, 0)
                   for s in self.route(purpose, tier))

    # --------------------------------------------------------------- ask
    @staticmethod
    def compress(text: str, limit: int) -> str:
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(r"[ \t]+\n", "\n", text)
        if len(text) <= limit:
            return text
        head = int(limit * 0.7)
        return text[:head] + "\n\n[... trimmed by nightops ...]\n\n" + text[-(limit - head - 40):]

    @staticmethod
    def parse_json(text: str | None) -> dict | None:
        if not text:
            return None
        m = re.search(r"\{.*\}", text, re.S)
        try:
            return json.loads(m.group(0)) if m else None
        except json.JSONDecodeError:
            return None

    def ask_json(self, prompt: str, **kw) -> dict | None:
        return self.parse_json(self.ask(prompt, **kw))

    def ask(self, prompt: str, *, system: str | None = None, tier: str = "cheap",
            max_tokens: int = 800, purpose: str = "general", max_chars: int | None = None) -> str | None:
        """Answer from the first available model on the route, or None.
        When None because of limits, self.deferred = {"until", "reason"}."""
        self.deferred = None
        if not self.enabled:
            return None
        prompt = self.compress(prompt, max_chars or int(self.c.get("max_input_chars", 12000)))
        key = hashlib.sha256(json.dumps([purpose, system, prompt, max_tokens]).encode()).hexdigest()
        if self.c.get("cache", True):
            hit = self.store.one("SELECT response FROM ai_cache WHERE key=?", (key,))
            if hit:
                self._record("cache", "cache", purpose, 0, 0, cached=True)
                return hit["response"]

        est = (len(prompt) + len(system or "")) // 4 + max_tokens
        waits: list[tuple[float, str]] = []
        for step in self.route(purpose, tier):
            p = self.providers[step["provider"]]
            if not p["usable"]:
                continue
            blocked = self._blocked(p, step, est)
            if blocked:
                waits.append(blocked)
                continue
            try:
                text, tin, tout = self._call(p, step, system, prompt, max_tokens)
            except AILimit as e:
                secs, daily = parse_retry(e.message)
                daily = e.daily if e.daily is not None else daily
                until = e.until or (next_midnight(p["daily_reset"]) if daily and not secs
                                    else time.time() + (secs or 60))
                reason = f"{p['name']} {'daily quota' if daily else 'per-minute limit'} reached"
                self._pause(p["name"], step["model"], until, reason)
                waits.append((until, reason))
                continue
            except Exception as e:  # network, auth, unknown model...: try the next one
                log("ai", f"{p['name']}/{step['model']} failed for '{purpose}': {str(e)[:200]}")
                continue
            self._record(p["name"], step["model"], purpose, tin, tout, cached=False)
            if text and self.c.get("cache", True):
                self.store.x("INSERT OR REPLACE INTO ai_cache(key,response,at) VALUES(?,?,?)",
                             (key, text, now_iso()))
            return text
        if waits:
            until, reason = min(waits)
            self.deferred = {"until": until, "reason": reason}
        return None

    # ---------------------------------------------------------- providers
    def _record(self, provider, model, purpose, tin, tout, cached) -> None:
        self.store.x("INSERT INTO ai_usage(day,at,purpose,model,tokens_in,tokens_out,cached,provider) "
                     "VALUES(?,?,?,?,?,?,?,?)",
                     (today(), now_iso(), purpose, model, tin, tout, int(cached), provider))

    def _pace(self, p: dict) -> None:
        gap = float(p.get("min_interval_seconds") or 0)
        wait = self._last_call.get(p["name"], 0) + gap - time.time()
        if wait > 0:
            time.sleep(wait)
        self._last_call[p["name"]] = time.time()

    def _call(self, p: dict, step: dict, system, prompt, max_tokens) -> tuple[str, int, int]:
        if self.transport:
            return self.transport(p["name"], step["model"], system, prompt, max_tokens)
        self._pace(p)
        if p["type"] == "gemini":
            return self._gemini(p, step, system, prompt, max_tokens)
        if p["type"] == "anthropic":
            return self._anthropic(p, step["model"], system, prompt, max_tokens)
        return self._openai(p, step, system, prompt, max_tokens)

    @staticmethod
    def _limit_or_error(r, name: str) -> None:
        if r.status_code == 429:
            raise AILimit(r.text[:2000] + json.dumps(dict(r.headers))[:500])
        if r.status_code >= 400:
            raise RuntimeError(f"{name} {r.status_code}: {r.text[:200]}")

    def _gemini(self, p, step, system, prompt, max_tokens):
        # Gemini counts its hidden "thinking" against the output limit, so keep room for it.
        gen = {"maxOutputTokens": max(max_tokens, int(step.get("min_output_tokens", p.get("min_output_tokens", 1024)))),
               "temperature": 0.2, **(p.get("generation") or {}), **(step.get("generation") or {})}
        body: dict = {"contents": [{"role": "user", "parts": [{"text": prompt}]}], "generationConfig": gen}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        r = requests.post(f"{p['base_url']}/models/{step['model']}:generateContent",
                          headers={"x-goog-api-key": p["key"] or ""}, json=body, timeout=180)
        self._limit_or_error(r, p["name"])
        d = r.json()
        cands = d.get("candidates") or []
        if not cands:
            raise RuntimeError(f"gemini returned no answer: {str(d.get('promptFeedback'))[:150]}")
        text = "".join(part.get("text", "") for part in (cands[0].get("content") or {}).get("parts", [])
                       if not part.get("thought"))
        u = d.get("usageMetadata") or {}
        tout = int(u.get("candidatesTokenCount", 0)) + int(u.get("thoughtsTokenCount", 0))
        if not text.strip():
            raise RuntimeError(f"gemini gave an empty answer (finish: {cands[0].get('finishReason')})")
        return text, int(u.get("promptTokenCount", len(prompt) // 4)), tout

    def _openai(self, p, step, system, prompt, max_tokens):
        messages = ([{"role": "system", "content": system}] if system else []) + \
                   [{"role": "user", "content": prompt}]
        headers = {"Authorization": f"Bearer {p['key']}"} if p.get("key") else {}
        # Reasoning models (e.g. gpt-oss) spend part of max_tokens thinking: keep room.
        floor = int(step.get("min_output_tokens", p.get("min_output_tokens", 0)) or 0)
        body = {"model": step["model"], "messages": messages,
                "max_tokens": max(max_tokens, floor), "temperature": 0.2,
                **(p.get("extra") or {}), **(step.get("extra") or {})}
        r = requests.post(f"{p['base_url']}/chat/completions", headers=headers, timeout=180, json=body)
        self._limit_or_error(r, p["name"])
        d = r.json()
        text = d["choices"][0]["message"].get("content") or ""
        u = d.get("usage") or {}
        if not text.strip():
            raise RuntimeError(f"{p['name']} gave an empty answer "
                               f"(finish: {d['choices'][0].get('finish_reason')}); raise min_output_tokens")
        return text, int(u.get("prompt_tokens", len(prompt) // 4)), int(u.get("completion_tokens", len(text) // 4))

    def _anthropic(self, p, model, system, prompt, max_tokens):
        body = {"model": model, "max_tokens": max_tokens, "messages": [{"role": "user", "content": prompt}]}
        if system:
            body["system"] = system
        r = requests.post("https://api.anthropic.com/v1/messages", json=body, timeout=180,
                          headers={"x-api-key": p.get("key") or "", "anthropic-version": "2023-06-01"})
        self._limit_or_error(r, p["name"])
        d = r.json()
        text = "".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text")
        u = d.get("usage", {})
        return text, int(u.get("input_tokens", 0)), int(u.get("output_tokens", 0))

    # ------------------------------------------------------------- status
    def report(self) -> list[str]:
        lines = []
        for name, p in self.providers.items():
            if not p["usable"]:
                lines.append(f"{name:<11} off" + ("" if not p.get("enabled", True) else " (no API key in .env)"))
                continue
            cap = int(p.get("daily_tokens") or 0)
            lines.append(f"{name:<11} {self.used_today(name):,} tokens today" + (f" of {cap:,}" if cap else ""))
        for purpose, steps in self.routes.items():
            parts = []
            for s in steps or []:
                p = self.providers.get(s.get("provider"))
                if not p or not p["usable"]:
                    continue
                until, reason = self.paused_until(p["name"], s["model"])
                state = "ready" if until <= time.time() else \
                    f"paused until {dt.datetime.fromtimestamp(until, dt.timezone.utc):%m-%d %H:%M} UTC"
                parts.append(f"{p['name']}/{s['model']} [{state}]")
            lines.append(f"route {purpose:<13} " + (" -> ".join(parts) or "no usable model"))
        return lines