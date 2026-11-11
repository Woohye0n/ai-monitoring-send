#!/usr/bin/env python3
"""크레딧 잔량이 배치까지 실려 가는지 검증한다 (stdlib 만 사용).

    python3 tests/test_credits.py

Codex 는 주간 한도를 다 쓰면 유료 크레딧으로 넘어가 계속 돈다(2026-10-06 계정 1:
16시간에 약 13,000 크레딧). 그 잔액은 rollout 의 rate_limits.credits 에 있었는데
송신기가 버리고 있었다. Claude 는 사용량 API 의 extra_usage 에 상태만 있다(잔액 없음).
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from sender import claude_usage                                  # noqa: E402
from sender.codex_collector import (CREDITS_SLOT, CodexCollector,  # noqa: E402
                                    _norm_codex_credits)
from sender.main import _dedupe_accounts                         # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {label}")
    if not ok:
        print(f"       기대 {want!r}\n       실제 {got!r}")
        failures.append(label)


print("[1] Codex 크레딧 값 읽기")
check("잔액 문자열 -> 숫자",
      _norm_codex_credits({"credits": {"has_credits": True, "unlimited": False,
                                       "balance": "49431.6477740000"}}),
      {"balance": 49431.647774, "has_credits": True, "unlimited": False})
check("크레딧 없음은 0",
      _norm_codex_credits({"credits": {"has_credits": False, "unlimited": False, "balance": "0"}}),
      {"balance": 0.0, "has_credits": False, "unlimited": False})
check("잔액 없이 has_credits 만 있으면 버린다(아는 잔액을 덮지 않게)",
      _norm_codex_credits({"credits": {"has_credits": True, "balance": None}}), None)
check("credits 가 없으면 None", _norm_codex_credits({"plan_type": "pro"}), None)

print("\n[2] Codex: 가장 최근 잔액이 account.credits 로 나간다")
SID = "019f7e79-0476-7053-9bb5-1073844c1201"
home = tempfile.mkdtemp(prefix="codex-credits-")
try:
    path = os.path.join(home, "sessions", "2026", "10", "06", f"rollout-x-{SID}.jsonl")
    os.makedirs(os.path.dirname(path))

    def turn(ts, balance):
        return {"type": "event_msg", "timestamp": ts, "payload": {
            "type": "token_count",
            "rate_limits": {"plan_type": "pro",
                            "secondary": {"used_percent": 100.0, "window_minutes": 10080,
                                          "resets_at": 1791000000},
                            "credits": {"has_credits": True, "unlimited": False,
                                        "balance": balance}},
            "info": {"last_token_usage": {"input_tokens": 10, "output_tokens": 1,
                                          "cached_input_tokens": 0}}}}
    with open(path, "w") as f:
        for row in ({"type": "session_meta", "payload": {"id": SID, "cwd": "/w"}},
                    turn("2026-10-06T12:11:00.000Z", "60118.69"),
                    turn("2026-10-06T16:03:00.000Z", "55567.07"),
                    turn("2026-10-06T16:04:00.000Z", None)):     # 잔액 없는 이벤트
            f.write(json.dumps(row) + "\n")
    c = CodexCollector(home, host="t", bootstrap_days=0)
    _meta, _rows, wins, _seq = c._parse_rollout(path, "lab@x.com", attributable_after_ms=1)
    check("가장 최근 '잔액 있는' 이벤트", wins[CREDITS_SLOT][1]["balance"], 55567.07)
    check("주간 창은 그대로 rate_limits 쪽", wins["seven_day"][1]["utilization"], 100.0)
finally:
    shutil.rmtree(home, ignore_errors=True)

print("\n[3] Claude extra_usage")
body = {"five_hour": {"utilization": 3.0, "resets_at": "x"},
        "extra_usage": {"is_enabled": False, "monthly_limit": None, "used_credits": 0.0,
                        "utilization": None, "currency": "USD", "decimal_places": 2,
                        "disabled_reason": "out_of_credits", "user_disabled": False,
                        "spend_limit_reached": False, "credits_ever_enabled": True}}
cr = claude_usage.normalize_credits(body)
check("꺼짐 + 사유", (cr["is_enabled"], cr["disabled_reason"]), (False, "out_of_credits"))
check("창 dict 에는 섞이지 않는다", "extra_usage" in (claude_usage.normalize_usage(body) or {}), False)

print("\n[4] 같은 계정이 여러 디렉토리에 있으면 가장 최근 잔액")
a = {"provider": "codex", "email": "e", "account_id": "1", "rate_limits": {"x": 1},
     "rate_limits_updated_at": 10, "credits": {"balance": 1.0, "observed_at": 5}}
b = {"provider": "codex", "email": "e", "account_id": "1",
     "credits": {"balance": 2.0, "observed_at": 9}}
out = _dedupe_accounts([a, b])
check("카드 하나", len(out), 1)
check("한도는 한도가 있는 쪽, 잔액은 더 최근 쪽",
      (out[0]["rate_limits"], out[0]["credits"]["balance"]), ({"x": 1}, 2.0))

print("\n[5] Claude 리셋권 (cedar_ember)")
grant = {"id": "launch-2610-fable", "label": "One Fable reset", "resets_total": 1, "resets_left": 1,
         "starts_at": "2026-11-09T18:00:00+00:00", "ends_at": "2027-03-09T07:59:00+00:00",
         "clears": ["seven_day_overage_included"], "paused": False, "usable_now": True,
         "percent_used": {"seven_day": 89}, "type": "full", "unavailable_reason": None}
body2 = dict(body, cedar_ember={"eligible": True, "ineligible_reason": None, "grants": [grant],
                                "event_props": {"tier": "claude_max_20x"}})
lr = claude_usage.normalize_limit_resets(body2)
check("남은 횟수·기한·사용 가능", (lr["eligible"], lr["grants"][0]["resets_left"],
                              lr["grants"][0]["ends_at"], lr["grants"][0]["usable_now"]),
      (True, 1, "2027-03-09T07:59:00+00:00", True))
check("사용률·계정 속성은 싣지 않는다", ("percent_used" in lr["grants"][0], "event_props" in lr), (False, False))
check("cedar_ember 가 없으면 None", claude_usage.normalize_limit_resets(body), None)


class _Resp:
    def __init__(self, data):
        self.data = json.dumps(data).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, *a):
        return self.data


seen = {}


def _fake_urlopen(req, timeout=None):
    seen["url"], seen["headers"] = req.full_url, dict(req.header_items())
    return _Resp(body2)


real = claude_usage.urllib.request.urlopen
claude_usage.urllib.request.urlopen = _fake_urlopen
try:
    rl, err = claude_usage.fetch_usage("tok", version="2.1.319")
finally:
    claude_usage.urllib.request.urlopen = real
check("리셋권 상태를 같은 요청으로 받는다", seen["url"].endswith("/api/oauth/usage?cedar_ember=1"), True)
check("CLI 요청으로 보낸다", (seen["headers"].get("X-app"), seen["headers"].get("User-agent")),
      ("cli", "claude-cli/2.1.319 (external, cli)"))
check("크레딧 줄에 리셋권이 함께 실린다",
      (rl["_credits"]["is_enabled"], rl["_credits"]["limit_resets"]["grants"][0]["id"], err),
      (False, "launch-2610-fable", None))

print()
if failures:
    print(f"실패 {len(failures)}건: {', '.join(failures)}")
    sys.exit(1)
print("전부 통과")
