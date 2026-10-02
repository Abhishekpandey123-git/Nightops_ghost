#!/usr/bin/env python3
"""Bring your config.yaml up to date with a newer nightops version, without
touching your own settings.

    python deploy/merge_config.py /path/to/new/config.yaml

  * Sections that are NEW in the newer version (e.g. `automations:`) are added.
  * The `ai:` section is replaced only if yours is the old single-provider kind.
  * Everything else (repos, Telegram, jobs, scout...) stays exactly as you set it.

A backup is written to config.yaml.before-merge first.
"""
import os
import re
import shutil
import sys

import yaml

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGET = os.path.join(HERE, "config.yaml")
HEADER = "# ---------------------------------------------------------------------\n"


def sections(text: str) -> dict[str, tuple[int, int, int]]:
    """Top-level key -> (block_start, key_start, block_end).
    block_start includes the column-0 comment lines directly above the key."""
    lines = text.split("\n")
    offs, pos = [], 0
    for ln in lines:
        offs.append(pos)
        pos += len(ln) + 1
    keys = [i for i, ln in enumerate(lines) if re.match(r"^[a-z_]+:", ln)]
    starts = []
    for i in keys:
        j = i
        while j > 0 and (lines[j - 1].startswith("#") or not lines[j - 1].strip()):
            j -= 1
        while j < i and not lines[j].strip():          # don't swallow leading blank lines
            j += 1
        starts.append(j)
    out = {}
    for n, i in enumerate(keys):
        end = offs[starts[n + 1]] if n + 1 < len(keys) else len(text)
        out[lines[i].split(":")[0]] = (offs[starts[n]], offs[i], end)
    return out


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    new = open(sys.argv[1], encoding="utf-8").read().replace("\r\n", "\n")
    old = open(TARGET, encoding="utf-8").read().replace("\r\n", "\n")
    new_cfg, old_cfg = yaml.safe_load(new), yaml.safe_load(old)
    ns, merged, changes = sections(new), old, []

    old_ai = (old_cfg or {}).get("ai") or {}
    if "ai" in ns and "ai" in (old_cfg or {}) and "providers" not in old_ai:
        _, ok, oe = sections(merged)["ai"]
        _, nk, ne = ns["ai"]
        merged = merged[:ok] + new[nk:ne].rstrip("\n") + "\n\n" + merged[oe:].lstrip("\n")
        changes.append("ai (upgraded to multiple providers)")

    for key, (bs, _, be) in ns.items():
        if key not in (old_cfg or {}):
            merged = merged.rstrip("\n") + "\n\n" + new[bs:be].strip("\n") + "\n"
            changes.append(f"{key} (new)")

    result = yaml.safe_load(merged)                           # never write something invalid
    for key in (old_cfg or {}):
        if key != "ai" and result.get(key) != old_cfg.get(key):
            print(f"refusing: '{key}' would change; config.yaml left untouched")
            return 1
    if not changes:
        print("config.yaml is already up to date")
        return 0
    shutil.copy2(TARGET, TARGET + ".before-merge")
    with open(TARGET, "w", encoding="utf-8") as fh:
        fh.write(merged)
    print("updated:", ", ".join(changes))
    print("kept as you set them:", ", ".join(k for k in old_cfg if k != "ai"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
