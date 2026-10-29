#!/usr/bin/env python3
"""공용 drop 업로더: 순서·이름·장부·실패 처리 (stdlib 만 사용).

    python3 tests/test_drop_uploader.py
"""
import importlib.util
import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
spec = importlib.util.spec_from_file_location(
    "drop_uploader", os.path.join(ROOT, "scripts", "drop-uploader.py"))
up = importlib.util.module_from_spec(spec)
spec.loader.exec_module(up)

fails = []


def check(label, got, want):
    ok = got == want
    print(("  PASS  " if ok else "  FAIL  ") + label + ("" if ok else f"  got={got!r} want={want!r}"))
    if not ok:
        fails.append(label)


class FakeTransport:
    """원격 대신 로컬 폴더. fail_on 번째 deliver 에서 실패한다."""

    def __init__(self, remote, fail_on=None):
        self.remote, self.fail_on, self.calls = remote, fail_on, 0
        self.cfg = {}

    def deliver(self, local, node, final):
        self.calls += 1
        if self.fail_on and self.calls == self.fail_on:
            raise RuntimeError("network down")
        d = os.path.join(self.remote, "inbox", node)
        os.makedirs(d, exist_ok=True)
        shutil.copyfile(local, os.path.join(d, final))

    def _remotedir(self, node):
        return os.path.join(self.remote, "inbox", node)


def put(drop, login, name, age=120, body=b"x"):
    d = os.path.join(drop, login, "inbox", "hangang-robot")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, name)
    with open(p, "wb") as f:
        f.write(body)
    t = time.time() - age
    os.utime(p, (t, t))
    return p


def remote_names(remote):
    d = os.path.join(remote, "inbox", "hangang-robot")
    return sorted(os.listdir(d)) if os.path.isdir(d) else []


base = tempfile.mkdtemp(prefix="drop-up-")
try:
    drop, remote = os.path.join(base, "drop"), os.path.join(base, "remote")
    state = os.path.join(base, "state.json")
    cfg = {"drop_root": drop, "node_id": "hangang-robot"}
    up.remote_last_ms = lambda tport, node: 0
    clock = lambda: 1793300000000          # 시계가 멈춰 있어도 이름은 증가해야 한다

    print("[1] 계정이 섞여도 원래 순서대로, 엄격히 증가하는 이름으로 올린다")
    put(drop, "u2", "batch-1793200000300.json.gz")
    put(drop, "u1", "batch-1793200000100.json.gz")
    put(drop, "u1", "batch-1793200000200.json.gz")
    put(drop, "u1", "batch-1793200000400.json.gz.building")   # 쓰는 중
    put(drop, "u1", "up-batch-1793200000500.json.gz")          # sender 임시 이름
    put(drop, "u2", "batch-1793200000600.json.gz", age=5)      # 막 생김
    put(drop, "u2", "batch-1793200000700.json.gz", body=b"")   # 0바이트
    rc = up.run(cfg, state, FakeTransport(remote), log=lambda m: None, clock=clock)
    names = remote_names(remote)
    check("성공", rc, 0)
    check("완성된 3개만", names, ["batch-1793300000000-u1.json.gz",
                                  "batch-1793300000001-u1.json.gz",
                                  "batch-1793300000002-u2.json.gz"])
    check("올린 원본은 지운다",
          os.path.exists(os.path.join(drop, "u1", "inbox", "hangang-robot",
                                      "batch-1793200000100.json.gz")), False)
    check("중앙이 받는 이름 형식(batch-*.json.gz)", all(n.startswith("batch-") and n.endswith(".json.gz") for n in names), True)

    print("\n[2] 다음 회차의 이름은 앞 회차보다 크다 (시계가 뒤로 가도)")
    put(drop, "u3", "batch-1793100000000.json.gz")   # 생성 시각은 훨씬 이르다
    rc = up.run(cfg, state, FakeTransport(remote), log=lambda m: None,
                clock=lambda: 1793000000000)
    check("새 이름이 맨 뒤", remote_names(remote)[-1], "batch-1793300000003-u3.json.gz")

    print("\n[3] 지우지 못한 파일은 장부 때문에 다시 올라가지 않는다")
    p = put(drop, "u4", "batch-1793200000800.json.gz")
    real_remove = os.remove
    up.os.remove = lambda path: (_ for _ in ()).throw(PermissionError(path))
    try:
        up.run(cfg, state, FakeTransport(remote), log=lambda m: None, clock=clock)
        n1 = len(remote_names(remote))
        up.run(cfg, state, FakeTransport(remote), log=lambda m: None, clock=clock)
        check("두 번 돌려도 한 번만", len(remote_names(remote)), n1)
        check("원본은 남아 있다", os.path.exists(p), True)
    finally:
        up.os.remove = real_remove

    print("\n[4] 실패하면 그 회차는 멈추고, 다음 회차에 순서대로 이어간다")
    a = put(drop, "u5", "batch-1793200000900.json.gz")
    b = put(drop, "u5", "batch-1793200000950.json.gz")
    before = len(remote_names(remote))
    rc = up.run(cfg, state, FakeTransport(remote, fail_on=1), log=lambda m: None, clock=clock)
    check("실패 코드", rc, 1)
    check("아무것도 안 올라감", len(remote_names(remote)), before)
    check("원본 그대로", (os.path.exists(a), os.path.exists(b)), (True, True))
    rc = up.run(cfg, state, FakeTransport(remote), log=lambda m: None, clock=clock)
    check("재시도에서 둘 다", len(remote_names(remote)), before + 2)

    print("\n[5] dry-run 은 아무것도 바꾸지 않는다")
    c = put(drop, "u6", "batch-1793200001000.json.gz")
    before = len(remote_names(remote))
    st_before = open(state).read()
    seen = []
    up.run(cfg, state, FakeTransport(remote), dry_run=True, log=seen.append, clock=clock)
    check("원격 그대로", len(remote_names(remote)), before)
    check("장부 그대로", open(state).read(), st_before)
    check("무엇을 올릴지 알려준다", len(seen), 1)
finally:
    shutil.rmtree(base, ignore_errors=True)

print()
if fails:
    print(f"실패 {len(fails)}건: {', '.join(fails)}")
    sys.exit(1)
print("전부 통과")
