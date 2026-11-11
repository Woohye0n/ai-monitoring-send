"""Anthropic OAuth usage client — the REAL 5-hour / weekly utilization.

This is the same data Claude Code's `/usage` command shows. It is NOT stored on
disk, so we query it live with the account's own OAuth token (read locally from
that account's .credentials.json — the token never leaves the machine it belongs
to; only the resulting percentages are reported).

    GET https://api.anthropic.com/api/oauth/usage
      Authorization: Bearer <accessToken>
      anthropic-beta: oauth-2025-04-20
      User-Agent: claude-code/<version>

Response (2026-07 schema): the legacy window keys (five_hour, seven_day,
seven_day_opus, seven_day_sonnet, ...) are still present but the model-scoped
weekly keys are deprecated — seven_day_opus is now always null.  The current
source of truth is the generic ``limits`` array:

    limits: [{kind: "session"|"weekly_all"|"weekly_scoped",
              percent, resets_at, is_active,
              scope: {model: {display_name: "Fable"}} | null}, ...]

The weekly model-scoped limit (formerly the Opus weekly limit) is reported via
``weekly_scoped`` — currently scoped to Fable — which we normalize to
``seven_day_fable`` so thresholds/labels can target it.

IMPORTANT: this endpoint rate-limits aggressively (429). Call it at most every
~180s per token, with the claude-code User-Agent. Callers must throttle.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
# 리셋권(limit reset grants) 상태는 이 매개변수를 붙여야 실리고, Claude Code CLI 에서 온
# 요청(x-app: cli, claude-cli User-Agent)이어야 "surface" 로 거절되지 않는다. 같은 요청
# 하나에 실리므로 호출 수는 늘지 않는다. skip_spend=1 은 extra_usage 를 빼므로 붙이지 않는다.
# 리셋권을 **쓰는** 요청(POST .../reset_rate_limits)은 이 송신기가 절대 보내지 않는다.
STATUS_QUERY = "?cedar_ember=1"
DEFAULT_VERSION = "2.1.177"


def read_access_token(config_dir):
    """Read the OAuth access token from <config_dir>/.credentials.json."""
    path = os.path.join(os.path.expanduser(config_dir), ".credentials.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f).get("claudeAiOauth", {}).get("accessToken")
    except (OSError, ValueError, KeyError):
        return None


def _scoped_window_key(entry):
    """seven_day_<model> key for a weekly_scoped limits[] entry (or None)."""
    model = (((entry.get("scope") or {}).get("model") or {})
             .get("display_name") or "")
    slug = re.sub(r"[^a-z0-9]+", "_", model.lower()).strip("_")
    if not slug:
        return None
    # "Fable" / "Fable 5" / "Opus 4" all normalize to the model family name so
    # a display-name tweak upstream doesn't silently break threshold matching.
    return "seven_day_" + slug.split("_")[0]


def normalize_usage(data):
    """Normalize a /api/oauth/usage response body to our window dict.

    Shape (matches the codex collector so the dashboard treats them uniformly):
      {five_hour: {utilization, resets_at}, seven_day: {...},
       seven_day_fable: {...}, ..., source: "claude_oauth"}
    Returns None when the body carries no usable window at all.
    """
    out = {"source": "claude_oauth"}

    def put(key, util, resets_at):
        if util is None or key in out:
            return
        out[key] = {"utilization": util, "resets_at": resets_at}

    # 1) legacy top-level window keys (five_hour / seven_day / seven_day_*)
    for key, v in (data or {}).items():
        if key != "five_hour" and not key.startswith("seven_day"):
            continue
        if isinstance(v, dict):
            put(key, v.get("utilization"), v.get("resets_at"))

    # 2) the newer generic `limits` array — fills windows the legacy keys no
    #    longer carry (the weekly model-scoped limit, formerly Opus and now
    #    reported for Fable, only exists here).
    for entry in (data or {}).get("limits") or []:
        if not isinstance(entry, dict) or entry.get("percent") is None:
            continue
        kind = entry.get("kind")
        if kind == "session":
            key = "five_hour"
        elif kind == "weekly_all":
            key = "seven_day"
        elif kind == "weekly_scoped":
            key = _scoped_window_key(entry)
        else:
            key = None
        if key:
            put(key, entry.get("percent"), entry.get("resets_at"))

    return out if len(out) > 1 else None


def normalize_credits(data):
    """Extra-usage (paid credits) state from a /api/oauth/usage body.

    The body carries no balance — only whether extra usage is on, why it is
    off (e.g. "out_of_credits"), what was spent and the monthly cap. Returned
    as the account's ``credits`` field, separate from the window dict.
    """
    eu = (data or {}).get("extra_usage")
    if not isinstance(eu, dict):
        return None
    keep = ("is_enabled", "disabled_reason", "used_credits", "monthly_limit",
            "utilization", "currency", "spend_limit_reached", "credits_ever_enabled")
    return {k: eu.get(k) for k in keep}


def normalize_limit_resets(data):
    """리셋권 상태 (없으면 None). grants 는 남은 횟수·기한·지금 쓸 수 있는지만 남긴다."""
    ce = (data or {}).get("cedar_ember")
    if not isinstance(ce, dict):
        return None
    keep = ("id", "label", "type", "resets_total", "resets_left", "starts_at", "ends_at",
            "clears", "paused", "usable_now", "unavailable_reason")
    return {"eligible": ce.get("eligible"), "ineligible_reason": ce.get("ineligible_reason"),
            "grants": [{k: g.get(k) for k in keep}
                       for g in ce.get("grants") or [] if isinstance(g, dict)]}


def fetch_usage(access_token, version=DEFAULT_VERSION, timeout=20):
    """Return ``(rate_limits, error)`` for this token.

    ``rate_limits`` is the normalized window dict (or None). ``error`` is None
    on success, else one of:
      "no_token"     - nothing to send
      "unauthorized" - 401/403: token revoked, logged out, or the account is
                       suspended/disabled (Claude Code itself can't fetch either)
      "rate_limited" - 429: throttled; says nothing about the account
      "http_<code>"  - other HTTP error
      "network"      - connection / parse failure
      "empty"        - HTTP 200 but no usable window in the body
    """
    if not access_token:
        return None, "no_token"
    req = urllib.request.Request(USAGE_URL + STATUS_QUERY, headers={
        "Authorization": f"Bearer {access_token}",
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": f"claude-cli/{version} (external, cli)",
        "x-app": "cli",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return None, "unauthorized"
        if e.code == 429:
            return None, "rate_limited"
        return None, f"http_{e.code}"
    except (urllib.error.URLError, ValueError, OSError):
        return None, "network"

    out = normalize_usage(data)
    if out is None:
        return None, "empty"
    credits = normalize_credits(data)
    resets = normalize_limit_resets(data)
    if resets is not None:
        # 리셋권도 계정 상태라 크레딧 줄에 함께 싣는다(중앙·집계가 이미 그대로 옮긴다).
        credits = dict(credits or {}, limit_resets=resets)
    if credits is not None:
        # Rides along in the window dict and is split off by the collector, so
        # fetch_usage keeps its (rate_limits, error) shape.
        out["_credits"] = credits
    return out, None
