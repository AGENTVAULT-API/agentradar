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
import importlib.util
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
ALERT_PATH = ROOT / "AGENTRADAR_ALERTS.md"
OPPORTUNITY_LATEST_PATH = LOG_DIR / "agentradar_recommended_opportunities_latest.json"

# TaskMarket tasks already handled by this mission. The monitor excludes them so
# a stale pending submission cannot hide a genuinely new actionable item.
EXCLUDED_TASK_IDS = {
    "0xe9fb8fe4e6f83b54d4850efd1c5b6aef6d1bbd7f9f91921d4329f404e64c5682",  # Alluviance/Product Zero
    "0x8e416ba0f3e473d2dddc7f7afc03ca35ab12b95972818808e9eff0d1e98e31fb",  # AgentKit integration
    "0x21cc30011dddb8c7a5e91b4c70c140defab447507169513745d0389572255a42",  # AgentKit integration
}


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


def _load_health_module():
    spec = importlib.util.spec_from_file_location("agentradar_health", ROOT / "api" / "health.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load AgentRadar health module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def opportunity_digest(opportunities: list[dict]) -> list[dict]:
    """Stable, compact identity for recommended opportunities.

    We intentionally keep only public metadata. This digest drives change
    detection and alerting; the full latest snapshot is written separately.
    """
    digest = []
    for item in opportunities:
        if not isinstance(item, dict):
            continue
        digest.append({
            "platform": item.get("platform"),
            "task_id": item.get("task_id") or item.get("id"),
            "title": item.get("title"),
            "url": item.get("url"),
            "reward": item.get("reward"),
            "currency": item.get("currency"),
        })
    return sorted(digest, key=lambda row: (str(row.get("platform")), str(row.get("task_id")), str(row.get("url"))))


def recommended_opportunity_snapshot(health_module=None) -> dict:
    """Run the live opportunity scorer and return recommended-only metadata."""
    health = health_module or _load_health_module()
    payload = health._live_opportunities(exclude_task_ids=EXCLUDED_TASK_IDS)  # noqa: SLF001 - same owned project
    recommended = payload.get("recommended_opportunities") or []
    digest = opportunity_digest(recommended)
    return {
        "checked_at": utc_now(),
        "generated_at": payload.get("generated_at"),
        "recommended_count": payload.get("recommended_count", len(recommended)),
        "recommended_for_autonomous_action": bool(payload.get("recommended_for_autonomous_action")),
        "recommended_opportunities": recommended,
        "source_errors": payload.get("source_errors", {}),
        "digest": digest,
        "digest_hash": stable_hash(digest),
    }


def write_opportunity_alert(snapshot: dict, previous_hash: str | None) -> None:
    """Persist a concise alert when a new recommended opportunity appears."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    OPPORTUNITY_LATEST_PATH.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if not snapshot.get("recommended_for_autonomous_action"):
        return
    if previous_hash == snapshot.get("digest_hash"):
        return

    lines = [
        "# AgentRadar recommended opportunity alert",
        "",
        f"Updated: {snapshot.get('checked_at')}",
        f"Recommended count: {snapshot.get('recommended_count')}",
        "",
    ]
    for item in snapshot.get("digest", []):
        lines.append(
            "- {platform}: {title} | reward={reward} {currency} | task={task_id} | {url}".format(
                platform=item.get("platform"),
                title=item.get("title"),
                reward=item.get("reward"),
                currency=item.get("currency"),
                task_id=item.get("task_id"),
                url=item.get("url"),
            )
        )
    lines.append("")
    ALERT_PATH.write_text("\n".join(lines), encoding="utf-8")


def run_once() -> int:
    before = platform_digest(load_json(DATA, {}))
    previous_state = load_json(STATE_PATH, {})
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
    opportunity_snapshot = recommended_opportunity_snapshot()
    previous_opportunity_hash = previous_state.get("recommended_opportunity_digest_hash")
    write_opportunity_alert(opportunity_snapshot, previous_opportunity_hash)
    state = {
        "last_run_utc": utc_now(),
        "platform_count": len(after),
        "digest_hash": stable_hash(after),
        "data_generated_at": after_data.get("generated_at"),
        "last_evidence_log": str(evidence),
        "last_changes": changes,
        "recommended_count": opportunity_snapshot.get("recommended_count", 0),
        "recommended_for_autonomous_action": opportunity_snapshot.get("recommended_for_autonomous_action", False),
        "recommended_opportunity_digest_hash": opportunity_snapshot.get("digest_hash"),
        "recommended_opportunity_latest": str(OPPORTUNITY_LATEST_PATH),
    }
    STATE_PATH.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")

    if changes:
        log(f"refreshed {len(after)} platforms with {len(changes)} status/probe changes; evidence={evidence}")
        for name, change in changes.items():
            log(f"change {name}: {change['before']} -> {change['after']}")
    else:
        log(f"refreshed {len(after)} platforms; no status/probe changes; evidence={evidence}")
    if opportunity_snapshot.get("recommended_for_autonomous_action"):
        log(
            "recommended opportunities available: count=%s alert=%s"
            % (opportunity_snapshot.get("recommended_count"), ALERT_PATH)
        )
    else:
        log("recommended opportunity scan: none currently actionable")
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
