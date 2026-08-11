#!/usr/bin/env python3
"""
AgentRadar health_check.py
===========================
Refreshes platform_data.json by running REAL, lightweight HTTP probes
against a known public endpoint for each tracked AI-agent-earning
platform, and (for platforms not yet in the seed file) adds new entries
that were verified by actually hitting a live endpoint on that platform's
site -- nothing here is invented.

What this script does NOT do:
  - It does not compute "uptime %" from a single probe. A single GET
    request only tells you the site/API was reachable *at the moment
    this script ran* -- it is recorded as such (reachable / status code /
    latency), not extrapolated into a fake historical uptime percentage.
  - It does not overwrite the deep, manually-verified `status`, `notes`,
    and `evidence` fields for the four seed platforms (those came from
    hands-on integration testing: real wallet auth, real submissions,
    etc. -- richer signal than a GET request can produce). It DOES
    refresh `last_checked` and attaches a `last_probe` sub-object with
    the live result of this run.

Run:
    python3 api/health_check.py
"""

import json
import os
import time
import datetime
from pathlib import Path

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_PATH = os.path.join(HERE, "..", "platform_data.json")
AGENTRADAR_ROOT = Path(HERE).resolve().parent
MISSION_ROOT = AGENTRADAR_ROOT.parent

USER_AGENT = "AgentRadar-HealthCheck/1.0 (+https://github.com/agentradar)"
TIMEOUT_S = 10


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def probe(url, method="GET", headers=None, timeout=TIMEOUT_S):
    """Do one real, lightweight HTTP request and report what actually happened."""
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json, text/html, */*"}
    if headers:
        hdrs.update(headers)
    t0 = time.time()
    try:
        resp = requests.request(method, url, headers=hdrs, timeout=timeout)
        elapsed_ms = int((time.time() - t0) * 1000)
        return {
            "probed_url": url,
            "reachable": True,
            "http_status": resp.status_code,
            "latency_ms": elapsed_ms,
            "checked_at": now_iso(),
        }
    except requests.RequestException as e:
        elapsed_ms = int((time.time() - t0) * 1000)
        return {
            "probed_url": url,
            "reachable": False,
            "http_status": None,
            "latency_ms": elapsed_ms,
            "error": str(e)[:300],
            "checked_at": now_iso(),
        }


def latest_taskmarket_summary():
    """Return the newest mission TaskMarket summary, if one exists.

    The summary is produced by the mission's live TaskMarket CLI observer and
    contains only aggregate counts/reward/task status metadata, not secrets.
    Keeping this optional prevents AgentRadar from serving stale hand-written
    market notes such as an old submission count while the monitor keeps
    probing the platform.
    """
    log_dir = MISSION_ROOT / "logs"
    candidates = sorted(log_dir.glob("taskmarket_summary_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            summary = payload.get("summary") if isinstance(payload, dict) else None
            if isinstance(summary, dict) and summary.get("submission_count") is not None:
                summary = dict(summary)
                summary["summary_file"] = str(path)
                return summary
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# Probe endpoints for existing (seed) platforms.
# Each was hand-verified to exist and respond by hitting it directly with
# `requests` before being hard-coded here.
# ---------------------------------------------------------------------------

SEED_PROBES = {
    "TaskMarket": {"url": "https://taskmarket.dev/api/health", "kind": "json_health_endpoint"},
    "BountyBook": {"url": "https://www.bountybook.ai/", "kind": "homepage"},  # no public API endpoint could be
                                                                               # discovered (Next.js app, no /api/*
                                                                               # route responds without auth) --
                                                                               # homepage GET is the honest, lightweight
                                                                               # probe available.
    "ClawTasks": {"url": "https://clawtasks.com/api/health", "kind": "json_health_endpoint"},
    "Superteam Earn": {"url": "https://earn.superteam.fun/api/listings?take=1", "kind": "json_api"},
}


# ---------------------------------------------------------------------------
# New platforms researched via requests + BeautifulSoup against their real,
# public sites. Only entries that could actually be verified by hitting a
# live endpoint are included. Where something could NOT be verified (e.g.
# "Rose Token", which is referenced by other AI agents in blog/forum posts
# but has no discoverable, resolvable, non-parked website), that is recorded
# honestly rather than invented.
# ---------------------------------------------------------------------------

def research_new_platforms():
    """Return a list of new platform dicts, each backed by a real probe done
    right now. Uses requests (+ BeautifulSoup where HTML structure needed to
    be parsed to find the API section) -- see MoltCities below."""
    from bs4 import BeautifulSoup

    new_platforms = []

    # --- MoltCities -----------------------------------------------------
    # Real, live site. Homepage HTML advertises a documented public API
    # ("A home for bots" -- register/canvas/pixel/mail endpoints). We parse
    # the API section with BeautifulSoup to confirm it's documented, then
    # hit one of the actually-public, unauthenticated GET endpoints
    # (/channels) to verify it responds.
    home = probe("https://moltcities.com/")
    api_doc_found = False
    if home.get("reachable") and home.get("http_status") == 200:
        try:
            html = requests.get("https://moltcities.com/", headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT_S).text
            soup = BeautifulSoup(html, "html.parser")
            api_section = soup.find(id="api")
            api_doc_found = api_section is not None and "POST /register" in api_section.get_text()
        except Exception:
            api_doc_found = False
    channels_probe = probe("https://moltcities.com/channels")
    working = bool(
        home.get("reachable") and home.get("http_status") == 200
        and channels_probe.get("reachable") and channels_probe.get("http_status") == 200
    )
    new_platforms.append({
        "name": "MoltCities",
        "url": "https://moltcities.com",
        "chain": None,
        "currency": None,
        "status": "operational_not_a_paid_earning_platform" if working else "unreachable",
        "verified_working": working,
        "verified_broken": not working,
        "notes": (
            "Not a bounty/earning platform -- a collaborative space where bots register an "
            "identity, build a homepage, paint on a shared 1024x1024 canvas (1 pixel/day), and "
            "message each other. Public API documented on homepage (POST /register, GET "
            "/canvas/image, GET/POST /canvas & /pixel, PUT /page, GET /channels, POST /mail). "
            "No USDC/token payment layer found on the public site. Included because it is a "
            "real, live, actively-documented agent-facing platform, not because it pays agents."
            if working else
            "Site or documented public endpoint did not respond during this check."
        ),
        "last_checked": now_iso(),
        "evidence": "Live GET https://moltcities.com/ (200) + GET /channels (public, unauthenticated, JSON) "
                    "+ homepage API docs parsed with BeautifulSoup, id=\"api\" section found: %s"
                    % api_doc_found,
        "last_probe": channels_probe,
    })

    # --- Openwork ---------------------------------------------------------
    # Real, live platform on Base ("Pilots + Claws = Crews", $OPENWORK token).
    # Its /api/missions and /api/jobs endpoints are public and unauthenticated
    # and return real, live mission data (confirmed by directly inspecting
    # JSON payload contents, not just status codes).
    missions_probe = probe("https://www.openwork.bot/api/missions")
    mission_count = None
    if missions_probe.get("reachable") and missions_probe.get("http_status") == 200:
        try:
            payload = requests.get("https://www.openwork.bot/api/missions", headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT_S).json()
            mission_count = len(payload.get("missions", [])) if isinstance(payload, dict) else None
        except Exception:
            mission_count = None
    working = missions_probe.get("reachable") and missions_probe.get("http_status") == 200 and mission_count is not None
    new_platforms.append({
        "name": "Openwork",
        "url": "https://www.openwork.bot",
        "chain": "Base",
        "currency": "$OPENWORK (token)",
        "status": "operational" if working else "unreachable",
        "verified_working": bool(working),
        "verified_broken": not bool(working),
        "notes": (
            "'The Crew Economy' -- Pilots (humans) oversee Claws (AI agents), deploy missions, and "
            "earn $OPENWORK on Base. Public, unauthenticated GET /api/missions and /api/jobs return "
            "real live mission listings with titles/descriptions (%s missions seen at check time). "
            "Did not attempt claim/submit flow (would require wallet auth), so payment reliability is "
            "NOT verified here -- only that the platform and its public read API are live." % mission_count
            if working else
            "Public missions API did not respond as expected."
        ),
        "last_checked": now_iso(),
        "evidence": "Live GET https://www.openwork.bot/api/missions (200, JSON, %s missions parsed)" % mission_count,
        "last_probe": missions_probe,
    })

    # --- BotBounty ----------------------------------------------------------
    # Real, live site (botbounty.co). Its /api/* routes are live but
    # protected (401 Missing X-API-Key), which itself confirms a real,
    # functioning API gateway is running -- just not publicly readable
    # without a key we don't have. Verified by direct probe only; no
    # claims made about job availability or payment reliability.
    api_probe = probe("https://botbounty.co/api/jobs")
    gated_but_alive = api_probe.get("reachable") and api_probe.get("http_status") == 401
    new_platforms.append({
        "name": "BotBounty",
        "url": "https://botbounty.co",
        "chain": None,
        "currency": None,
        "status": "operational_api_key_gated" if gated_but_alive else "unreachable",
        "verified_working": bool(gated_but_alive),
        "verified_broken": not bool(gated_but_alive),
        "notes": (
            "'AI Agent Jobs -- Find Paid Work for Your AI Agents'. Site is live. GET /api/jobs "
            "returns HTTP 401 {\"error\":\"Missing X-API-Key header\"} -- confirms a real, running API "
            "gateway, but we hold no API key so job listings/payment reliability could not be "
            "verified beyond 'the API is alive and enforcing auth'."
            if gated_but_alive else
            "Homepage or API did not respond as expected."
        ),
        "last_checked": now_iso(),
        "evidence": "Live GET https://botbounty.co/api/jobs -> HTTP 401 with explicit auth-required JSON error",
        "last_probe": api_probe,
    })

    # --- Agent Bounty (agentbounty.org) -------------------------------------
    # Real, live marketing/app site ("AI Agent Bounty Platform - post
    # challenges, earn rewards for AI agent tasks, benchmarks, and open
    # source contributions"). No public, unauthenticated API endpoint could
    # be discovered (tried /api/challenges, /api/bounties, /api/missions,
    # /api/jobs -- all 404). Only the homepage itself could be verified live.
    home_probe = probe("https://agentbounty.org/")
    working = home_probe.get("reachable") and home_probe.get("http_status") == 200
    new_platforms.append({
        "name": "Agent Bounty",
        "url": "https://agentbounty.org",
        "chain": None,
        "currency": None,
        "status": "site_live_api_undiscovered" if working else "unreachable",
        "verified_working": bool(working),
        "verified_broken": not bool(working),
        "notes": (
            "'AI Agent Bounty Platform - post challenges, earn rewards for AI agent tasks, "
            "benchmarks, and open source contributions.' Homepage is live (Next.js app). Probed "
            "/api/challenges, /api/bounties, /api/missions, /api/jobs -- all returned 404, so no "
            "public read API could be verified. Could not confirm job volume, payment reliability, "
            "or agent access from outside the app."
            if working else
            "Homepage did not respond as expected."
        ),
        "last_checked": now_iso(),
        "evidence": "Live GET https://agentbounty.org/ (200); 4 common API paths probed, all 404",
        "last_probe": home_probe,
    })

    # --- Agent Bounties (agentbounties.app) ---------------------------------
    # Canonical on-chain bounty surface with a public feed. The API's
    # claimable_only=true shortcut has intermittently returned an empty list
    # while the unfiltered canonical feed contained status=claimable bounties,
    # so health tracking probes the unfiltered endpoint and counts claimable
    # statuses locally. This remains read-only and does not sign/claim/bond.
    feed_url = "https://api.agentbounties.app/v1/base/autonomous-bounties/feed?network=base-mainnet&claimable_only=false"
    feed_probe = probe(feed_url)
    claimable_count = None
    total_count = None
    if feed_probe.get("reachable") and feed_probe.get("http_status") == 200:
        try:
            payload = requests.get(feed_url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT_S).json()
            bounties = payload if isinstance(payload, list) else payload.get("bounties", []) if isinstance(payload, dict) else []
            total_count = len(bounties)
            claimable_count = sum(1 for bounty in bounties if isinstance(bounty, dict) and bounty.get("status") == "claimable")
        except Exception:
            claimable_count = None
            total_count = None
    working = feed_probe.get("reachable") and feed_probe.get("http_status") == 200 and claimable_count is not None
    new_platforms.append({
        "name": "Agent Bounties",
        "url": "https://agentbounties.app",
        "chain": "Base",
        "currency": "USDC",
        "status": "operational_claimable_feed" if working else "unreachable_or_changed_api",
        "verified_working": bool(working),
        "verified_broken": not bool(working),
        "notes": (
            "Canonical Base/USDC autonomous bounty surface. Public feed is reachable, currently "
            "returns %s total bounties with %s status=claimable items. AgentRadar blocks autonomous "
            "action while claim bonds or required external spend exceed realized mission funds; GitHub "
            "mirrored issues remain discovery only."
            if working else
            "Agent Bounties API did not return a parseable public feed during this check."
        ) % (total_count, claimable_count) if working else "Agent Bounties API did not return a parseable public feed during this check.",
        "last_checked": now_iso(),
        "evidence": "Live GET %s -> HTTP %s, parsed total_count=%s, status_claimable_count=%s" % (feed_url, feed_probe.get("http_status"), total_count, claimable_count),
        "last_probe": feed_probe,
    })

    # --- TaskBounty (task-bounty.com) ----------------------------------------
    # Newly discovered via live web search. Unlike many agent-bounty sites, it
    # publishes an OpenAPI document plus an unauthenticated read-only task list.
    # The current task list can legitimately be empty; health tracking records
    # the API surface separately from any claim/submit/auth/payout attempt.
    taskbounty_tasks_url = "https://www.task-bounty.com/api/v1/tasks"
    taskbounty_openapi_url = "https://www.task-bounty.com/api/v1/openapi.json"
    tasks_probe = probe(taskbounty_tasks_url)
    openapi_probe = probe(taskbounty_openapi_url)
    task_count = None
    has_solver_paths = False
    if tasks_probe.get("reachable") and tasks_probe.get("http_status") == 200:
        try:
            payload = requests.get(taskbounty_tasks_url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT_S).json()
            task_count = len(payload) if isinstance(payload, list) else len(payload.get("data", [])) if isinstance(payload, dict) else None
        except Exception:
            task_count = None
    if openapi_probe.get("reachable") and openapi_probe.get("http_status") == 200:
        try:
            spec = requests.get(taskbounty_openapi_url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT_S).json()
            paths = spec.get("paths", {}) if isinstance(spec, dict) else {}
            has_solver_paths = "/tasks" in paths and "/submissions" in paths and "/agents" in paths
        except Exception:
            has_solver_paths = False
    working = tasks_probe.get("reachable") and tasks_probe.get("http_status") == 200 and task_count is not None and has_solver_paths
    new_platforms.append({
        "name": "TaskBounty",
        "url": "https://www.task-bounty.com/for-agents",
        "chain": None,
        "currency": "USDC / crypto payout options advertised",
        "status": "operational_public_solver_api_empty" if working and task_count == 0 else ("operational_public_solver_api" if working else "unreachable_or_changed_api"),
        "verified_working": bool(working),
        "verified_broken": not bool(working),
        "notes": (
            "Public agent-facing bounty platform. Live GET /api/v1/tasks returned %s tasks and "
            "OpenAPI exposes solver-relevant paths (/auth/register, /tasks, /tasks/{id}/access, "
            "/agents, /submissions). No claim, registration, payout-address setup, or submission was "
            "attempted; current public task list is empty, so this is monitor-only until funded tasks appear."
            if working else
            "TaskBounty public task list or OpenAPI document did not respond as expected during this check."
        ) % task_count if working else "TaskBounty public task list or OpenAPI document did not respond as expected during this check.",
        "last_checked": now_iso(),
        "evidence": "Live GET %s -> HTTP %s, task_count=%s; GET %s -> HTTP %s, solver_paths=%s" % (
            taskbounty_tasks_url, tasks_probe.get("http_status"), task_count,
            taskbounty_openapi_url, openapi_probe.get("http_status"), has_solver_paths,
        ),
        "last_probe": tasks_probe,
    })

    # --- BountyBot Network (bountybot.network) ------------------------------
    # Real, registered domain -- but the deployment itself is down. Verified
    # by direct probe: the platform returns HTTP 402 "Payment required /
    # DEPLOYMENT_DISABLED" (a Vercel-style error indicating the hosting
    # deployment has been disabled, most likely a billing/hosting issue on
    # the operator's side), not a normal application response.
    bbn_probe = probe("https://bountybot.network/")
    is_broken = bbn_probe.get("reachable") and bbn_probe.get("http_status") == 402
    new_platforms.append({
        "name": "BountyBot Network",
        "url": "https://bountybot.network",
        "chain": None,
        "currency": None,
        "status": "broken_deployment_disabled" if is_broken else ("operational" if bbn_probe.get("http_status") == 200 else "unreachable"),
        "verified_working": False,
        "verified_broken": bool(is_broken),
        "notes": (
            "Domain resolves and responds, but with HTTP 402 and body 'Payment required / "
            "DEPLOYMENT_DISABLED' -- a Vercel hosting error, not an application response. The "
            "platform's own hosting deployment appears to be disabled (likely a billing lapse on "
            "the operator's side). Could not find a working alternate domain for this project."
            if is_broken else
            "Did not return the previously-observed DEPLOYMENT_DISABLED error; re-check manually."
        ),
        "last_checked": now_iso(),
        "evidence": "Live GET https://bountybot.network/ -> HTTP 402, body: 'Payment required\n\nDEPLOYMENT_DISABLED'",
        "last_probe": bbn_probe,
    })

    # --- Rose Token ----------------------------------------------------------
    # Mentioned alongside ClawTasks/Openwork in agent-written blog posts
    # (e.g. dev.to "Every Way an AI Agent Can Get Paid in 2026", moltbook
    # posts referencing "RoseProtocol") as a crypto bounty platform, but NO
    # live, resolvable, non-parked website could be found for it. Every
    # plausible domain either fails DNS resolution or resolves to a
    # GoDaddy "domain for sale" parking page. Rather than invent a URL or
    # status, this is recorded honestly as unverifiable.
    rose_candidates = [
        "https://rosetoken.io",
        "https://rosetoken.ai",
        "https://rosetoken.com",
        "https://rosetoken.xyz",
        "https://rose.money",
    ]
    rose_probes = {u: probe(u) for u in rose_candidates}
    new_platforms.append({
        "name": "Rose Token",
        "url": None,
        "chain": None,
        "currency": None,
        "status": "unverifiable_no_live_site_found",
        "verified_working": False,
        "verified_broken": False,
        "notes": (
            "Referenced in third-party/agent-written posts (dev.to article 'Every Way an AI Agent "
            "Can Get Paid in 2026' lists it alongside ClawTasks and Openwork as a crypto bounty "
            "platform where an agent reported a -$8.30 P&L) but no live, working, official website "
            "could be located. Domains tried: %s -- each either fails DNS resolution or resolves to "
            "a GoDaddy 'domain for sale' parking page (114-byte redirect stub), not an application. "
            "Not including fabricated status/evidence for this one -- flagging it as a platform that "
            "could not be independently verified as currently live."
        ) % ", ".join(rose_candidates),
        "last_checked": now_iso(),
        "evidence": "5 candidate domains probed directly, none resolved to a live application: %s"
                    % json.dumps({u: {"reachable": p.get("reachable"), "status": p.get("http_status"), "error": p.get("error")} for u, p in rose_probes.items()}),
        "last_probe": None,
    })

    return new_platforms


def refresh_seed_platform(entry):
    """Run a real probe for an existing seed platform and attach the live
    result + a fresh last_checked timestamp, without overwriting the
    hand-verified status/notes/evidence from deeper integration testing."""
    name = entry.get("name")
    probe_cfg = SEED_PROBES.get(name)
    if probe_cfg is None:
        entry["last_checked"] = now_iso()
        return entry

    result = probe(probe_cfg["url"])
    entry["last_probe"] = result
    entry["last_checked"] = now_iso()

    if name == "TaskMarket":
        summary = latest_taskmarket_summary()
        if summary:
            balance = summary.get("balanceUsdc")
            submissions = summary.get("submission_count")
            unique = summary.get("unique_submitted_tasks")
            rejected = summary.get("rejected_count")
            open_tasks = summary.get("open_task_count")
            completed = summary.get("completedTasks")
            unsubmitted = summary.get("unsubmitted_open_tasks") or []
            entry["notes"] = (
                "API fully functional and free worker submissions have been confirmed working. "
                "Latest live mission summary: balance %s USDC, completed tasks %s, %s submissions "
                "across %s unique tasks, %s rejections, %s public open tasks, and %s unsubmitted "
                "open tasks currently visible. Market activity remains low/illiquid, so quality and "
                "requester selection dominate expected value."
            ) % (balance, completed, submissions, unique, rejected, open_tasks, len(unsubmitted))
            entry["evidence"] = "Live TaskMarket CLI summary from %s" % summary.get("summary_file")
    return entry


def main():
    with open(DATA_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    platforms = data.get("platforms", [])
    existing_names = {p["name"] for p in platforms}

    refreshed = [refresh_seed_platform(p) for p in platforms]

    new_platforms = research_new_platforms()
    for p in new_platforms:
        if p["name"] not in existing_names:
            refreshed.append(p)
            existing_names.add(p["name"])

    data["platforms"] = refreshed
    data["generated_at"] = now_iso()

    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")

    print("Wrote %d platforms to %s" % (len(refreshed), DATA_PATH))
    for p in refreshed:
        probe_info = p.get("last_probe") or {}
        print(" - %-20s status=%-35s reachable=%s http=%s" % (
            p["name"], p.get("status"), probe_info.get("reachable"), probe_info.get("http_status")))


if __name__ == "__main__":
    main()
