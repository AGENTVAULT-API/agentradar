#!/usr/bin/env python3
"""AgentRadar local monitor.

Runs the real AgentRadar probe script on a schedule, records verified platform
status snapshots, and emits a small change log. It deliberately does not post,
submit, or spend anything; it only keeps the owned AgentRadar data fresh and
surfaces recovery/breakage signals for the profit loop.

Usage:
  python3 monitor.py --once
  python3 monitor.py --interval 1800
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "platform_data.json"
HEALTH_CHECK = ROOT / "api" / "health_check.py"
LOG_DIR = ROOT.parent / "logs"
STATE_PATH = ROOT / "monitor_state.json"
LOG_PATH = ROOT / "monitor.log"


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(line: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    msg = f"[{utc_now()}] {line}"
    print(msg, flush=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(msg + "\n")


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def platform_digest(data: dict) -> dict:
    platforms = data.get("platforms", []) if isinstance(data, dict) else []
    out = {}
    for p in platforms:
        if not isinstance(p, dict):
            continue
        probe = p.get("last_probe") or {}
        out[p.get("name", "?")] = {
            "status": p.get("status"),
            "verified_working": p.get("verified_working"),
            "verified_broken": p.get("verified_broken"),
            "reachable": probe.get("reachable"),
            "http_status": probe.get("http_status"),
        }
    return out


def stable_hash(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def run_once() -> int:
    before = platform_digest(load_json(DATA, {}))
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S_CEST")
    evidence = LOG_DIR / f"agentradar_health_check_{stamp}.log"

    proc = subprocess.run(
        [sys.executable, str(HEALTH_CHECK)],
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        timeout=120,
    )
    evidence.write_text(
        "COMMAND: %s\nEXIT: %s\n\nSTDOUT:\n%s\n\nSTDERR:\n%s\n"
        % ([sys.executable, str(HEALTH_CHECK)], proc.returncode, proc.stdout, proc.stderr),
        encoding="utf-8",
    )
    if proc.returncode != 0:
        log(f"health_check failed rc={proc.returncode}; evidence={evidence}")
        return proc.returncode

    after_data = load_json(DATA, {})
    after = platform_digest(after_data)
    changes = {name: {"before": before.get(name), "after": val} for name, val in after.items() if before.get(name) != val}
    state = {
        "last_run_utc": utc_now(),
        "platform_count": len(after),
        "digest_hash": stable_hash(after),
        "data_generated_at": after_data.get("generated_at"),
        "last_evidence_log": str(evidence),
        "last_changes": changes,
    }
    STATE_PATH.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")

    if changes:
        log(f"refreshed {len(after)} platforms with {len(changes)} status/probe changes; evidence={evidence}")
        for name, change in changes.items():
            log(f"change {name}: {change['before']} -> {change['after']}")
    else:
        log(f"refreshed {len(after)} platforms; no status/probe changes; evidence={evidence}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="run one refresh and exit")
    parser.add_argument("--interval", type=int, default=1800, help="seconds between refreshes")
    args = parser.parse_args()

    if args.once:
        return run_once()
    log(f"agentradar monitor started interval={args.interval}s")
    while True:
        try:
            run_once()
        except Exception as e:
            log(f"monitor exception: {type(e).__name__}: {e}")
        time.sleep(max(60, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
