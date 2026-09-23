"""이 장비가 이미 쓰고 있던 노드 이름을 찾아낸다.

노드 이름은 사람이 아니라 **장비**를 가리켜야 한다. 그런데 한 서버에 여러
사용자가 각자 설치하면서 저마다 --host 를 골랐고, 대시보드에는 서버 하나가
셋으로 보였다(ADS-A100 = aidas-a100 = hgkim_AIDAS_A100). 이름을 외워서
매번 넘기게 하는 대신, 이미 보고 중인 이름이 있으면 그것을 그대로 물려받는다.

식별 키는 fqdn 이고 machine_id 는 보강용이다. 반대로 하면 안 된다:
같은 컨테이너 이미지로 뜬 kakao-b200 파드 4개가 /etc/machine-id 를 공유해
machine_id 가 전부 같았다. 그것만 보면 서로 다른 노드 4개가 하나로 합쳐진다.
IP 는 파드가 재시작할 때마다 바뀌므로 키에 넣지 않는다.
"""
from __future__ import annotations

import glob
import gzip
import hashlib
import json
import os
import socket
import tempfile

MARKER = "_identity.json"

# 장비를 못 가리키는 이름들. 역방향 DNS 가 실패하면 fqdn 자리에
# "1.0.0.0.0.0.0.0.0.0.0.0.0.0." 같은 쓰레기가 들어오기도 한다.
_GENERIC = {"", "localhost", "localhost.localdomain", "ubuntu", "debian",
            "server", "servername", "node", "host", "pc", "desktop"}


def _meaningful(fqdn):
    if not fqdn:
        return False
    low = fqdn.strip().lower().rstrip(".")
    if low in _GENERIC:
        return False
    # 역방향 DNS 존 이름(...ip6.arpa)은 호스트 이름이 아니라 주소의 다른 표기다.
    # 주소가 바뀌면 같이 바뀌므로 장비를 가리키는 데 쓸 수 없다.
    if low.endswith(".arpa"):
        return False
    # 숫자와 점만 있으면 이름이 아니라 주소(또는 역방향 DNS 잔해)다.
    return not all(c in "0123456789.:" for c in low)


def machine_id():
    for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            raw = open(path, encoding="utf-8").read().strip()
        except OSError:
            continue
        if raw:
            return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return None


def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.settimeout(1)
        s.connect(("8.8.8.8", 53))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def identity():
    return {"fqdn": socket.getfqdn(), "ip": local_ip(), "machine_id": machine_id()}


def same_machine(a, b):
    """fqdn 이 같아야 같은 장비다. machine_id 는 양쪽에 다 있을 때만 따진다.

    machine_id 를 함께 보는 이유는 서로 다른 클러스터가 똑같이 'gpu-0-0' 같은
    파드 이름을 쓸 수 있기 때문이다. 그때는 machine_id 가 갈라 준다.
    """
    fa, fb = (a or {}).get("fqdn"), (b or {}).get("fqdn")
    if not _meaningful(fa) or not _meaningful(fb):
        return False
    if fa.strip().lower().rstrip(".") != fb.strip().lower().rstrip("."):
        return False
    ma, mb = (a or {}).get("machine_id"), (b or {}).get("machine_id")
    return not (ma and mb) or ma == mb


def _read_json(path, gz=False):
    try:
        with (gzip.open(path) if gz else open(path, "rb")) as fh:
            return json.load(fh)
    except Exception:                                        # noqa: BLE001
        return None


def scan_inbox(nas_root):
    """inbox 의 노드별로 (신원, 배치 수, 최근 시각) 을 모은다.

    아직 안 올라간 노드에는 마커가 없으므로 마지막 배치에서 읽는다. 배치에도
    이미 fqdn/machine_id 가 실려 있어서 기존 노드까지 전부 커버된다.
    """
    out = {}
    for d in sorted(glob.glob(os.path.join(nas_root, "inbox", "*"))):
        if not os.path.isdir(d):
            continue
        name = os.path.basename(d)
        if name.startswith("_"):
            continue
        batches = sorted(glob.glob(os.path.join(d, "batch-*.json*")))
        ident = _read_json(os.path.join(d, MARKER))
        if not ident and batches:
            last = batches[-1]
            ident = _read_json(last, gz=last.endswith(".gz"))
        if not ident:
            continue
        mtime = 0
        if batches:
            try:
                mtime = os.path.getmtime(batches[-1])
            except OSError:
                pass
        out[name] = {"ident": ident, "batches": len(batches), "mtime": mtime}
    return out


