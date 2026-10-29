#!/usr/bin/env python3
"""공용 drop 폴더에 쌓인 여러 계정의 배치를 NAS inbox 로 올린다.

NAS 로 나가는 길이 계정 하나에만 있는 서버(KISTI 한강: 외부 TCP 는 allowlist,
TrueNAS 는 그 계정의 userspace tailscaled 로만 닿음)에서, 다른 계정들도 함께
집계하기 위한 업로더입니다.

    각 계정의 sender (--local --nas <drop>/<login>)
        → <drop>/<login>/inbox/<node>/batch-<ms>.json.gz
    이 업로더 (길을 가진 계정 하나, cron)
        → <remote_root>/inbox/<node>/batch-<업로드 ms>-<login>.json.gz

이름을 다시 붙이는 이유: 중앙(aidas-ai-monitoring nas_reader)은 노드 폴더마다
**마지막으로 처리한 파일 이름**만 기억하고 그보다 큰 이름만 읽습니다. 계정마다
sender 가 붙인 생성 시각 이름을 그대로 올리면, 늦게 도착한 작은 이름의 배치는
영영 읽히지 않습니다. 그래서 이 노드 폴더에 쓰는 주체는 이 업로더 하나뿐이어야
하고, 이름은 올린 순서대로 엄격히 증가합니다.

    python3 scripts/drop-uploader.py --config data/uploader.json --dry-run
    python3 scripts/drop-uploader.py --config data/uploader.json      # cron 에서

설정(JSON, chmod 600 권장):

    {"drop_root": "/appsdata/<owner>/ai-monitoring-drop",
     "node_id": "hangang-robot",
     "transport": {"mode": "ssh", "ssh_host": "...", "ssh_port": 22,
                   "ssh_user": "...", "ssh_key": "/path/to/key",
                   "remote_root": "/mnt/.../ai-monitoring"}}

다른 계정의 sender 는 umask 007 로 돌아야 합니다. 그래야 이 계정이 그 파일을
읽고(필수) 지울(선택) 수 있습니다. 지우지 못한 파일은 장부 때문에 다시 올라가지
않고, 그 계정 sender 의 prune(기본 72시간)이 정리합니다.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# 쓰는 중인 파일을 피한다. sender 는 up- 이름으로 복사한 뒤 rename 하므로 이미
# 원자적이지만, 공유 파일시스템(Lustre)의 메타데이터 지연에 대한 여유.
SETTLE_SECONDS = 30
_BATCH = re.compile(r"^batch-.*\.json(\.gz)?$")
_LOGIN = re.compile(r"^[A-Za-z0-9._-]+$")


def now_ms():
    return int(time.time() * 1000)


def load_state(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, ValueError):
        st = {}
    st.setdefault("last_ms", 0)
    st.setdefault("done", {})
    return st


def save_state(path, st):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f)
    os.replace(tmp, path)


def pending(drop_root, node, done, settle=SETTLE_SECONDS, now=None):
    """올릴 배치 목록 [(원래 이름, login, 경로, 지문)] — 원래 이름 순."""
    now = time.time() if now is None else now
    out = []
    try:
        logins = sorted(os.listdir(drop_root))
    except OSError:
        return out
    for login in logins:
        if not _LOGIN.match(login):
            continue
        d = os.path.join(drop_root, login, "inbox", node)
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for name in names:
            if not _BATCH.match(name):
                continue
            path = os.path.join(d, name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            if not st.st_size or now - st.st_mtime < settle:
                continue
            fp = [st.st_size, st.st_mtime_ns]
            if done.get(path) == fp:
                continue
            out.append((name, login, path, fp))
    out.sort()
    return out


def remote_last_ms(tport, node):
    """원격 노드 폴더에 이미 있는 batch 이름의 가장 큰 ms (없으면 0)."""
    from sender import sshcmd
    rd = tport._remotedir(node)
    out = sshcmd.ssh_exec(tport.cfg, f"ls -1 {sshcmd.shquote(rd)} 2>/dev/null || true")
    best = 0
    for line in out.splitlines():
        m = re.match(r"^(?:batch|up-batch)-(\d{13})", line.strip())
        if m:
            best = max(best, int(m.group(1)))
    return best


def run(cfg, state_path, tport, dry_run=False, log=print, clock=now_ms):
    node = cfg["node_id"]
    st = load_state(state_path)
    # 사라진 파일의 장부는 버린다(sender prune 또는 우리가 지움).
    st["done"] = {p: fp for p, fp in st["done"].items() if os.path.exists(p)}
    todo = pending(cfg["drop_root"], node, st["done"])
    if not todo:
        if not dry_run:
            save_state(state_path, st)
        return 0
    if not st["last_ms"] and not dry_run:
        st["last_ms"] = remote_last_ms(tport, node)

    sent = 0
    for name, login, path, fp in todo:
        ms = max(clock(), st["last_ms"] + 1)
        ext = ".json.gz" if name.endswith(".gz") else ".json"
        final = f"batch-{ms:013d}-{login}{ext}"
        if dry_run:
            log(f"would upload {path} -> inbox/{node}/{final}")
            st["last_ms"] = ms
            continue
        try:
            tport.deliver(path, node, final)
        except Exception as exc:  # noqa: BLE001 — 순서를 지키려고 이 회차는 멈춘다
            log(f"upload failed at {path}: {exc}")
            save_state(state_path, st)
            return 1
        st["last_ms"] = ms
        st["done"][path] = fp
        save_state(state_path, st)          # 원격 mv 성공 뒤에만 장부에 남긴다
        sent += 1
        try:
            os.remove(path)
        except OSError:
            pass                            # 권한 없음 → 장부가 재전송을 막는다
    if sent:
        log(f"uploaded {sent} batch(es) to inbox/{node}")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--config", default=os.path.join(ROOT, "data", "uploader.json"))
    p.add_argument("--state", default=os.path.join(ROOT, "data", "uploader-state.json"))
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)

    with open(args.config, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    for k in ("drop_root", "node_id", "transport"):
        if not cfg.get(k):
            sys.exit(f"config {args.config}: '{k}' 가 없습니다")

    os.makedirs(os.path.dirname(args.state), exist_ok=True)
    lock = open(args.state + ".lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 0                            # 앞 회차가 아직 도는 중

    from sender.transport import SshTransport
    tport = SshTransport(cfg["transport"])

    def log(msg):
        print(time.strftime("%Y-%m-%d %H:%M:%S ") + msg, flush=True)

    return run(cfg, args.state, tport, dry_run=args.dry_run, log=log)


if __name__ == "__main__":
    sys.exit(main())
