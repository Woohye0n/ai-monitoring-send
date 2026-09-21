#!/usr/bin/env python3
"""NAS inbox 를 읽어 노드별 수집 상태를 한 화면에 보여준다 (관리자용).

각 노드가 **언제 마지막으로 보고했는지**, 무엇을 읽고 있는지, 어느 표면이 잡히는지,
그리고 **쓰이는데 아무도 안 읽는 디렉토리**가 있는지 봅니다. 배포 직후와 정기 점검
때 이것만 보면 됩니다.

    python3 scripts/fleet-status.py                  # config.json 의 nas_root
    python3 scripts/fleet-status.py --nas /mnt/nas/yunseok/ai-monitoring
    python3 scripts/fleet-status.py --stale-hours 6  # 이보다 오래되면 경고

NAS 가 마운트된 곳(중앙 서버 등)에서 실행하세요. 읽기만 합니다.
"""
from __future__ import annotations

import argparse
import glob
import gzip
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_NAS = "/mnt/nas/yunseok/ai-monitoring"


def nas_root(arg):
    if arg:
        return arg
    try:
        with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as f:
            return json.load(f).get("nas_root") or DEFAULT_NAS
    except (OSError, ValueError):
        return DEFAULT_NAS


def newest_batch(node_dir):
    files = sorted(glob.glob(os.path.join(node_dir, "batch-*.json*")))
    if not files:
        return None
    path = files[-1]
    try:
        opener = gzip.open if path.endswith(".gz") else open
        with opener(path, "rb") as f:
            return json.load(f)
    except Exception:                                       # noqa: BLE001
        return None


def ago(ms):
    if not ms:
        return "?"
    s = max(0, time.time() - ms / 1000)
    if s < 3600:
        return f"{int(s / 60)}분"
    if s < 86400:
        return f"{int(s / 3600)}시간"
    return f"{int(s / 86400)}일"


def machine_key(b):
    """같은 장비인지 판별할 키. 구 배치에는 machine_id 가 없어 fqdn+ip 로 떨어진다."""
    if b.get("machine_id"):
        return ("mid", b["machine_id"])
    if b.get("fqdn") and b.get("ip"):
        return ("net", b["fqdn"], b["ip"])
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(description="노드별 수집 상태")
    ap.add_argument("--nas", default=None)
    ap.add_argument("--stale-hours", type=float, default=1.0,
                    help="이보다 오래된 노드를 경고 (기본 1시간)")
    a = ap.parse_args(argv)

    inbox = os.path.join(nas_root(a.nas), "inbox")
    if not os.path.isdir(inbox):
        print(f"inbox 를 못 찾았습니다: {inbox}", file=sys.stderr)
        return 2

    rows, by_machine, problems = [], {}, []
    for node_dir in sorted(glob.glob(os.path.join(inbox, "*"))):
        if not os.path.isdir(node_dir):
            continue
        node = os.path.basename(node_dir)
        b = newest_batch(node_dir)
        if b is None:
            rows.append((node, "배치 없음", "", "", "", ""))
            continue
        diag = b.get("diagnostics") or {}
        surfaces = diag.get("surfaces")
        age_ms = b.get("generated_at")
        stale = (time.time() - (age_ms or 0) / 1000) > a.stale_hours * 3600
        flags = []
        if stale:
            flags.append("멈춤")
        if diag.get("uncollected"):
            flags.append(f"누락{len(diag['uncollected'])}")
        if diag.get("warnings"):
            flags.append(f"경고{len(diag['warnings'])}")
        if not diag:
            flags.append("구버전")
        rows.append((node, ago(age_ms), b.get("os_user") or "?",
                     str(len(diag.get("dirs") or [])) if diag else "-",
                     " ".join(f"{k}={v}" for k, v in sorted((surfaces or {}).items())) or "-",
                     " ".join(flags)))
        key = machine_key(b)
        if key:
            by_machine.setdefault(key, []).append(node)
        for w in diag.get("warnings") or []:
            problems.append(f"{node}: {w}")
        for u in diag.get("uncollected") or []:
            problems.append(f"{node}: {u} 를 아무도 읽지 않습니다")

    print(f"{'노드':<22} {'최근':>6}  {'os_user':<11} {'dirs':>4}  {'표면':<26} 상태")
    print("-" * 84)
    for node, age, user, dirs, surf, flags in rows:
        print(f"{node:<22} {age:>6}  {user:<11} {dirs:>4}  {surf:<26} {flags}")

    dupes = {k: v for k, v in by_machine.items() if len(v) > 1}
    if dupes:
        print("\n같은 장비가 여러 노드로 보고 중 — 이름을 하나로 통일하세요:")
        for nodes in dupes.values():
            print(f"  {' = '.join(sorted(nodes))}")

    if problems:
        print("\n확인이 필요한 것:")
        for p in problems[:20]:
            print(f"  - {p}")
        if len(problems) > 20:
            print(f"  ... 외 {len(problems) - 20}건")

    return 1 if (dupes or problems) else 0


if __name__ == "__main__":
    sys.exit(main())
