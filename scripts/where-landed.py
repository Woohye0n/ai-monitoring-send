#!/usr/bin/env python3
"""새 기록이 어느 계정 디렉토리에 쌓였는지 확인 — 이관·라우팅 검증용.

이관(migrate-history.py)이 끝나고 `codex resume` 으로 thread 를 다시 열었을 때,
그 대화가 정말 랩 디렉토리에 쌓이는지 확인합니다. 설정이 맞아 보여도 실제로
쓰이는 위치는 프로세스가 기동될 때 정해진 CODEX_HOME 이 결정하므로, 눈으로
확인하는 것이 유일하게 확실합니다.

    python3 scripts/where-landed.py              # 디렉토리별 최신 기록·계정·수집여부
    python3 scripts/where-landed.py --since 10   # 최근 10분에 쓰인 것만
    python3 scripts/where-landed.py --watch 180  # 새 기록을 기다렸다가 어디 쌓였는지

--watch 로 띄워두고 다른 창에서 프롬프트를 하나 보내세요. 어느 디렉토리에
쌓였는지 알려주고, 수집 대상이 아니면 종료코드 1 을 냅니다.
"""
from __future__ import annotations

import argparse
import base64
import glob
import json
import os
import sys
import time

HOME = os.path.expanduser("~")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from sender import discovery  # noqa: E402  (ROOT must be on the path first)

# 대화 기록이 실제로 쌓이는 하위 경로. codex 는 rollout, claude 는 프로젝트별 세션.
SUBDIRS = {"codex": ("sessions", "archived_sessions"), "claude": ("projects",)}

# 경로 -> 도구. 이름으로 때려맞히지 않고 sender 와 같은 탐색 결과를 쓴다.
_PROVIDER = {}


def kind_of(d):
    return _PROVIDER.get(os.path.realpath(d)) or (
        "codex" if "codex" in os.path.basename(d) else "claude")


def codex_account(d):
    try:
        tok = ((json.load(open(os.path.join(d, "auth.json"),
                               encoding="utf-8")).get("tokens") or {}).get("id_token") or "")
        p = tok.split(".")[1]
        p += "=" * (-len(p) % 4)
        return json.loads(base64.urlsafe_b64decode(p)).get("email")
    except (OSError, ValueError, IndexError, KeyError):
        return None


def claude_account(d):
    for p in (os.path.join(d, ".claude.json"), f"{d}.json",
              os.path.join(d, "claude.json")):
        try:
            acct = (json.load(open(p, encoding="utf-8")).get("oauthAccount") or {})
        except (OSError, ValueError):
            continue
        if acct.get("emailAddress"):
            return acct["emailAddress"]
    return None


def account(d):
    return codex_account(d) if kind_of(d) == "codex" else claude_account(d)


def _load_cfg():
    try:
        return json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8")), True
    except (OSError, ValueError):
        return {}, False


def scan():
    """sender 와 **같은 방식으로** 디렉토리를 찾는다.

    예전에는 여기서 `~/.codex*` `~/.claude*` 만 훑었습니다. 그래서 홈 밖에
    CLAUDE_CONFIG_DIR/CODEX_HOME 을 둔 서버에서는, 정작 일이 쌓이는 디렉토리가
    이 목록에 아예 나오지 않았습니다 — 확인 도구가 문제를 못 보는 셈이었죠.
    """
    cfg, have_cfg = _load_cfg()
    disc = discovery.discover(cfg)
    collected = set()
    for provider, dirs in disc["dirs"].items():
        for d in dirs:
            _PROVIDER[d["path"]] = provider
            if os.path.isdir(d["path"]):
                collected.add(d["path"])
    # 수집 대상이 아니더라도 홈에 있는 것은 함께 보여준다 (제외 설정 확인용)
    others = set()
    for pattern in (f"{HOME}/.codex*", f"{HOME}/.claude*"):
        for d in glob.glob(pattern):
            real = os.path.realpath(d)
            if os.path.isdir(d) and not d.endswith((".lock", ".bak")) \
                    and real not in collected:
                others.add(real)
    return disc, collected, sorted(collected | others), have_cfg


def snapshot(d):
    """{경로: (mtime, size)} — stat 만 하므로 크기와 무관하게 빠릅니다."""
    out = {}
    for sub in SUBDIRS[kind_of(d)]:
        for f in glob.glob(os.path.join(d, sub, "**", "*.jsonl"), recursive=True):
            try:
                st = os.stat(f)
            except OSError:
                continue
            out[f] = (st.st_mtime, st.st_size)
    return out