def resolve(nas_root, me=None, exclude=()):
    """이 장비가 이미 쓰고 있는 노드 이름. 없으면 None."""
    me = me or identity()
    hits = [(v["batches"], v["mtime"], n) for n, v in scan_inbox(nas_root).items()
            if n not in exclude and same_machine(me, v["ident"])]
    if not hits:
        return None
    # 배치가 가장 많은 이름이 그 장비의 '원래' 이름일 가능성이 높다.
    # 같으면 최근에 쓰인 쪽.
    hits.sort(reverse=True)
    return hits[0][2]


def claimed_by(nas_root, node):
    """이 노드 이름을 이미 쓰고 있는 다른 장비. 없으면 None."""
    ident = _read_json(os.path.join(nas_root, "inbox", node, MARKER))
    return ident if ident and ident.get("fqdn") else None


def write_marker(nas_root, node, me=None):
    """다음 설치가 한 번의 읽기로 찾을 수 있도록 신원을 남긴다.

    이미 다른 장비가 이 이름을 쓰고 있으면 덮어쓰지 않는다. 실제로 gpu-1-0 에서
    --host kakao-b200-2 를 잘못 줘서, gpu-2-0 의 마커가 gpu-1-0 것으로 바뀌었다.
    그러면 두 장비가 한 이름으로 보고하는데 마커는 한쪽만 가리켜, 다음 설치의
    자동 해석까지 같이 틀어진다.
    """
    me = dict(me or identity())
    me["node_id"] = node
    other = claimed_by(nas_root, node)
    if other and not same_machine(me, other):
        return False
    d = os.path.join(nas_root, "inbox", node)
    try:
        os.makedirs(d, exist_ok=True)
        tmp = os.path.join(d, MARKER + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(me, fh)
        os.replace(tmp, os.path.join(d, MARKER))
        return True
    except OSError:
        return False


# ---------------------------------------------------------------- SSH 모드
# NAS 를 마운트하지 않은 노드(kakao-b200 계열)도 같은 판단을 할 수 있어야 한다.
# 왕복을 한 번으로 끝내려고 원격에서 한 번에 훑는다. 아직 마커가 없는 노드는
# 마지막 배치에서 읽는데, JSON 키 순서에 기대지 않도록 쉼표로 쪼개서 고른다.
_REMOTE_SCAN = r"""
for d in @ROOT@/inbox/*/; do
  [ -d "$d" ] || continue
  n=$(basename "$d")
  case "$n" in _*) continue;; esac
  printf '== %s\n' "$n"
  printf 'batches=%s\n' "$(ls "$d"batch-*.json* 2>/dev/null | wc -l)"
  if [ -f "${d}_identity.json" ]; then
    cat "${d}_identity.json"
  else
    f=$(ls -t "$d"batch-*.json.gz 2>/dev/null | head -1)
    [ -n "$f" ] && gzip -dc "$f" 2>/dev/null | tr ',' '\n' \
      | grep -E '"(fqdn|machine_id)"' | head -4
  fi
  printf '\n'
done
"""


def parse_scan(text):
    """원격 스캔 출력을 {노드: 신원} 으로. 마커(JSON)와 조각 둘 다 받는다."""
    out, name, buf = {}, None, []

    def flush():
        if not name:
            return
        # batches= 줄은 신원이 아니다. JSON 으로 읽기 전에 걷어내야 한다.
        count = 0
        body = []
        for line in buf:
            if line.startswith("batches="):
                try:
                    count = int(line.split("=", 1)[1].strip())
                except ValueError:
                    pass
            else:
                body.append(line)
        blob = "\n".join(body)
        ident = None
        try:
            ident = json.loads(blob)
        except Exception:                                    # noqa: BLE001
            ident = {}
            for key in ("fqdn", "machine_id"):
                for line in body:
                    hit = line.split('"%s"' % key, 1)
                    if len(hit) == 2:
                        val = hit[1].split(":", 1)[-1].strip().strip('}').strip()
                        val = val.strip('"').strip()
                        if val and val != "null":
                            ident[key] = val
                        break
        if isinstance(ident, dict) and ident.get("fqdn"):
            # 마커는 설치 때 그 이름으로 직접 쓴 것이라, 배치에서 긁어낸
            # 조각보다 믿을 만하다. 같은 장비에 이름이 여럿 남아 있을 때의
            # 우선순위가 된다.
            out[name] = {"ident": ident, "marker": bool(ident.get("node_id")),
                         "batches": count, "mtime": 0}

    for line in (text or "").splitlines():
        if line.startswith("== "):
            flush()
            name, buf = line[3:].strip(), []
        else:
            buf.append(line)
    flush()
    return out


def resolve_marker_ssh(cfg, node):
    """원격 inbox/<node>/_identity.json 하나만 읽는다."""
    from . import sshcmd

    tr = cfg.get("transport") or cfg
    root = tr.get("remote_root") or ""
    if not root:
        return None
    try:
        out = sshcmd.ssh_exec(tr, "cat %s 2>/dev/null" % sshcmd.shquote(
            "%s/inbox/%s/%s" % (root, node, MARKER)))
        ident = json.loads(out.strip() or "null")
    except Exception:                                        # noqa: BLE001
        return None
    return ident if isinstance(ident, dict) and ident.get("fqdn") else None


def write_marker_ssh(cfg, node, me=None):
    """NAS 를 마운트하지 않은 노드는 마커를 올려 둔다."""
    from . import sshcmd

    # sshcmd 는 전체 config 가 아니라 transport 하위 딕셔너리를 받는다.
    tr = cfg.get("transport") or cfg
    root = tr.get("remote_root") or ""
    if not root:
        return False
    me = dict(me or identity())
    me["node_id"] = node
    # 로컬과 같은 이유로, 남의 이름을 뺏지 않는다.
    other = resolve_marker_ssh(cfg, node)
    if other and not same_machine(me, other):
        return False
    tmp = os.path.join(tempfile.gettempdir(), "_identity.%d.json" % os.getpid())
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(me, fh)
        sshcmd.scp_put(tr, tmp, "%s/inbox/%s/%s" % (root, node, MARKER))
        return True
    except Exception:                                        # noqa: BLE001
        return False
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def resolve_ssh(cfg, me=None, exclude=()):
    from . import sshcmd

    tr = cfg.get("transport") or cfg
    root = tr.get("remote_root") or ""
    if not root:
        return None
    try:
        out = sshcmd.ssh_exec(tr, _REMOTE_SCAN.replace("@ROOT@", sshcmd.shquote(root)))
    except Exception:                                        # noqa: BLE001
        return None
    me = me or identity()
    # 로컬 모드와 같은 기준으로 고른다: 마커가 있으면 그것, 없으면 배치가 가장
    # 많은 이름. 한쪽만 알파벳순으로 고르면 같은 장비가 모드에 따라 다른 이름을
    # 갖게 된다.
    hits = [(v.get("marker", False), v.get("batches", 0), n)
            for n, v in parse_scan(out).items()
            if n not in exclude and same_machine(me, v["ident"])]
    if not hits:
        return None
    hits.sort(key=lambda h: (not h[0], -h[1], h[2]))
    return hits[0][2]


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--nas", required=True)
    ap.add_argument("--write", metavar="NODE", help="신원 마커를 남긴다")
    ap.add_argument("--exclude", default="")
    a = ap.parse_args()
    if a.write:
        write_marker(a.nas, a.write)
    else:
        print(resolve(a.nas, exclude=[x for x in a.exclude.split(",") if x]) or "")
