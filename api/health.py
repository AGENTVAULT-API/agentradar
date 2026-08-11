"""
AgentRadar API — /api/status and /api/opportunities (Vercel Serverless, WSGI)
============================================================================
Live status/health tracker for AI-agent-earning platforms.

Endpoints:
  GET /api/status         -> current contents of platform_data.json plus counts
  GET /api/opportunities  -> live public read-only opportunity feed

The opportunities endpoint uses only unauthenticated public GET requests. It
never uses cookies, secrets, wallet signatures, submissions, or paid actions.
"""

import datetime
import json
import os
import urllib.parse
import urllib.request

_DATA_PATH = os.path.join(os.path.dirname(__file__), "..", "platform_data.json")
_HTTP_TIMEOUT_S = 8
_USER_AGENT = "AgentRadar/0.2 (+https://agentradar-three.vercel.app)"

_CORS_HEADERS = [
    ("Access-Control-Allow-Origin", "*"),
    ("Access-Control-Allow-Methods", "GET, OPTIONS"),
    ("Access-Control-Allow-Headers", "Content-Type"),
    ("Access-Control-Max-Age", "86400"),
]


def _load_platform_data():
    try:
        with open(_DATA_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (IOError, json.JSONDecodeError) as e:
        return {"error": "Failed to load platform_data.json: %s" % e, "platforms": []}


def _enrich_status(data):
    """Add stable top-level summary fields for API consumers."""
    if not isinstance(data, dict):
        return {"error": "Invalid platform data", "platforms": [], "platform_count": 0, "status_counts": {}}

    platforms = data.get("platforms")
    if not isinstance(platforms, list):
        platforms = []
        data = dict(data)
        data["platforms"] = platforms

    status_counts = {}
    for platform in platforms:
        if not isinstance(platform, dict):
            continue
        status = platform.get("status") or "unknown"
        status_counts[status] = status_counts.get(status, 0) + 1

    enriched = dict(data)
    enriched["platform_count"] = len(platforms)
    enriched["status_counts"] = dict(sorted(status_counts.items()))
    return enriched


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fetch_json(url):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": _USER_AGENT, "Accept": "application/json, */*"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:
            body = resp.read(1_500_000)
            return json.loads(body.decode("utf-8")), None
    except Exception as exc:  # pragma: no cover - live network failures vary
        return None, str(exc)[:300]


def _first(*values):
    for value in values:
        if value not in (None, "", []):
            return value
    return None


def _taskmarket_opportunities(limit=20):
    query = urllib.parse.urlencode({"status": "open", "limit": str(limit)})
    payload, error = _fetch_json("https://taskmarket.dev/api/tasks?" + query)
    items = []
    if error:
        return items, error

    if isinstance(payload, dict):
        data = payload.get("data")
        tasks = _first(payload.get("tasks"), data.get("tasks") if isinstance(data, dict) else None, data, payload.get("entries"), [])
    else:
        tasks = payload or []

    for task in tasks[:limit]:
        if not isinstance(task, dict):
            continue
        task_id = task.get("id") or task.get("taskId")
        desc = (task.get("description") or task.get("taskDescription") or "").replace("\n", " ")
        items.append({
            "platform": "TaskMarket",
            "title": desc[:96] or "Untitled TaskMarket task",
            "url": "https://taskmarket.dev/task/%s" % task_id,
            "task_id": task_id,
            "reward": _first(task.get("netRewardBaseUnits"), task.get("netReward"), task.get("taskReward"), task.get("reward")),
            "currency": "USDC base units",
            "agent_access": "allowed_public_worker_market",
            "submission_count": task.get("submissionCount"),
            "award_count": task.get("awardCount"),
            "status": _first(task.get("status"), task.get("taskStatus")),
            "risk_note": "Requires task-specific quality/legal review before submitting; AgentRadar only lists public opportunities.",
        })
    return items, None


def _superteam_opportunities(limit=50):
    payload, error = _fetch_json("https://earn.superteam.fun/api/listings?take=%d" % limit)
    items = []
    if error:
        return items, error

    listings = payload if isinstance(payload, list) else payload.get("listings", []) if isinstance(payload, dict) else []
    for listing in listings[:limit]:
        if not isinstance(listing, dict) or listing.get("status") != "OPEN":
            continue
        if listing.get("agentAccess") == "HUMAN_ONLY":
            continue
        items.append({
            "platform": "Superteam Earn",
            "title": listing.get("title"),
            "url": "https://earn.superteam.fun/listings/%s" % listing.get("slug"),
            "task_id": listing.get("id"),
            "reward": listing.get("rewardAmount"),
            "currency": listing.get("token"),
            "agent_access": listing.get("agentAccess"),
            "submission_count": (listing.get("_count") or {}).get("Submission"),
            "deadline": listing.get("deadline"),
            "sponsor": (listing.get("sponsor") or {}).get("name"),
            "risk_note": "Agent-allowed by platform metadata, but many Superteam submissions still require external social/account workflows.",
        })
    return items, None


def _openwork_opportunities(limit=30):
    payload, error = _fetch_json("https://www.openwork.bot/api/missions")
    items = []
    if error:
        return items, error

    missions = payload.get("missions", []) if isinstance(payload, dict) else []
    for mission in missions[:limit]:
        if not isinstance(mission, dict) or mission.get("status") != "open":
            continue
        reward = mission.get("reward")
        if not reward:
            continue
        items.append({
            "platform": "Openwork",
            "title": mission.get("title"),
            "url": "https://www.openwork.bot",
            "task_id": mission.get("id"),
            "reward": reward,
            "currency": "$OPENWORK",
            "agent_access": "unknown_wallet_auth_required",
            "risk_note": "Payment flow not yet verified by AgentRadar; public API confirms listing only.",
        })
    return items, None


def _agentbounties_opportunities(limit=20):
    """Return canonically claimable Agent Bounties items only.

    Agent Bounties mirrors some GitHub issues that are already claimed,
    verification-pending, or temporarily unavailable. For a money-making agent,
    the safe public feed is the canonical `claimable_only=true` endpoint, not a
    GitHub label search. This function is deliberately conservative: an empty
    canonical feed is a valid, non-error result and means "do not claim now".
    """
    payload, error = _fetch_json(
        "https://api.agentbounties.app/v1/base/autonomous-bounties/feed?network=base-mainnet&claimable_only=true"
    )
    items = []
    if error:
        return items, error

    bounties = payload if isinstance(payload, list) else payload.get("bounties", []) if isinstance(payload, dict) else []
    for bounty in bounties[:limit]:
        if not isinstance(bounty, dict):
            continue
        title = _first(bounty.get("title"), bounty.get("goal"), bounty.get("name"), "Claimable Agent Bounties task")
        contract = _first(bounty.get("contract"), bounty.get("contractAddress"), bounty.get("bountyContract"), bounty.get("address"))
        reward = _first(
            bounty.get("solverRewardUsdc"),
            bounty.get("solverReward"),
            bounty.get("rewardUsdc"),
            bounty.get("reward"),
        )
        items.append({
            "platform": "Agent Bounties",
            "title": title,
            "url": _first(
                bounty.get("url"),
                "https://agentbounties.app/earn.html?bountyContract=%s&network=base-mainnet" % contract if contract else "https://agentbounties.app/earn.html",
            ),
            "task_id": _first(bounty.get("id"), contract),
            "contract": contract,
            "reward": reward,
            "currency": "USDC",
            "agent_access": "canonical_claimable_feed",
            "status": _first(bounty.get("lifecycle"), bounty.get("status"), "claimable"),
            "risk_note": "Claimable-only canonical feed item; still verify bond/gas/settlement rules before signing any wallet transaction.",
        })
    return items, None


def _toku_opportunities(limit=20):
    """Return public Toku job-board items with conservative caveats.

    Toku's public job feed currently mixes genuine buyer requests with many
    agent self-promotion posts titled "AVAILABLE: ...". AgentRadar lists them
    because they are real public earning-surface signals, but the scorer flags
    seller-ad posts and crowded bid counts so agents do not confuse them with a
    guaranteed buyer queue.
    """
    payload, error = _fetch_json("https://www.toku.agency/api/agents/jobs?limit=%d" % limit)
    items = []
    if error:
        return items, error

    job_posts = payload.get("jobPosts", []) if isinstance(payload, dict) else []
    for job in job_posts[:limit]:
        if not isinstance(job, dict) or job.get("status") != "OPEN":
            continue
        budget = job.get("budgetCents")
        if not budget:
            continue
        items.append({
            "platform": "Toku.agency",
            "title": job.get("title") or "Untitled Toku job",
            "url": "https://www.toku.agency/agents/jobs",
            "task_id": job.get("id"),
            "reward": budget,
            "currency": "USD cents",
            "agent_access": "public_job_board_api",
            "submission_count": job.get("bidCount"),
            "deadline": job.get("deadline"),
            "category": job.get("category"),
            "risk_note": "Public Toku job feed; many current posts are seller self-advertisements rather than buyer orders, so review title and bid count before bidding.",
        })
    return items, None


def _reward_as_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _quality_first_enrich(items):
    """Add conservative, agent-facing triage metadata.

    AgentRadar should not simply sort by advertised reward. For autonomous
    agents, the highest-value feed item is one that is legal, low/no-spend,
    objectively verifiable, and not already saturated. These heuristics are
    intentionally transparent and conservative; they do not submit anything or
    claim payout probability.
    """
    enriched = []
    for item in items:
        row = dict(item)
        title = (row.get("title") or "").lower()
        platform = row.get("platform")
        blockers = []
        score = 50

        if platform == "TaskMarket":
            score += 15
            submissions = row.get("submission_count") or 0
            if submissions >= 50:
                score -= 35
                blockers.append("high_submission_count")
            elif submissions >= 20:
                score -= 15
                blockers.append("moderate_submission_count")
            if row.get("award_count") == 0:
                blockers.append("no_awards_yet")
            if any(word in title for word in ("photorealistic", "seinfeld", "video", "tv series")):
                score -= 35
                blockers.append("likeness_or_video_generation_risk")
            if "proxywar" in title or "benchmark" in title:
                score -= 30
                blockers.append("benchmark_or_external_result_required")
                blockers.append("proof_or_submission_may_require_spend")
            if "guest task drop" in title or "onboard an established" in title:
                score -= 35
                blockers.append("third_party_authorization_required")
            if "integration" in title or "pull request" in title or "plugin" in title:
                score += 15
                row["quality_fit"] = "strong_if_real_pr_and_tests_exist"
            else:
                row["quality_fit"] = "review_task_requirements_before_work"
        elif platform == "Superteam Earn":
            score -= 15
            blockers.append("external_social_or_account_workflow_likely")
            row["quality_fit"] = "monitor_only_without_user_social_authorization"
        elif platform == "Openwork":
            score -= 20
            blockers.append("wallet_payment_flow_unverified")
            row["quality_fit"] = "monitor_until_reward_and_claim_flow_verified"
        elif platform == "Agent Bounties":
            score += 20
            row["quality_fit"] = "strong_if_canonical_claimable_and_bond_ev_positive"
        elif platform == "Toku.agency":
            score += 5
            title_upper = (row.get("title") or "").strip().upper()
            bids = row.get("submission_count") or 0
            seller_ad_markers = (
                "AVAILABLE",
                "BY AI AGENT",
                "FAST DELIVERY",
                "I WILL ",
                "BY AN AI AGENT",
                "AI AGENT -",
            )
            if any(marker in title_upper for marker in seller_ad_markers):
                score -= 25
                blockers.append("likely_seller_ad_not_buyer_request")
            if bids >= 20:
                score -= 20
                blockers.append("crowded_bid_count")
            elif bids >= 8:
                score -= 10
                blockers.append("moderate_bid_count")
            row["quality_fit"] = "direct_small_paid_work_if_real_buyer_request"
        else:
            row["quality_fit"] = "unknown"

        if _reward_as_float(row.get("reward")) <= 0:
            score -= 20
            blockers.append("zero_or_unknown_reward")

        hard_blockers = {
            "high_submission_count",
            "likeness_or_video_generation_risk",
            "proof_or_submission_may_require_spend",
            "third_party_authorization_required",
            "external_social_or_account_workflow_likely",
            "wallet_payment_flow_unverified",
            "likely_seller_ad_not_buyer_request",
            "crowded_bid_count",
            "zero_or_unknown_reward",
        }
        row["recommended_for_autonomous_action"] = not any(blocker in hard_blockers for blocker in blockers)

        row["quality_first_score"] = max(0, min(100, score))
        row["autonomy_blockers"] = blockers
        row["triage_note"] = (
            "Ranked for probability of legal no/low-spend realized value before deadline; "
            "not a recommendation to submit without task-specific review."
        )
        enriched.append(row)

    return sorted(enriched, key=lambda x: (x.get("quality_first_score", 0), _reward_as_float(x.get("reward"))), reverse=True)


def _live_opportunities(exclude_task_ids=None, source_filter=None):
    exclude_task_ids = set(exclude_task_ids or [])
    requested_sources = set(source_filter or [])
    source_factories = {
        "taskmarket": _taskmarket_opportunities,
        "superteam_earn_agent_allowed": _superteam_opportunities,
        "openwork_rewarded": _openwork_opportunities,
        "agentbounties_claimable": _agentbounties_opportunities,
        "toku_public_jobs": _toku_opportunities,
    }
    if requested_sources:
        source_factories = {name: factory for name, factory in source_factories.items() if name in requested_sources}
    raw_items = []
    errors = {}
    for source, factory in source_factories.items():
        source_items, error = factory()
        raw_items.extend(source_items)
        if error:
            errors[source] = error
    if exclude_task_ids:
        raw_items = [item for item in raw_items if item.get("task_id") not in exclude_task_ids]

    items = _quality_first_enrich(raw_items)
    platform_counts = {}
    for item in items:
        platform = item.get("platform") or "unknown"
        platform_counts[platform] = platform_counts.get(platform, 0) + 1

    recommended_items = [item for item in items if item.get("recommended_for_autonomous_action")]

    return {
        "generated_at": _now_iso(),
        "scope": "public unauthenticated read-only endpoints; no wallet actions, submissions, or spending",
        "ranking_method": "quality_first_score: conservative heuristic favoring legal, no/low-spend, objective, unsaturated opportunities; recommended_for_autonomous_action=false when hard blockers such as spend, social-account, likeness/video, third-party-authorization, or seller-ad risk are detected",
        "opportunity_count": len(items),
        "platform_count": len(platform_counts),
        "platform_counts": dict(sorted(platform_counts.items())),
        "excluded_task_ids": sorted(exclude_task_ids),
        "recommended_count": len(recommended_items),
        "recommended_for_autonomous_action": bool(recommended_items),
        "recommended_opportunities": recommended_items[:5],
        "top_quality_first": items[:5],
        # `items` is the original field. `opportunities` is a stable alias for
        # clients that naturally look for the resource name in the response.
        "items": items,
        "opportunities": items,
        "source_errors": errors,
    }


def _parse_exclude_task_ids(query_string):
    params = urllib.parse.parse_qs(query_string or "")
    excluded = []
    for value in params.get("exclude_task_ids", []):
        excluded.extend([part.strip() for part in value.split(",") if part.strip()])
    return excluded


def _parse_source_filter(query_string):
    """Map optional `platform=` query values to concrete live-opportunity sources.

    This keeps `/api/opportunities?platform=taskmarket` fast and reliable for
    callers who only want TaskMarket: a slow unrelated upstream such as Toku
    should not add `source_errors` or latency to a filtered TaskMarket query.
    Unknown platform names deliberately return an empty filter, preserving the
    historical all-sources behavior instead of surprising callers with no data.
    """
    params = urllib.parse.parse_qs(query_string or "")
    aliases = {
        "taskmarket": "taskmarket",
        "superteam": "superteam_earn_agent_allowed",
        "superteam_earn": "superteam_earn_agent_allowed",
        "superteam earn": "superteam_earn_agent_allowed",
        "openwork": "openwork_rewarded",
        "agentbounties": "agentbounties_claimable",
        "agent bounties": "agentbounties_claimable",
        "agent-bounties": "agentbounties_claimable",
        "toku": "toku_public_jobs",
        "toku.agency": "toku_public_jobs",
    }
    sources = []
    for value in params.get("platform", []) + params.get("source", []):
        for part in value.split(","):
            key = part.strip().lower().replace("_", " ")
            source = aliases.get(key) or aliases.get(key.replace(" ", "_"))
            if source and source not in sources:
                sources.append(source)
    return sources


def handle(method, path, query_string=""):
    if path == "/api/opportunities":
        if method != "GET":
            return {"error": "Method not allowed"}, 405
        return _live_opportunities(
            exclude_task_ids=_parse_exclude_task_ids(query_string),
            source_filter=_parse_source_filter(query_string),
        ), 200
    if path in ("/api/status", "/status", "/"):
        if method != "GET":
            return {"error": "Method not allowed"}, 405
        return _enrich_status(_load_platform_data()), 200
    return {"error": "Not found"}, 404


def app(environ, start_response):
    method = environ.get("REQUEST_METHOD", "GET")
    path = environ.get("PATH_INFO", "/")
    query_string = environ.get("QUERY_STRING", "")

    if method == "OPTIONS":
        start_response("204", _CORS_HEADERS + [("Content-Length", "0")])
        return [b""]

    response, status = handle(method, path, query_string)
    output = json.dumps(response, indent=2).encode("utf-8")
    start_response(
        str(status),
        _CORS_HEADERS + [("Content-Type", "application/json"), ("Content-Length", str(len(output)))],
    )
    return [output]
