"""Config + .env loading. Everything runs relative to the project folder."""
from __future__ import annotations

import os

import yaml

from .util import register_secret

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_dotenv(path: str) -> None:
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def load(path: str | None = None) -> dict:
    os.chdir(BASE)
    load_dotenv(os.path.join(BASE, ".env"))
    with open(path or os.path.join(BASE, "config.yaml"), "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    # Register every secret we know about so logs never print them.
    for name in ("GITHUB_TOKEN", "AI_API_KEY", "GEMINI_API_KEY", "TELEGRAM_BOT_TOKEN", "SMTP_PASSWORD",
                 "NIGHTOPS_PASSWORD_HASH", "NIGHTOPS_TOTP_SECRET", "GEMINI_API_KEY",
                 "GROQ_API_KEY", "OPENROUTER_API_KEY"):
        register_secret(os.environ.get(name))
    return cfg


def env(name: str | None) -> str | None:
    return os.environ.get(name) if name else None


def own_repos(cfg: dict) -> set[str]:
    return {r.lower() for r in (cfg.get("github", {}).get("own_repos") or [])}


def set_env_var(key: str, value: str, path: str | None = None) -> None:
    """Create or update KEY=value in .env, keeping other lines. chmod 600."""
    path = path or os.path.join(BASE, ".env")
    lines = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    out, found = [], False
    for line in lines:
        if line.split("=", 1)[0].strip() == key:
            out.append(f"{key}={value}")
            found = True
        else:
            out.append(line)
    if not found:
        out.append(f"{key}={value}")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out) + "\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    os.environ[key] = value