def thread_name(d, path):
    """codex rollout 파일의 thread 이름 (session_index.jsonl 에서)."""
    base = os.path.basename(path)
    try:
        with open(os.path.join(d, "session_index.jsonl"), encoding="utf-8",
                  errors="replace") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("id") and r["id"] in base:
                    return r.get("thread_name")
    except OSError:
        pass
    return None


def mark(d, collected, have_cfg):
    if not have_cfg:
        return "  ?  "
    return "수집" if os.path.realpath(d) in collected else "제외"


def coverage(disc, collected):
    """지금 돌고 있는 도구가 어디에 쓰는지, 그게 수집되는지."""
    procs = disc["processes"]
    print("  지금 돌고 있는 claude/codex 프로세스")
    if not procs:
        print("    (없음)")
    for pr in procs:
        ok = pr["config_dir"] in collected
        where = (pr["config_dir"] or "?").replace(HOME, "~", 1)
        print(f"    [{'수집' if ok else '누락'}] pid {pr['pid']:<8} {pr['provider']:6s} "
              f"{pr['surface_hint']:9s} → {where}")
    for w in disc["warnings"]:
        print(f"    ⚠ {w}")
    print()


def report(since_min, dirs, collected, have_cfg):
    cutoff = time.time() - since_min * 60 if since_min else 0
    any_row = False
    for d in dirs:
        snap = snapshot(d)
        rows = sorted(((mt, sz, p) for p, (mt, sz) in snap.items() if mt >= cutoff),
                      reverse=True)
        if since_min and not rows:
            continue
        any_row = True
        print(f"  [{mark(d, collected, have_cfg)}] {os.path.basename(d):16s} "
              f"{account(d) or '(로그인 안 됨)':26s} 기록 {len(snap)}개")
        for mt, _sz, p in rows[:3]:
            tn = thread_name(d, p) if kind_of(d) == "codex" else None
            print(f"          {time.strftime('%m-%d %H:%M:%S', time.localtime(mt))}  "
                  f"{os.path.basename(p)[:44]}" + (f"  [{tn}]" if tn else ""))
    if since_min and not any_row:
        print(f"  최근 {since_min}분간 아무 기록도 없습니다.")


def watch(seconds, dirs, collected, have_cfg):
    before = {d: snapshot(d) for d in dirs}
    print(f"  {seconds}초 동안 새 기록을 기다립니다. 다른 창에서 프롬프트를 보내세요…")
    deadline = time.time() + seconds
    while time.time() < deadline:
        time.sleep(2)
        for d in dirs:
            now = snapshot(d)
            changed = [p for p, v in now.items() if before[d].get(p) != v]
            if not changed:
                continue
            ok = have_cfg and os.path.realpath(d) in collected
            for p in changed:
                tn = thread_name(d, p) if kind_of(d) == "codex" else None
                print(f"\n  → {os.path.basename(d)} 에 쌓였습니다"
                      + (f"  [{tn}]" if tn else ""))
                print(f"     {os.path.basename(p)}")
            print(f"     계정 {account(d) or '(로그인 안 됨)'}")
            if ok:
                print("     수집 대상입니다 ✅  이관·라우팅 정상")
                return 0
            print("     ⚠ 수집 대상이 아닙니다 — 이 작업은 대시보드에 안 잡힙니다.")
            if kind_of(d) == "codex" and os.path.basename(d) == ".codex":
                print("       맨 `codex` 로 실행했을 때 생기는 현상입니다.")
                print("       `lab1 codex resume` 또는 `~/.local/bin/codex-lab1 resume` 을 쓰세요.")
            return 1
        before = {d: snapshot(d) for d in dirs}
    print("\n  시간 안에 새 기록이 없었습니다. 프롬프트가 실제로 전송됐는지 확인하세요.")
    return 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="새 기록이 어느 계정 디렉토리에 쌓였는지")
    ap.add_argument("--since", type=int, metavar="분", help="최근 N분에 쓰인 것만")
    ap.add_argument("--watch", type=int, metavar="초", nargs="?", const=180,
                    help="새 기록을 기다렸다가 보고 (기본 180초)")
    a = ap.parse_args(argv)

    disc, collected, dirs, have_cfg = scan()
    if not have_cfg:
        print("  ! config.json 이 없어 기본값으로 판정합니다 "
              "(setup.sh 를 돌린 서버에서 실행하세요).\n")
    if a.watch:
        return watch(a.watch, dirs, collected, have_cfg)
    coverage(disc, collected)
    report(a.since, dirs, collected, have_cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
