#!/usr/bin/env python3
"""턴 하나가 중앙에서 한 행으로만 남는지 검증한다 (stdlib 만 사용).

    python3 tests/test_codex_uuid.py

재현하는 것은 중앙(aidas-ai-monitoring)에서 실제로 관측된 고장입니다. 세션을
resume 하면 Codex 가 이전 턴들을 **재개 시각으로 다시 적습니다.** 예전 키
(``codex:{sid}:{ts}:{seq}``)는 ts 와 seq 를 모두 품고 있어서, 같은 턴이 적힐
때마다 다른 키가 됐고 중앙은 그걸 새 사용량으로 받았습니다.

  세션 019f7e79 — 같은 (input, output, cache_read) 조합이 08-10..08-31 사이
  최대 168회. 실제 턴 69,275개가 1,396,827행(20배)으로 불었고, 허수 cache_read
  약 76B 이 주간 합계에 섞여 한 사람의 7일 사용량이 3.1B 대신 58.4B 로 보였습니다.

emitted_seq 커서가 재전송을 줄여 주지만 고장을 막지는 못합니다. 커서가 사라지는
경우(파일 축소·inode 변경·신규 설치·상태 초기화)에 파일을 처음부터 다시 읽으면,
그 안의 재개분이 서로 다른 ts 를 들고 있어 그대로 중복이 됩니다. 그래서 키 자체가
읽은 위치와 무관해야 합니다.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from sender.codex_collector import CodexCollector, usage_uuid   # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {label}")
    if not ok:
        print(f"       got={got!r} want={want!r}")
        failures.append(label)


SID = "019f7e79-0476-7053-9bb5-1073844c1201"


def _turn(ts, inp, out, cached, model="gpt-5.6-sol"):
    """rollout 의 token_count 이벤트 한 줄.

    ``inp`` 는 DB 에 남을 순(캐시 제외) 입력량으로 준다. rollout 의
    ``input_tokens`` 는 캐시분을 포함한 값이라 여기서 다시 더해 적는다.
    """
    return json.dumps({
        "type": "event_msg",
        "timestamp": ts,
        "payload": {
            "type": "token_count",
            "rate_limits": {"plan_type": "pro"},
            "info": {"last_token_usage": {
                "input_tokens": inp + cached, "cached_input_tokens": cached,
                "output_tokens": out, "total_tokens": inp + cached + out}},
        },
    })


def _rollout(path, turns, sid=SID, cwd="/home/u/proj"):
    lines = [json.dumps({"type": "session_meta", "payload": {
        "id": sid, "cwd": cwd, "timestamp": "2026-08-10T08:03:00.000Z",
        "originator": "Codex Desktop", "cli_version": "0.145.0"}})]
    lines.append(json.dumps({"type": "turn_context",
                             "payload": {"model": "gpt-5.6-sol"}}))
    lines += turns
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


print("[1] 키가 읽은 위치에 딸려 가지 않는가")
a = usage_uuid(SID, "gpt-5.6-sol", 7339, 470, 115456)
check("같은 턴은 같은 키", usage_uuid(SID, "gpt-5.6-sol", 7339, 470, 115456), a)
check("토큰이 다르면 다른 키",
      usage_uuid(SID, "gpt-5.6-sol", 7339, 471, 115456) != a, True)
check("모델이 다르면 다른 키",
      usage_uuid(SID, "gpt-5.6-terra", 7339, 470, 115456) != a, True)
check("세션이 다르면 다른 키",
      usage_uuid("other-sid", "gpt-5.6-sol", 7339, 470, 115456) != a, True)
check("ts 는 키에 영향을 주지 않는다 (resume 가 ts 를 다시 찍는다)",
      usage_uuid(SID, "gpt-5.6-sol", 7339, 470, 115456), a)


print("\n[2] resume 가 이력을 다시 적어도 턴 수가 늘지 않는가")
home = tempfile.mkdtemp(prefix="codex-uuid-")
try:
    sess = os.path.join(home, "sessions", "2026", "08", "10")
    os.makedirs(sess)
    rollout = os.path.join(sess, f"rollout-2026-08-10T08-03-00-{SID}.jsonl")

    # 실제 턴 3개.
    real = [(7339, 470, 115456), (1216, 306, 165632), (4465, 197, 123648)]
    _rollout(rollout, [_turn(f"2026-08-10T08:0{i}:00.000Z", *t)
                       for i, t in enumerate(real)])

    def _collect():
        # 커서를 매번 버린다 = 파일을 처음부터 다시 읽는 상황(신규 설치/상태 초기화).
        c = CodexCollector(home, host="t", file_state={})
        return c.collect().get("usage") or []

    first = _collect()
    check("처음 수집에서 턴 3개", len(first), 3)

    # resume: 같은 세 턴이 재개 시각으로 다시 적히고, 뒤에 새 턴 하나가 붙는다.
    resumed = [_turn(f"2026-08-10T08:0{i}:00.000Z", *t)
               for i, t in enumerate(real)]
    resumed += [_turn("2026-08-31T15:37:00.000Z", *t) for t in real]
    resumed += [_turn("2026-08-31T15:38:00.000Z", 999, 11, 120000)]
    _rollout(rollout, resumed)

    second = _collect()
    uniq = {u["uuid"] for u in second}
    check("재개분까지 읽어도 고유 키는 4개 (3 + 새 턴 1)", len(uniq), 4)
    check("중앙이 INSERT OR IGNORE 로 접은 뒤 남는 행도 4개",
          len({u["uuid"]: u for u in second}), 4)

    # 예전 키로는 몇 행이 됐을지 — 같은 입력에서 7행.
    old = {f"codex:{u['session_id']}:{u['ts']}:{i}"
           for i, u in enumerate(second, 1)}
    check("예전 키였다면 7행으로 남았다", len(old), 7)

    # 남는 행은 그 턴의 '원래' 시각이어야 한다 (INSERT OR IGNORE = 먼저 들어온 것).
    by_uuid = {}
    for u in second:
        by_uuid.setdefault(u["uuid"], u)      # 먼저 나온 것 = 더 이른 ts
    first_turn = usage_uuid(SID, "gpt-5.6-sol", 7339, 470, 115456)
    check("살아남는 행은 원래 시각을 들고 있다",
          by_uuid[first_turn]["ts"] < 1788000000000, True)
finally:
    shutil.rmtree(home, ignore_errors=True)


print("\n[3] 토큰 성분이 키와 어긋나지 않는가")
home = tempfile.mkdtemp(prefix="codex-uuid2-")
try:
    sess = os.path.join(home, "sessions", "2026", "08", "10")
    os.makedirs(sess)
    _rollout(os.path.join(sess, f"rollout-x-{SID}.jsonl"),
             [_turn("2026-08-10T08:00:00.000Z", 2339, 470, 5000)])
    u = (CodexCollector(home, host="t", file_state={}).collect()
         .get("usage") or [None])[0]
    # input_tokens 은 캐시분을 뺀 값이고, 키도 같은 값으로 계산돼야 한다.
    check("input_tokens = input - cached", u["input_tokens"], 2339)
    check("키가 행의 토큰 값과 일치한다", u["uuid"],
          usage_uuid(SID, u["model"], u["input_tokens"],
                     u["output_tokens"], u["cache_read_tokens"]))
finally:
    shutil.rmtree(home, ignore_errors=True)


print()
if failures:
    print(f"실패 {len(failures)}건: {', '.join(failures)}")
    sys.exit(1)
print("전부 통과")
