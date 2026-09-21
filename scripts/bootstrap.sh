#!/usr/bin/env bash
# 이 서버의 모든 사용자에게 송신기를 깔고 켠다. **인자 없이** 실행해도 된다.
#
#   sudo ./scripts/bootstrap.sh
#
# 알아서 하는 것 — 물어보지 않아도 되는 건 묻지 않는다:
#   노드 이름   이미 이 장비가 보고하던 이름을 찾아 쓴다 (없으면 hostname)
#   NAS 자격증명 이미 설치된 sender 의 config.json 에서 가져온다
#               (NAS 가 마운트된 서버면 애초에 필요 없다)
#   둘 다 없을 때만 그때 터미널에서 물어본다
#
# 왜 이렇게: 예전 안내는 "dist 를 scp 로 받고, 비번을 파일로 쓰고, 설치하고,
# 파일을 지우고" 네 단계였다. 서버가 여러 대면 그 네 단계를 매번 정확히 반복해야
# 한다 — 한 번만 틀려도 조용히 실패한다(실제로 그랬다).
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAS_MOUNT="${NAS_ROOT:-/mnt/nas/yunseok/ai-monitoring}"
NODE=""; PASS=()                     # PASS: install-all-users 로 그대로 넘길 인자
while [ $# -gt 0 ]; do
  case "$1" in
    --host) NODE="${2:-}"; shift 2;;
    -*) PASS+=("$1"); shift;;        # --dry-run --all …
    *) [ -z "$NODE" ] && NODE="$1" || PASS+=("$1"); shift;;
  esac
done

say() { printf '%s\n' "$*"; }
ask() {   # 파이프로 실행돼도(curl|bash) 터미널에서 읽는다
  local prompt="$1" var="$2" silent="${3:-}" val=""
  if [ -r /dev/tty ]; then
    if [ -n "$silent" ]; then printf '%s' "$prompt" > /dev/tty; read -rs val < /dev/tty; echo > /dev/tty
    else printf '%s' "$prompt" > /dev/tty; read -r val < /dev/tty; fi
  fi
  printf -v "$var" '%s' "$val"
}

[ "$(id -u)" = "0" ] || { echo "root 로 실행하세요:  sudo $0" >&2; exit 1; }

# scp -r 로 받아오면 실행 비트가 떨어져 있을 수 있다. 여기서 되살린다 —
# 안 그러면 "Permission denied" 한 줄로 끝나고 원인이 안 보인다.
chmod +x "$ROOT"/*.sh "$ROOT"/scripts/*.sh 2>/dev/null || true

# ---- 1) 이미 설치된 sender 에서 설정을 물려받는다 -------------------------
say "1/4  기존 설치 찾는 중…"
EXIST="$(find /home /root -maxdepth 4 -name config.json -path '*ai-monitoring-send*' \
          -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -1 | cut -d' ' -f2-)"
FOUND_NODE=""; FOUND_PW=""; FOUND_KEY=""; FOUND_MODE=""
if [ -n "$EXIST" ]; then
  eval "$(python3 - "$EXIST" <<'PY'
import json, shlex, sys
try:
    c = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    raise SystemExit
t = c.get("transport") or {}
for k, v in (("FOUND_NODE", c.get("node_id")), ("FOUND_MODE", t.get("mode")),
             ("FOUND_PW", t.get("ssh_password")), ("FOUND_KEY", t.get("ssh_key"))):
    print(f"{k}={shlex.quote(str(v or ''))}")
PY
)"
  say "     찾음: ${EXIST/#\/home\//~…/}  (노드=${FOUND_NODE:-?} 전송=${FOUND_MODE:-?})"
else
  say "     없음 — 이 서버의 첫 설치입니다"
fi

# ---- 2) 노드 이름 ----------------------------------------------------------
if [ -z "$NODE" ]; then
  NODE="$FOUND_NODE"
  # NAS 가 보이면 같은 장비가 어떤 이름으로 보고 중인지 직접 확인한다
  if [ -z "$NODE" ] && timeout 5 ls -d "$NAS_MOUNT/inbox" >/dev/null 2>&1; then
    NODE="$(python3 - "$NAS_MOUNT/inbox" <<'PY'
import glob, gzip, json, os, socket, sys
def ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.settimeout(1); s.connect(("8.8.8.8", 53)); return s.getsockname()[0]
    except OSError: return None
    finally: s.close()
fq, myip = socket.getfqdn(), ip()
# 한 장비가 여러 이름으로 보고 중일 수 있다(이름을 새로 지으면 그렇게 된다).
# 그럴 때는 **이력이 가장 긴 이름**을 고른다 — 알파벳순으로 아무거나 집으면
# 방금 잘못 생긴 이름이 정답이 되어 버린다.
cands = []
for d in sorted(glob.glob(os.path.join(sys.argv[1], "*"))):
    files = sorted(glob.glob(os.path.join(d, "batch-*.json*")))
    if not files: continue
    try:
        f = files[-1]
        j = json.load(gzip.open(f) if f.endswith(".gz") else open(f, "rb"))
    except Exception: continue
    if j.get("fqdn") == fq and j.get("ip") == myip:
        cands.append((len(files), os.path.basename(d)))
cands.sort(reverse=True)
if len(cands) > 1:
    others = ", ".join(n for _, n in cands[1:])
    sys.stderr.write(f"     이 장비가 여러 이름으로 보고 중입니다: {others} "
                     f"→ 이력이 가장 긴 '{cands[0][1]}' 로 통일합니다\n")
print(cands[0][1] if cands else "")
PY
)"
    [ -n "$NODE" ] && say "     NAS 기록에서 이 장비 이름을 찾음: $NODE"
  fi
  [ -z "$NODE" ] && NODE="$(hostname)"
fi
say "2/4  노드 이름: $NODE   (이 장비의 모든 사용자가 같은 이름을 씁니다)"

# ---- 3) 자격증명 — 필요할 때만 -------------------------------------------
CRED=()
if timeout 5 ls -d "$NAS_MOUNT" >/dev/null 2>&1; then
  say "3/4  NAS 가 마운트돼 있어 자격증명이 필요 없습니다"
elif [ -n "$FOUND_KEY" ] && [ -f "$FOUND_KEY" ]; then
  CRED=(--key "$FOUND_KEY"); say "3/4  기존 설치의 SSH 키를 씁니다: $FOUND_KEY"
elif [ -n "$FOUND_PW" ]; then
  PWF="$(mktemp)"; chmod 600 "$PWF"; printf '%s' "$FOUND_PW" > "$PWF"
  CRED=(--password-file "$PWF"); say "3/4  기존 설치의 NAS 비밀번호를 씁니다 (다시 묻지 않습니다)"
else
  say "3/4  NAS 마운트도, 기존 자격증명도 없습니다"
  ask "     NAS 비밀번호: " PW silent
  [ -n "${PW:-}" ] || { echo "     비밀번호가 필요합니다." >&2; exit 1; }
  PWF="$(mktemp)"; chmod 600 "$PWF"; printf '%s' "$PW" > "$PWF"; unset PW
  CRED=(--password-file "$PWF")
fi
trap '[ -n "${PWF:-}" ] && shred -u "$PWF" 2>/dev/null || true' EXIT

# ---- 4) 전원 설치 ----------------------------------------------------------
say "4/4  설치"
"$ROOT/scripts/install-all-users.sh" --host "$NODE" "${CRED[@]}" ${PASS[@]+"${PASS[@]}"}
