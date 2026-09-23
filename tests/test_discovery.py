#!/usr/bin/env python3
"""송신 에이전트가 '실제로 쓰이는 곳'을 찾는지 검증한다 (stdlib 만 사용).

    python3 tests/test_discovery.py

여기서 재현하는 것은 가상의 경우가 아니라 이 클러스터에서 실제로 관측된 두 가지
고장입니다.

  1. 홈 밖 config dir — 한 사용자가 CLAUDE_CONFIG_DIR=/mnt/data/<user>/.claude-<user>,
     CODEX_HOME=/mnt/nvme1/<user>/.codex-<user> 로 돕니다. `~/.claude*` glob 은
     둘 다 못 찾아, 그 노드는 몇 주 동안 계정만 보고하고 usage=0 이었습니다.
  2. 수집 경로 좁히기 — setup-accounts.py 가 config_dirs 를 랩 디렉토리로 덮어써서,
     VSCode 사이드바가 쓰는 ~/.codex 는 아예 읽히지 않았습니다.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from sender import discovery                      # noqa: E402
from sender.claude_collector import ClaudeCollector  # noqa: E402
from sender.codex_collector import CodexCollector    # noqa: E402

failures = []


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        print(f"        기대 {want!r}\n        실제 {got!r}")
        failures.append(name)


# --------------------------------------------------------------- 픽스처 만들기
def make_claude_dir(path, *, transcript_lines=(), mtime=None):
    os.makedirs(os.path.join(path, "projects", "proj"), exist_ok=True)
    os.makedirs(os.path.join(path, "sessions"), exist_ok=True)
    if transcript_lines:
        f = os.path.join(path, "projects", "proj", "s.jsonl")
        with open(f, "w", encoding="utf-8") as fh:
            for line in transcript_lines:
                fh.write(json.dumps(line) + "\n")
        if mtime:
            os.utime(f, (mtime, mtime))
    return path


def make_codex_dir(path, *, rollout_lines=(), mtime=None):
    day = os.path.join(path, "sessions", "2026", "09", "21")
    os.makedirs(day, exist_ok=True)
    if rollout_lines:
        f = os.path.join(day, "rollout-2026-09-21T00-00-00-1111-2222-3333-4444-5555.jsonl")
        with open(f, "w", encoding="utf-8") as fh:
            for line in rollout_lines:
                fh.write(json.dumps(line) + "\n")
        if mtime:
            os.utime(f, (mtime, mtime))
    return path


def turn(uuid, ts, entrypoint="cli"):
    return {"type": "assistant", "uuid": uuid, "sessionId": "sess-1",
            "timestamp": ts, "cwd": "/w", "entrypoint": entrypoint,
            "message": {"model": "m", "usage": {"input_tokens": 1, "output_tokens": 2}}}


def codex_turn(ts, originator="codex_vscode", source="vscode"):
    return [{"type": "session_meta", "timestamp": ts,
             "payload": {"id": "cx-1", "cwd": "/w", "originator": originator,
                         "source": source, "cli_version": "0.1"}},
            {"type": "event_msg", "timestamp": ts,
             "payload": {"type": "token_count",
                         "info": {"last_token_usage": {"input_tokens": 10,
                                                       "cached_input_tokens": 4,
                                                       "output_tokens": 5}}}}]


# ------------------------------------------------------------------- 표면 분류
print("\n[1] 표면 분류 — 실제로 관측된 값들")
check("claude 터미널", discovery.normalize_surface("cli"), "terminal")
check("claude 사이드바", discovery.normalize_surface("vscode"), "vscode")
check("codex 터미널", discovery.normalize_surface("codex_cli_rs", "cli"), "terminal")
check("codex tui", discovery.normalize_surface("codex-tui"), "terminal")
check("codex tui + source", discovery.normalize_surface("codex-tui", "tui"), "terminal")
check("codex 사이드바", discovery.normalize_surface("codex_vscode", "vscode"), "vscode")
# 데스크탑 앱도 source='vscode' 를 보낸다 — originator 를 먼저 보지 않으면 뭉갠다
check("codex 데스크탑", discovery.normalize_surface("Codex Desktop", "vscode"), "desktop")
# subagent 스레드는 source 가 dict 다. 그걸 문자열로 만들면 안 된다
check("codex subagent(source=dict)",
      discovery.normalize_surface("codex_vscode", {"subagent": {"depth": 1}}), "vscode")
check("모르는 값은 통과시킨다",
      discovery.normalize_surface("codex_exec"), "other:codex_exec")
check("값이 없으면 unknown", discovery.normalize_surface(None, ""), "unknown")


# ------------------------------------------------- 홈 밖 dir + 설정이 가리지 않기
print("\n[2] 디렉토리 탐색")
tmp = tempfile.mkdtemp(prefix="aidas-send-test-")
try:
    home = os.path.join(tmp, "home")
    outside = os.path.join(tmp, "mnt", "data")
    os.makedirs(home)
    os.makedirs(outside)
    personal = make_claude_dir(os.path.join(home, ".claude"))
    lab = make_claude_dir(os.path.join(home, ".claude-lab1"))
    far = make_claude_dir(os.path.join(outside, ".claude-someone"))
    codex_personal = make_codex_dir(os.path.join(home, ".codex"))

    # 두 도구 모두 sessions/ 를 가진다. 이름이 아니라 증거로 경로를 찾게 되면서
    # "sessions 가 있다" 만으로는 Claude 디렉토리가 Codex 로도 잡혔다.
    check("claude dir 를 codex 로 오인하지 않는다",
          discovery.looks_like("codex", personal), False)
    check("codex dir 를 claude 로 오인하지 않는다",
          discovery.looks_like("claude", codex_personal), False)
    check("claude dir 는 claude 로 잡힌다", discovery.looks_like("claude", personal), True)
    check("codex dir 는 codex 로 잡힌다", discovery.looks_like("codex", codex_personal), True)

    cfg_narrow = {"claude": {"enabled": True, "config_dirs": [lab]},
                  "codex": {"enabled": True, "dirs": []}}
    found = discovery.discover(cfg_narrow, home=home)
    paths = {d["path"] for d in found["dirs"]["claude"]}
    # 예전 동작: config_dirs 가 울타리라 lab 하나만 읽혔다
    check("설정이 개인 dir 를 가리지 않는다", personal in paths, True)
    check("설정한 랩 dir 도 그대로 읽는다", lab in paths, True)
    check("codex 개인 dir 도 찾는다",
          codex_personal in {d["path"] for d in found["dirs"]["codex"]}, True)

    # exclude_dirs 는 유일한 차단 수단
    cfg_excl = dict(cfg_narrow, exclude_dirs=[personal])
    paths = {d["path"] for d in discovery.discover(cfg_excl, home=home)["dirs"]["claude"]}
    check("exclude_dirs 는 지킨다", personal in paths, False)

    # discover:false 면 예전처럼 설정한 것만
    cfg_off = dict(cfg_narrow, discover=False)
    paths = {d["path"] for d in discovery.discover(cfg_off, home=home)["dirs"]["claude"]}
    check("discover:false 는 설정한 것만", paths, {lab})

    # 홈 밖 dir 는 glob 으로는 절대 안 잡힌다 — 살아 있는 프로세스가 유일한 단서다
    fake_bin = os.path.join(tmp, "bin")
    os.makedirs(fake_bin)
    # 진짜 실행 파일이어야 argv[0] 이 'claude' 가 된다. #!/bin/sh 스크립트로 두면
    # 커널이 인터프리터를 argv[0] 에 넣어 도구로 안 보인다 — 그 경우는 아래
    # '셸 래퍼' 검사에서 따로 다룬다.
    claude_stub = os.path.join(fake_bin, "claude")
    shutil.copy(shutil.which("sleep") or "/bin/sleep", claude_stub)
    env = dict(os.environ, CLAUDE_CONFIG_DIR=far, HOME=home)
    proc = subprocess.Popen([claude_stub, "30"], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(0.4)
        found = discovery.discover(cfg_narrow, home=home)
        paths = {d["path"] for d in found["dirs"]["claude"]}
        by_source = {d["path"]: d["source"] for d in found["dirs"]["claude"]}
        if os.path.isdir("/proc"):
            check("홈 밖 CLAUDE_CONFIG_DIR 를 프로세스에서 찾는다", far in paths, True)
            check("출처가 process 로 기록된다", by_source.get(far), "process")
        else:
            print("  SKIP  /proc 없음 — 프로세스 탐색 검사를 건너뜁니다")
    finally:
        proc.terminate()
        proc.wait(timeout=10)

    # 도구가 아닌 프로세스의 명령줄에 설정 디렉토리가 드러나는 경우.
    # 실제 예: `/bin/bash -c source /mnt/data/<user>/.claude-<user>/shell-snapshots/...`
    # 이건 도구가 아니라 셸이라 프로세스 목록에는 안 넣지만, 거기 적힌 경로는
    # glob 으로는 못 찾는 설정 디렉토리의 유일한 단서일 수 있다.
    hinted = make_claude_dir(os.path.join(outside, ".claude-hinted"))
    os.makedirs(os.path.join(hinted, "shell-snapshots"), exist_ok=True)
    snap = os.path.join(hinted, "shell-snapshots", "snapshot-bash-1.sh")
    open(snap, "w", encoding="utf-8").close()
    # 끝에 `exit 0` 을 둬야 bash 가 마지막 명령으로 exec 해버리지 않고 살아 있다
    # (그래야 명령줄이 남는다). 실제 관측된 줄도 명령이 여럿이라 bash 로 남아 있다.
    sh = subprocess.Popen(["/bin/bash", "-c", f"source {snap}; sleep 30; exit 0"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(0.4)
        found = discovery.discover(cfg_narrow, home=home)
        paths = {d["path"] for d in found["dirs"]["claude"]}
        if os.path.isdir("/proc"):
            check("셸 명령줄에 드러난 dir 도 찾는다", hinted in paths, True)
            check("셸 자체는 도구 프로세스가 아니다",
                  any(p["pid"] == sh.pid for p in found["processes"]), False)
        else:
            print("  SKIP  /proc 없음 — 명령줄 힌트 검사를 건너뜁니다")
    finally:
        sh.terminate()
        sh.wait(timeout=10)

    # ---------------------------------------------------- 부트스트랩 가드
    print("\n[3] 처음 보는 디렉토리의 과거 이력")
    old = time.time() - 30 * 86400
    old_dir = make_claude_dir(os.path.join(tmp, "old-claude"),
                              transcript_lines=[turn(f"u{i}", "2026-08-01T00:00:00Z")
                                                for i in range(50)], mtime=old)
    c = ClaudeCollector(old_dir, usage_enabled=False, bootstrap_days=2)
    part = c.collect()
    check("오래된 백로그는 싣지 않는다", len(part["usage"]), 0)
    check("커서는 남겨 이후 추가분을 귀속한다", len(part["file_state"]), 1)
    cursor = next(iter(part["file_state"].values()))
    check("커서가 파일 끝에 있다", cursor["offset"] == cursor["size"], True)

    c2 = ClaudeCollector(old_dir, usage_enabled=False, bootstrap_days=0)
    check("가드를 끄면 전부 읽는다", len(c2.collect()["usage"]), 50)

    # 최근 파일은 처음 봐도 읽는다
    new_dir = make_claude_dir(os.path.join(tmp, "new-claude"),
                              transcript_lines=[turn("n1", "2026-09-21T00:00:00Z")])
    c3 = ClaudeCollector(new_dir, usage_enabled=False, bootstrap_days=2)
    part = c3.collect()
    check("최근 파일은 처음 봐도 읽는다", len(part["usage"]), 1)
    check("표면이 레코드에 실린다", part["usage"][0]["surface"], "terminal")

    # ------------------------------------------------------- codex 표면
    print("\n[4] codex 레코드")
    cx = make_codex_dir(os.path.join(tmp, "cx"), rollout_lines=codex_turn("2026-09-21T00:00:00Z"))
    r = CodexCollector(cx, bootstrap_days=2).collect()
    check("codex usage 1건", len(r["usage"]), 1)
    check("codex 표면=vscode", r["usage"][0]["surface"], "vscode")
    check("토큰 매핑(input=10-4)", r["usage"][0]["input_tokens"], 6)
    check("세션 행에도 표면", r["sessions"][0]["surface"], "vscode")

    # 대시보드는 세션을 cwd 로 사람에게 붙인다. 변화 없는 롤아웃의 세션 행을
    # 빈 meta 로 만들면 cwd 가 사라져 그 세션은 누구에게도 안 붙는다 — 직전
    # 스냅샷의 미지정 세션 81개 중 79개가 규칙 문제가 아니라 이것이었다.
    c = CodexCollector(cx, bootstrap_days=2)
    c.collect()
    again = c.collect()                      # 파일은 그대로 — 여기가 문제 지점이었다
    check("변화 없는 주기에도 cwd 유지", (again["sessions"] or [{}])[0].get("cwd"), "/w")
    check("version 도 유지", (again["sessions"] or [{}])[0].get("version"), "0.1")
    check("표면도 유지", (again["sessions"] or [{}])[0].get("surface"), "vscode")

    # sender 를 재시작하면 메모리 캐시가 비지만, 커서는 남아 파일은 '변화 없음'
    # 으로 보인다. 그때도 디스크에서 meta 를 읽어야 한다.
    restarted = CodexCollector(cx, bootstrap_days=2)
    restarted.file_state = dict(again["file_state"] or c.file_state)
    after_restart = restarted.collect()
    check("재시작 직후에도 cwd 유지",
          (after_restart["sessions"] or [{}])[0].get("cwd"), "/w")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# ------------------------------------------ 같은 계정이 여러 디렉토리에 있을 때
print("\n[5] 같은 로그인이 두 디렉토리에 있을 때")
from sender.main import _dedupe_accounts, _dedupe_sessions, _dedupe_usage  # noqa: E402


def rec(uuid, assumed=True, email=None, tokens=100):
    return {"uuid": uuid, "assumed": assumed, "account_email": email,
            "input_tokens": tokens, "output_tokens": 0}


# 이력을 복사만 하고 원본을 안 지우면(= migrate-history.py 를 --move 없이) 같은 턴이
# 두 디렉토리에 남는다. 넓게 읽게 된 지금은 양쪽 다 읽히므로 두 번 보고된다.
out, dropped = _dedupe_usage([rec("a"), rec("a"), rec("b")])
check("같은 턴은 한 번만 보낸다", (len(out), dropped), (2, 1))
check("토큰이 부풀지 않는다", sum(r["input_tokens"] for r in out), 200)

# 어느 쪽이 살아남는지가 중요하다: 한 디렉토리는 계정을 증명하고 다른 쪽은 못 한다
out, _ = _dedupe_usage([rec("a", assumed=True), rec("a", assumed=False, email="x@y")])
check("귀속 가능한 사본이 이긴다", (out[0]["assumed"], out[0]["account_email"]),
      (False, "x@y"))
out, _ = _dedupe_usage([rec("a", assumed=False, email="x@y"), rec("a", assumed=True)])
check("순서가 반대여도 마찬가지", out[0]["assumed"], False)

# 같은 계정이라도 디렉토리마다 한 일이 다르면 합치면 안 된다
out, dropped = _dedupe_usage([rec("d1"), rec("d2")])
check("다른 턴은 합치지 않는다", (len(out), dropped), (2, 0))

acc = [{"provider": "claude", "email": "a@b", "account_id": "1"},
       {"provider": "claude", "email": "a@b", "account_id": "1",
        "rate_limits": {"five_hour": {}}, "rate_limits_updated_at": 100}]
merged = _dedupe_accounts(acc)
check("같은 로그인은 카드 하나", len(merged), 1)
check("한도를 가진 쪽을 남긴다", bool(merged[0].get("rate_limits")), True)
check("계정이 다르면 따로 둔다",
      len(_dedupe_accounts([{"provider": "claude", "email": "a@b", "account_id": "1"},
                            {"provider": "claude", "email": "a@b", "account_id": "2"}])), 2)
check("세션도 한 번만",
      len(_dedupe_sessions([{"provider": "claude", "session_id": "s"},
                            {"provider": "claude", "session_id": "s"}])), 1)

# --------------------------------------- 도중에 생긴 세션·계정을 잡는가
print("\n[6] sender 를 재시작하지 않고 새로 생긴 것을 잡는가")
from sender import main as _sm                                   # noqa: E402
from sender.main import CollectorRegistry, collect_all           # noqa: E402

_real_iter = discovery.iter_processes
discovery.iter_processes = lambda: ([], set())   # 진짜 홈이 섞이지 않게
try:
    box = tempfile.mkdtemp(prefix="aidas-live-")
    boxhome = os.path.join(box, "home"); os.makedirs(boxhome)
    ACC = {"oauthAccount": {"emailAddress": "lab@x.com", "accountUuid": "u1"}}

    def mkclaude(path):
        os.makedirs(os.path.join(path, "projects", "p"), exist_ok=True)
        os.makedirs(os.path.join(path, "sessions"), exist_ok=True)
        json.dump(ACC, open(os.path.join(path, ".claude.json"), "w"))
        open(os.path.join(path, "projects", "p", "s.jsonl"), "a").close()
        return path

    def add_turn(path, uuid, sess="s1"):
        with open(os.path.join(path, "projects", "p", "s.jsonl"), "a") as fh:
            fh.write(json.dumps({
                "type": "assistant", "uuid": uuid, "sessionId": sess,
                "timestamp": "2026-09-21T10:00:00Z", "cwd": "/w", "entrypoint": "cli",
                "message": {"model": "m", "usage": {"input_tokens": 10, "output_tokens": 5}},
            }) + "\n")

    cfg = json.loads(json.dumps(_sm.DEFAULTS))
    cfg.update(node_id="t", claude_usage={"enabled": False},
               codex={"enabled": False, "dirs": []})
    reg, state = CollectorRegistry(), {}

    def cycle():
        nonlocal_state = discovery.discover(cfg, home=boxhome)
        res, st = collect_all(cfg, reg.sync(cfg, nonlocal_state), state, nonlocal_state)
        state.clear(); state.update(st)
        return res

    first = mkclaude(os.path.join(boxhome, ".claude")); add_turn(first, "a1")
    cycle()                                   # 부트스트랩 (계정 미확정)
    add_turn(first, "a2")
    r = cycle()
    check("기존 세션의 새 턴을 귀속한다",
          [u["uuid"] for u in r["usage"] if not u["assumed"]], ["a2"])

    add_turn(first, "a3", sess="brand-new")
    r = cycle()
    check("새 세션도 재시작 없이 귀속한다",
          [u["uuid"] for u in r["usage"] if not u["assumed"]], ["a3"])

    # 로그인/계정 분리로 디렉토리가 새로 생기는 경우 — 수집기를 새로 만들어야 한다
    second = mkclaude(os.path.join(boxhome, ".claude-lab1")); add_turn(second, "b1")
    r = cycle()
    check("새 계정 디렉토리를 재시작 없이 잡는다",
          len(r["diagnostics"]["dirs"]), 2)
    add_turn(second, "b2")
    r = cycle()
    check("새 디렉토리의 턴도 귀속된다",
          [u["uuid"] for u in r["usage"] if not u["assumed"]], ["b2"])
    shutil.rmtree(box, ignore_errors=True)
finally:
    discovery.iter_processes = _real_iter

# ------------------------------------------------ codex 턴 귀속 기준점
print("\n[7] codex 턴이 '직전 폴링' 창을 놓쳐도 귀속되는가")
import datetime as _dt                                            # noqa: E402

_tmp = tempfile.mkdtemp(prefix="aidas-codex-")
try:
    cdir = os.path.join(_tmp, "cx")
    _day = os.path.join(cdir, "sessions", "2026", "09", "22")
    os.makedirs(_day)
    json.dump({"tokens": {"account_id": "acc1",
                          "id_token": "x.eyJlbWFpbCI6ImxhYkB4LmNvbSJ9.y"}},
              open(os.path.join(cdir, "auth.json"), "w"))
    roll = os.path.join(_day, "rollout-2026-09-22T00-00-00-1111-2222-3333-4444-5555.jsonl")

    def _turn(ts_ms, meta=False):
        iso = _dt.datetime.fromtimestamp(ts_ms / 1000, _dt.timezone.utc).isoformat()
        with open(roll, "a", encoding="utf-8") as fh:
            if meta:
                fh.write(json.dumps({"type": "session_meta", "timestamp": iso, "payload": {
                    "id": "cx1", "cwd": "/w", "originator": "codex_cli_rs", "source": "cli"}}) + "\n")
            fh.write(json.dumps({"type": "event_msg", "timestamp": iso, "payload": {
                "type": "token_count", "info": {"last_token_usage": {
                    "input_tokens": 100, "cached_input_tokens": 0, "output_tokens": 50}}}}) + "\n")

    _turn(time.time() * 1000 - 600_000, meta=True)     # 10분 전 턴
    col = CodexCollector(cdir, host="t", bootstrap_days=0)
    col.collect()                                       # 폴링1: 계정 확인
    first_poll = time.time() * 1000
    time.sleep(0.3)
    col.collect()                                       # 폴링2: 변화 없음
    time.sleep(0.3)
    # 폴링1 직후 시각의 턴이 뒤늦게 기록된다(=직전 폴링보다 오래된 턴).
    # 예전 규칙(기준점=직전 폴링)은 이걸 영영 버렸다 — 실측으로 5시간에 63M 토큰.
    _turn(first_poll + 100)
    res = col.collect()
    attributed = [u for u in res["usage"] if not u["assumed"]]
    check("직전 폴링보다 오래된 턴도 귀속한다", len(attributed), 1)
    check("토큰이 그대로 실린다",
          sum(u["input_tokens"] + u["output_tokens"] for u in attributed), 150)
    # 커서가 없으면 파일이 커질 때마다 처음부터 다시 보낸다. 실측으로 활성
    # 세션 하나가 배치 하나에 105,507행으로 실렸다(고유 턴은 95,881개).
    check("이미 보낸 턴은 다시 보내지 않는다", len(res["usage"]), 1)
    _turn(first_poll + 200)
    _turn(first_poll + 300)
    res2 = col.collect()
    check("새로 붙은 턴만 보낸다", len(res2["usage"]), 2)
    res3 = col.collect()
    check("바뀐 게 없으면 아무것도 안 보낸다", len(res3["usage"]), 0)

    # 파일이 줄면 다른 파일이다 — 커서를 버리고 처음부터 읽어야 한다.
    _lines = open(roll, encoding="utf-8").read().splitlines()
    open(roll, "w", encoding="utf-8").write("\n".join(_lines[:4]) + "\n")
    res4 = col.collect()
    check("파일이 줄면 커서를 버린다", len(res4["usage"]) > 0, True)

    # 계정을 못 읽는 주기(auth.json 손상)에 나간 턴은 assumed 라 중앙에서 버려진다.
    # 그 턴들은 계정이 돌아왔을 때 다시 보내져야 한다.
    _auth = os.path.join(cdir, "auth.json")
    _saved = open(_auth, encoding="utf-8").read()
    open(_auth, "w").write("{}")
    _turn(first_poll + 400)
    res5 = col.collect()
    check("계정을 모르면 assumed 로 나간다",
          all(u["assumed"] for u in res5["usage"]) and len(res5["usage"]) > 0, True)
    open(_auth, "w").write(_saved)
    res6 = col.collect()
    check("계정이 돌아오면 그 턴을 다시 보낸다", len(res6["usage"]) > 0, True)

    # 계정이 실제로 바뀌면 그 이전 턴은 여전히 미확정이어야 한다
    json.dump({"tokens": {"account_id": "acc2",
                          "id_token": "x.eyJlbWFpbCI6Im90aGVyQHguY29tIn0=.y"}},
              open(os.path.join(cdir, "auth.json"), "w"))
    time.sleep(0.3)
    _turn(first_poll + 200)                             # 전환 전 시각의 턴
    res = col.collect()
    check("계정이 바뀌면 그 이전 턴은 남의 것으로 세지 않는다",
          [u for u in res["usage"] if not u["assumed"]], [])
finally:
    shutil.rmtree(_tmp, ignore_errors=True)

print()
if failures:
    print(f"실패 {len(failures)}건: {', '.join(failures)}")
    sys.exit(1)
print("전부 통과")
