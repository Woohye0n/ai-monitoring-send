#!/usr/bin/env python3
"""단가를 가르는 값이 배치까지 실려 가는지 검증한다 (stdlib 만 사용).

    python3 tests/test_price_fields.py

토큰 수가 같아도 값이 다른 경우가 있습니다(공식 API 단가표 기준).

  Claude 캐시 쓰기 TTL   5분 쓰기 = 입력가 x1.25,  1시간 쓰기 = 입력가 x2
  Claude fast mode       Opus 5.5 $4/$20 -> $8/$40 (2배)
  Codex 처리 티어        priority(=Fast) gpt-5.6-sol $4/$20 -> $8/$40 (2배)

세 값 모두 원본에는 있는데 송신기가 버리고 있었습니다. 실제로 관측한 모양을
그대로 재현합니다.

  - Claude Code 의 캐시 쓰기는 97~100% 가 1시간짜리였다 (합계 칸 하나로는 구분 불가)
  - Codex 는 9,430 턴이 priority 로 돌았다. 그 값은 턴의 usage 가 아니라
    thread_settings_applied 이벤트에 '설정 상태' 로만 적힌다.
  - Codex 송신기는 같은 이름의 service_tier 칸에 요금제(plan_type="pro")를 실었다.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from sender.claude_collector import ClaudeCollector                 # noqa: E402
from sender.codex_collector import CodexCollector, speed_of_tier    # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {label}")
    if not ok:
        print(f"       got={got!r} want={want!r}")
        failures.append(label)


def _write(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(json.dumps(r) for r in rows) + "\n")


# ---------------------------------------------------------------------------
print("[1] Claude: TTL·speed·effort 가 턴마다 실리는가")
home = tempfile.mkdtemp(prefix="claude-price-")
try:
    path = os.path.join(home, "projects", "-w-p", "s1.jsonl")

    def _assistant(uid, usage, effort):
        return {"type": "assistant", "uuid": uid, "sessionId": "s1",
                "cwd": "/w/p", "timestamp": "2026-10-01T00:00:00.000Z",
                "effort": effort,
                "message": {"model": "claude-opus-5-5", "usage": usage}}

    _write(path, [
        # 실제 줄 모양: 합계 칸 + TTL 별 분해 객체 + speed
        _assistant("a1", {"input_tokens": 2, "output_tokens": 309,
                          "cache_read_input_tokens": 21204,
                          "cache_creation_input_tokens": 7730,
                          "cache_creation": {"ephemeral_5m_input_tokens": 0,
                                             "ephemeral_1h_input_tokens": 7730},
                          "service_tier": "standard", "speed": "standard"}, "xhigh"),
        _assistant("a2", {"input_tokens": 5, "output_tokens": 10,
                          "cache_read_input_tokens": 0,
                          "cache_creation_input_tokens": 100,
                          "cache_creation": {"ephemeral_5m_input_tokens": 100,
                                             "ephemeral_1h_input_tokens": 0},
                          "speed": "fast"}, "low"),
        # 분해 객체가 없는 옛 줄 — TTL 은 '모름' 이어야지 0 이면 안 된다
        _assistant("a3", {"input_tokens": 1, "output_tokens": 1,
                          "cache_creation_input_tokens": 50}, None),
    ])
    c = ClaudeCollector(home, usage_enabled=False, bootstrap_days=0)
    rows, _ = c._parse_file("-w-p", path, "lab@x.com", False)
    by = {r["uuid"]: r for r in rows}
    check("1시간 쓰기를 구분한다", by["a1"]["cache_creation_1h_tokens"], 7730)
    check("5분 쓰기를 구분한다", by["a2"]["cache_creation_5m_tokens"], 100)
    check("합계 칸은 그대로", by["a1"]["cache_creation_tokens"], 7730)
    check("speed 가 실린다", (by["a1"]["speed"], by["a2"]["speed"]), ("standard", "fast"))
    check("effort 가 실린다", (by["a1"]["effort"], by["a2"]["effort"]), ("xhigh", "low"))
    check("분해 객체가 없으면 TTL 은 None (0 아님)",
          (by["a3"]["cache_creation_5m_tokens"], by["a3"]["cache_creation_1h_tokens"]),
          (None, None))
finally:
    shutil.rmtree(home, ignore_errors=True)


# ---------------------------------------------------------------------------
print("\n[2] Codex: 처리 티어·effort 를 상태로 따라가는가")
SID = "019f7e79-0476-7053-9bb5-1073844c1201"
home = tempfile.mkdtemp(prefix="codex-price-")
try:
    path = os.path.join(home, "sessions", "2026", "10", "01", f"rollout-x-{SID}.jsonl")

    def _ctx(model, effort):
        return {"type": "turn_context", "payload": {"model": model, "effort": effort}}

    def _tier(t):
        return {"type": "event_msg", "payload": {
            "type": "thread_settings_applied",
            "thread_settings": {"model": "gpt-5.6-sol", "service_tier": t}}}

    def _turn(ts, inp, out, cached, written=0):
        return {"type": "event_msg", "timestamp": ts, "payload": {
            "type": "token_count", "rate_limits": {"plan_type": "pro"},
            "info": {"last_token_usage": {
                "input_tokens": inp, "cached_input_tokens": cached,
                "cache_write_input_tokens": written, "output_tokens": out,
                "total_tokens": inp + out}}}}

    _write(path, [
        {"type": "session_meta", "payload": {"id": SID, "cwd": "/w/p",
                                             "originator": "Codex Desktop"}},
        _tier("default"),
        _ctx("gpt-5.6-sol", "xhigh"),
        _turn("2026-10-01T00:00:00.000Z", 12793, 13, 9984),
        _tier("priority"),                      # 사용자가 /fast 를 켰다
        _ctx("gpt-5.6-sol", "low"),
        _turn("2026-10-01T00:01:00.000Z", 5000, 20, 4000, written=500),
        _tier("default"),                       # 다시 껐다
        _ctx("gpt-6-astra", "medium"),
        _turn("2026-10-01T00:02:00.000Z", 3000, 30, 1000),
    ])
    c = CodexCollector(home, host="t", bootstrap_days=0)
    _meta, rows, _rl, _seq = c._parse_rollout(path, "lab@x.com", attributable_after_ms=1)
    check("턴 3개", len(rows), 3)
    check("티어가 턴마다 따라온다",
          [r["service_tier"] for r in rows], ["default", "priority", "default"])
    check("priority 는 fast 로 옮겨진다",
          [r["speed"] for r in rows], ["standard", "fast", "standard"])
    check("effort 가 턴마다 따라온다", [r["effort"] for r in rows], ["xhigh", "low", "medium"])
    check("요금제는 plan 으로 옮겨졌다", rows[0]["plan"], "pro")
    check("캐시 쓰기를 0 으로 박지 않는다", rows[1]["cache_creation_tokens"], 500)
    check("입력은 캐시 읽기·쓰기를 뺀 몫", rows[1]["input_tokens"], 5000 - 4000 - 500)
    check("캐시 쓰기가 없으면 예전과 같은 입력값 (uuid 불변)",
          rows[0]["input_tokens"], 12793 - 9984)

    # 커서로 앞의 두 턴을 건너뛰어도, 건너뛴 구간의 설정은 그대로 반영돼야 한다.
    _meta, rows2, _rl, _seq = c._parse_rollout(path, "lab@x.com",
                                               attributable_after_ms=1, since_seq=2)
    check("이미 보낸 턴을 건너뛰어도 상태는 유지", (len(rows2), rows2[0]["effort"],
                                          rows2[0]["speed"]), (1, "medium", "standard"))
finally:
    shutil.rmtree(home, ignore_errors=True)


# ---------------------------------------------------------------------------
print("\n[3] 티어 이름 맞추기")
check("default -> standard", speed_of_tier("default"), "standard")
check("값 없음 -> standard", speed_of_tier(None), "standard")
check("priority -> fast", speed_of_tier("priority"), "fast")
check("flex 는 그대로", speed_of_tier("flex"), "flex")
check("모르는 티어는 뭉개지 않는다", speed_of_tier("ultrafast"), "ultrafast")


print()
if failures:
    print(f"실패 {len(failures)}건: {', '.join(failures)}")
    sys.exit(1)
print("전부 통과")
