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

print("\n[6] 사용량 요청에 싣는 Claude Code 버전")
real_dir = claude_usage.INSTALLED_VERSIONS_DIR
vdir = tempfile.mkdtemp()
for v in ("2.1.300", "2.1.327", "not-a-version"):
    os.makedirs(os.path.join(vdir, v))
claude_usage.INSTALLED_VERSIONS_DIR = vdir
try:
    check("세션 기록의 옛 버전보다 설치된 새 버전", claude_usage.newest_version("2.1.234"), "2.1.327")
    check("세션 기록이 더 새로우면 그것", claude_usage.newest_version("2.1.400"), "2.1.400")
finally:
    claude_usage.INSTALLED_VERSIONS_DIR = real_dir
claude_usage.INSTALLED_VERSIONS_DIR = os.path.join(vdir, "없음")
try:
    check("설치 폴더가 없으면 기본값 이상", claude_usage.newest_version("2.1.177"), claude_usage.DEFAULT_VERSION)
finally:
    claude_usage.INSTALLED_VERSIONS_DIR = real_dir
shutil.rmtree(vdir, ignore_errors=True)

print("\n[7] Codex 리셋권")
from sender import codex_collector as cc   # noqa: E402
import base64 as _b64   # noqa: E402
import time as _time    # noqa: E402

body3 = {"credits": [
    {"id": "R1", "status": "available", "is_supported_by_plan": True, "title": "Full reset",
     "granted_at": "2026-10-31T19:52:27Z", "expires_at": "2026-11-30T19:52:27Z", "redeemed_at": None},
    {"id": "R2", "status": "redeemed", "is_supported_by_plan": True, "title": "Full reset",
     "granted_at": "2026-10-01T00:00:00Z", "expires_at": "2026-10-31T00:00:00Z",
     "redeemed_at": "2026-10-20T00:00:00Z"}]}
n3 = cc.norm_reset_credits(body3)
check("남은 것·쓴 것", [(g["id"], g["resets_left"], g["usable_now"]) for g in n3["grants"]],
      [("R1", 1, True), ("R2", 0, False)])
check("Claude 리셋권과 같은 모양(주간 전체)", (n3["eligible"], n3["grants"][0]["clears"]), (True, ["seven_day"]))


def _jwt(exp):
    enc = lambda o: _b64.urlsafe_b64encode(json.dumps(o).encode()).decode().rstrip("=")
    return enc({"alg": "none"}) + "." + enc({"exp": exp}) + ".x"


cdir = tempfile.mkdtemp()
col = CodexCollector(codex_dir=cdir, host="h")
calls = []
real_fetch = cc.fetch_reset_credits
cc.fetch_reset_credits = lambda tok, acc, timeout=20: (calls.append((tok, acc)) or (n3, None))
try:
    with open(os.path.join(cdir, "auth.json"), "w") as f:
        json.dump({"tokens": {"access_token": _jwt(int(_time.time()) - 10), "account_id": "A"}}, f)
    cc._RESET_CACHE.clear()
    check("만료된 토큰이면 조회하지 않는다 (갱신도 안 한다)",
          (col._reset_credits({"account_id": "A"}, ("A", "e"), 1000), calls), (None, []))
    with open(os.path.join(cdir, "auth.json"), "w") as f:
        json.dump({"tokens": {"access_token": _jwt(int(_time.time()) + 3600), "account_id": "A"}}, f)
    got = col._reset_credits({"account_id": "A"}, ("A", "e"), 2000)
    col._reset_credits({"account_id": "A"}, ("A", "e"), 2000 + 60 * 1000)
    check("유효한 토큰이면 조회하고, 15분 안에는 다시 부르지 않는다", (got[0]["grants"][0]["id"], len(calls)), ("R1", 1))
finally:
    cc.fetch_reset_credits = real_fetch
    shutil.rmtree(cdir, ignore_errors=True)
x1 = {"provider": "codex", "email": "e", "account_id": "1", "credits": {"balance": 3.0, "observed_at": 50}}
x2 = {"provider": "codex", "email": "e", "account_id": "1",
      "credits": {"source": "codex_api", "limit_resets": n3, "limit_resets_observed_at": 40}}
m = _dedupe_accounts([x1, x2])[0]["credits"]
check("잔액은 더 최근 디렉토리, 리셋권은 조회한 디렉토리에서", (m["balance"], m["limit_resets"] is n3, m["limit_resets_observed_at"]), (3.0, True, 40))
check("리셋권을 쓰는 주소는 부르지 않는다", "consume" in cc.RESET_CREDITS_URL, False)

print()
if failures:
    print(f"실패 {len(failures)}건: {', '.join(failures)}")
    sys.exit(1)
print("전부 통과")
