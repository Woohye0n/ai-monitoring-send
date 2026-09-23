#!/usr/bin/env bash
# ONE-SHOT setup for a sender server.
#
#   git clone <repo> ai-monitoring-send && cd ai-monitoring-send
#   SSH_PASSWORD='<NAS_PASSWORD>' ./setup.sh
#
# Default transport is SSH (for servers WITHOUT the NAS mounted): the batch is
# scp'd to the Synology NAS and the password is typed automatically.
# Creates config.json (chmod 600) and starts the sender in the background.
# Safe to re-run. Override via env or flags:
#
#   SSH_PASSWORD=...  HOST_ID=gpu7  INTERVAL=300 ./setup.sh
#   ./setup.sh --password '<NAS_PASSWORD>' --host gpu7
#   ./setup.sh --ssh-host aidaslab.synology.me --ssh-port 2244 --ssh-user synologynas \
#              --remote-root /volume1/nas-nfs/yunseok/ai-monitoring --password '...'
#   ./setup.sh --host gpu7 --claude-dir /data/work/.claude     # explicit .claude dir(s)
#   ./setup.sh --host gpu7 --claude-dir ~/.claude --claude-dir ~/.claude2   # repeatable
#   ./setup.sh --local --nas /mnt/nas/yunseok/ai-monitoring   # server HAS the mount
#   ./setup.sh --key ~/.ssh/id_ed25519                        # use an SSH key, no password
#   ./setup.sh --systemd                                      # print a systemd unit
set -euo pipefail
cd "$(dirname "$0")"
PY="${PYTHON:-python3}"

# clones/copies sometimes drop the executable bit — restore it (best effort) so
# that `./setup.sh`, `./start.sh`, ... work afterwards even if it was lost.
chmod +x ./*.sh 2>/dev/null || true

MODE="ssh"
NODE_ID="${HOST_ID:-${NODE_ID:-}}"
NODE_EXPLICIT=0   # --host 를 직접 줬는가
INTERVAL="${INTERVAL:-300}"
SSH_HOST="${SSH_HOST:-aidaslab.synology.me}"
SSH_PORT="${SSH_PORT:-2244}"
SSH_USER="${SSH_USER:-synologynas}"
SSH_PASSWORD="${SSH_PASSWORD:-}"
SSH_KEY="${SSH_KEY:-}"
REMOTE_ROOT="${REMOTE_ROOT:-/volume1/nas-nfs/yunseok/ai-monitoring}"
NAS_ROOT="${NAS_ROOT:-/mnt/nas/yunseok/ai-monitoring}"
USE_SYSTEMD=0
FORCE_SSH=0       # --ssh: do not auto-switch to local even if the NAS is mounted
AUTOSTART=1       # register a cron watchdog so a reboot does not end collection
CLAUDE_DIRS=()    # extra Claude config dir(s) — added to what discovery finds
CODEX_DIRS=()     # extra Codex dir(s) — added to what discovery finds

while [ $# -gt 0 ]; do
  case "$1" in
    --local) MODE="local"; shift;;
    --nas) NAS_ROOT="$2"; shift 2;;
    --host) NODE_ID="$2"; NODE_EXPLICIT=1; shift 2;;
    --interval) INTERVAL="$2"; shift 2;;
    --ssh-host) SSH_HOST="$2"; shift 2;;
    --ssh-port) SSH_PORT="$2"; shift 2;;
    --ssh-user) SSH_USER="$2"; shift 2;;
    --password) SSH_PASSWORD="$2"; shift 2;;
    --key) SSH_KEY="$2"; shift 2;;
    --remote-root) REMOTE_ROOT="$2"; shift 2;;
    --claude-dir) CLAUDE_DIRS+=("$2"); shift 2;;   # repeatable: explicit .claude dir
    --codex-dir) CODEX_DIRS+=("$2"); shift 2;;     # repeatable: explicit .codex dir
    --ssh) FORCE_SSH=1; shift;;
    --no-autostart) AUTOSTART=0; shift;;
    --systemd) USE_SYSTEMD=1; shift;;
    *) echo "unknown arg: $1"; exit 1;;
  esac
done

command -v "$PY" >/dev/null || { echo "python3 not found (set PYTHON=...)"; exit 1; }
PY_ABS="$(command -v "$PY")"

# 이미 config.json 이 있으면 거기 적힌 이름이 최우선이다. 업그레이드하려고
# 다시 돌릴 때 이름이 바뀌어 히스토리가 갈라지면 안 된다.
if [ "$NODE_EXPLICIT" = "0" ] && [ -z "$NODE_ID" ] && [ -f config.json ]; then
  NODE_ID="$("$PY" -c 'import json;print(json.load(open("config.json")).get("node_id") or "")' 2>/dev/null || true)"
  [ -n "$NODE_ID" ] && NODE_SRC="config.json"
fi


# If the NAS is already mounted, no credential is needed at all. Handing the NAS
# password to every user on a shared box is both a risk and an extra way for the
# install to fail, so prefer the mount whenever it is actually writable.
# `timeout ls` rather than `test -d`: a hung NFS mount must not block setup.
if [ "$MODE" = "ssh" ] && [ "$FORCE_SSH" = "0" ] && [ -z "$SSH_PASSWORD" ] && [ -z "$SSH_KEY" ]; then
  if timeout 5 ls -d "$NAS_ROOT" >/dev/null 2>&1 \
     && { [ -w "$NAS_ROOT/inbox" ] || [ -w "$NAS_ROOT" ]; }; then
    MODE="local"
    echo "NAS 가 $NAS_ROOT 에 마운트돼 있어 local 모드로 씁니다 (비밀번호 불필요)."
    echo "  SSH 전송을 강제하려면 --ssh 를 주세요."
  fi
fi

# 이 장비가 이미 어떤 이름으로 보고하고 있었는지 NAS 에 물어본다. 사람이
# --host 를 외워서 넣지 않아도 되고, 같은 서버가 이름 여러 개로 갈리지도
# 않는다. 판별은 fqdn 으로 한다 — 같은 컨테이너 이미지로 뜬 파드들은
# machine_id 가 전부 같아서, 그것만 보면 서로 다른 노드가 하나로 합쳐진다.
if [ "$NODE_EXPLICIT" = "0" ] && [ -z "$NODE_ID" ]; then
  if [ "$MODE" = "local" ]; then
    FOUND="$(PYTHONPATH=. "$PY" -m sender.node_name --nas "$NAS_ROOT" 2>/dev/null || true)"
  else
    # config.json 은 아직 없다(아래에서 만든다). 자격증명을 명령줄에 노출하지
    # 않도록 환경변수로 넘긴다.
    FOUND="$(SSH_HOST="$SSH_HOST" SSH_PORT="$SSH_PORT" SSH_USER="$SSH_USER" \
             SSH_PASSWORD="$SSH_PASSWORD" SSH_KEY="$SSH_KEY" REMOTE_ROOT="$REMOTE_ROOT" \
             PYTHONPATH=. "$PY" - <<'PYEOF' 2>/dev/null || true
import json, os
from sender import node_name
try:
    cfg = json.load(open("config.json"))
except OSError:
    cfg = {"transport": {
        "mode": "ssh",
        "ssh_host": os.environ.get("SSH_HOST", ""),
        "ssh_port": int(os.environ.get("SSH_PORT") or 22),
        "ssh_user": os.environ.get("SSH_USER", ""),
        "ssh_password": os.environ.get("SSH_PASSWORD", ""),
        "ssh_key": os.environ.get("SSH_KEY", ""),
        "remote_root": os.environ.get("REMOTE_ROOT", ""),
    }}
print(node_name.resolve_ssh(cfg) or "")
PYEOF
)"
  fi
  if [ -n "$FOUND" ]; then
    NODE_ID="$FOUND"; NODE_SRC="NAS 기록"
  fi
fi

# 아무 데서도 못 찾으면 호스트 이름. 새 장비의 첫 설치가 여기로 온다.
if [ -z "$NODE_ID" ]; then
  NODE_ID="$(hostname)"; NODE_SRC="hostname"
fi
[ "$NODE_EXPLICIT" = "1" ] && NODE_SRC="--host"

# A node name must identify the MACHINE, not the person installing it. Three
# users on one box each picked their own --host, so one server showed up as
# three on the dashboard. Reject names that cannot possibly be a server name.
case "$(printf '%s' "$NODE_ID" | tr 'A-Z' 'a-z')" in
  ""|localhost|localhost.localdomain|ubuntu|debian|server|servername|node|host|pc|desktop)
    echo "ERROR: 호스트 이름이 '$NODE_ID' 라 노드 이름으로 쓸 수 없습니다." >&2
    echo "       관리자가 정한 서버 이름으로 --host <이름> 을 주세요." >&2
    exit 1;;
esac

echo
echo "  노드 이름: $NODE_ID   (출처: ${NODE_SRC:-hostname}, 전송: $MODE)"
echo "  ⚠ 이 장비의 모든 사용자가 같은 노드 이름을 써야 합니다."
echo "    다르면 대시보드에 서버가 여러 대로 보입니다."
echo

# ask for the password if SSH mode and neither password nor key was given.
# 사람이 없는 자리(설치 스크립트, cron)에서는 물어볼 수 없다. 예전에는 그냥
# 프롬프트를 찍고 read 가 EOF 를 만나 set -e 로 죽었다 — 화면에는 프롬프트 한 줄만
# 남아서, 무엇이 잘못됐는지 알 수 없었다. 이제 무엇을 달라는지 말하고 멈춘다.
if [ "$MODE" = "ssh" ] && [ -z "$SSH_PASSWORD" ] && [ -z "$SSH_KEY" ]; then
  if [ -t 0 ]; then
    printf "SSH password for %s@%s: " "$SSH_USER" "$SSH_HOST" >&2
    read -rs SSH_PASSWORD; echo >&2
  else
    cat >&2 <<MSG
ERROR: NAS 가 마운트돼 있지 않아 SSH 전송인데, 비밀번호도 키도 없습니다.
       (대화형이 아니라 물어볼 수도 없습니다)

  SSH_PASSWORD='<NAS 비밀번호>' ./setup.sh --host $NODE_ID
  또는  ./setup.sh --key ~/.ssh/id_ed25519_nas --host $NODE_ID
  NAS 가 마운트된 서버라면  --local --nas <마운트 경로>
MSG
    exit 2
  fi
fi

# A passphrase-protected key works when you ssh by hand (ssh-agent holds it) but
# NOT for the sender: it runs in the background with BatchMode=yes and no agent,
# so every delivery fails with a confusing permission error. Catch it here.
if [ -n "$SSH_KEY" ]; then
  KEY_PATH="${SSH_KEY/#\~/$HOME}"
  if [ ! -f "$KEY_PATH" ]; then
    echo "ssh key not found: $KEY_PATH" >&2; exit 1
  fi
  if ! ssh-keygen -y -P '' -f "$KEY_PATH" >/dev/null 2>&1; then
    cat >&2 <<MSG
ERROR: $KEY_PATH 에 passphrase 가 걸려 있습니다.

sender 는 백그라운드에서 ssh-agent 없이 돌기 때문에 이 키로는 배송이 계속
실패합니다(손으로 ssh 하면 agent 가 대신 풀어주므로 되는 것처럼 보입니다).

NAS 전용 passphrase 없는 키를 따로 만드세요 — 기존 키의 passphrase 를 벗기는
것보다 안전합니다(그 키는 GitHub 등 다른 곳에도 쓰일 수 있습니다):

  ssh-keygen -t ed25519 -N '' -f ~/.ssh/id_ed25519_nas -q
  cat ~/.ssh/id_ed25519_nas.pub      # 이 줄을 NAS 의 ~/.ssh/authorized_keys 에 추가
  ./setup.sh --key ~/.ssh/id_ed25519_nas --host <서버이름>
MSG
    exit 1
  fi
fi

# 1) create config.json (chmod 600 — it can hold the password)
CONFIG_WRITTEN=0
if [ ! -f config.json ]; then
  CONFIG_WRITTEN=1
  echo "creating config.json (mode=$MODE, node=$NODE_ID)"
  CLAUDE_DIRS_ENV=""; CODEX_DIRS_ENV=""
  if [ ${#CLAUDE_DIRS[@]} -gt 0 ]; then CLAUDE_DIRS_ENV=$(printf '%s\n' "${CLAUDE_DIRS[@]}"); fi
  if [ ${#CODEX_DIRS[@]} -gt 0 ]; then CODEX_DIRS_ENV=$(printf '%s\n' "${CODEX_DIRS[@]}"); fi
  CLAUDE_DIRS_ENV="$CLAUDE_DIRS_ENV" CODEX_DIRS_ENV="$CODEX_DIRS_ENV" \
  "$PY" - "$MODE" "$NODE_ID" "$INTERVAL" "$SSH_HOST" "$SSH_PORT" "$SSH_USER" \
        "$SSH_PASSWORD" "$SSH_KEY" "$REMOTE_ROOT" "$NAS_ROOT" <<'PYEOF'
import json, sys, os
mode,node,interval,host,port,user,pw,key,remote,nas = sys.argv[1:11]
cfg = json.load(open("config.example.json"))
cfg["node_id"] = node
cfg["interval_seconds"] = int(interval)
cfg["nas_root"] = nas
cfg["transport"] = {
    "mode": mode, "ssh_host": host, "ssh_port": int(port), "ssh_user": user,
    "ssh_password": pw, "ssh_key": key, "remote_root": remote,
}
cd = [x for x in os.environ.get("CLAUDE_DIRS_ENV", "").splitlines() if x.strip()]
xd = [x for x in os.environ.get("CODEX_DIRS_ENV", "").splitlines() if x.strip()]
if cd:
    cfg["claude"]["config_dirs"] = cd
if xd:
    cfg["codex"]["dirs"] = xd
json.dump(cfg, open("config.json", "w"), indent=2)
print("  wrote config.json" + (f"  claude_dirs={cd}" if cd else "")
      + (f"  codex_dirs={xd}" if xd else ""))
PYEOF
  chmod 600 config.json
else
  # --host 를 직접 준 경우에만 이름을 제자리에서 고친다. 예전에는 이름을 바꾸려면
  # config.json 을 지우는 수밖에 없었는데, 그러면 NAS 자격증명까지 같이 날아가
  # 비밀번호를 다시 받아야 했다. 이름 하나 고치자고 치를 대가가 아니다.
  if [ "$NODE_EXPLICIT" = "1" ]; then
    "$PY" - "$NODE_ID" <<'PYEOF'
import json, sys
want = sys.argv[1]
cfg = json.load(open("config.json"))
if cfg.get("node_id") == want:
    print("config.json already exists — leaving it as-is")
else:
    was = cfg.get("node_id")
    cfg["node_id"] = want
    json.dump(cfg, open("config.json", "w"), indent=2)
    print(f"  노드 이름을 '{was}' → '{want}' 로 바꿨습니다 (자격증명은 그대로)")
PYEOF
    chmod 600 config.json
  else
    echo "config.json already exists — leaving it as-is (delete it to reconfigure)"
  fi
fi

# 2) 돌고 있던 송신기를 먼저 멈춘다.
#
# 예전에는 config.json 이 새로 쓰였을 때만 멈췄다. 그런데 setup.sh 를 다시 도는
# 가장 흔한 이유는 **코드 업그레이드**이고, 그때 config 는 그대로다. 그러면
# start.sh 가 "already running" 만 찍고 끝나, 옛 프로세스가 옛 코드로 계속
# 돌았다 — 배포한 줄 알았는데 아무것도 안 바뀌는, 알아채기 어려운 실패다.
# one-shot 전에 멈춰야 같은 상태 파일을 두 프로세스가 건드리지도 않는다.
if [ -f data/sender.pid ] && kill -0 "$(cat data/sender.pid 2>/dev/null)" 2>/dev/null; then
  echo "기존 송신기를 멈춥니다 (새 코드/설정으로 다시 띄우기 위해)"
  bash stop.sh
fi

# 3) one-shot test (actually collects + delivers one batch -> validates SSH/creds)
echo "running a one-shot collection + delivery test…"
PYTHONPATH=. "$PY" -m sender.main --once || {
  echo "one-shot failed — check the SSH host/port/user/password above"; exit 1; }

# 2b) 신원 마커를 남긴다. 다음 설치가 --host 없이도 이 장비의 이름을 한 번의
# 조회로 찾아낸다. 그리고 --host 를 직접 준 경우에만, 같은 장비가 다른 이름으로
# 이미 보고 중인지 확인해 준다 — 자동으로 고른 이름은 애초에 그 조회 결과다.
"$PY" - "$NAS_ROOT" "$NODE_ID" "$MODE" "$NODE_EXPLICIT" <<'PYEOF' || true
import json, os, sys
sys.path.insert(0, os.getcwd())
from sender import node_name

nas, node, mode, explicit = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4] == "1"
me = node_name.identity()
taken = None       # 이 이름을 이미 쓰고 있는 '다른' 장비
if mode == "local":
    claim = node_name.claimed_by(nas, node)
    if claim and not node_name.same_machine(me, claim):
        taken = claim
    node_name.write_marker(nas, node, me)
    other = node_name.resolve(nas, me, exclude=[node])
else:
    try:
        cfg = json.load(open("config.json"))
    except OSError:
        cfg = {}
    if cfg:
        claim = node_name.resolve_marker_ssh(cfg, node)
        if claim and not node_name.same_machine(me, claim):
            taken = claim
        node_name.write_marker_ssh(cfg, node, me)
        other = node_name.resolve_ssh(cfg, me, exclude=[node])
    else:
        other = None
if taken:
    # 실수로 남의 노드 이름을 준 경우. gpu-1-0 에서 --host kakao-b200-2 를
    # 줘서 두 장비가 한 이름으로 보고한 적이 있다. 조용히 넘어가면 대시보드에서
    # 두 노드의 사용량이 한 덩어리로 섞인다.
    print(f"  \u26a0 '{node}' 는 이미 다른 장비({taken.get('fqdn')})가 쓰는 이름입니다.")
    print(f"    이 장비는 {me.get('fqdn')} 입니다. 두 장비가 한 이름으로 보고하면"
          f" 사용량이 섞입니다.")
    print(f"    이 장비의 올바른 이름으로 다시 돌리세요:  ./setup.sh --host <이 장비 이름>")
if other and explicit:
    print(f"  \u26a0 이 장비는 '{other}' 라는 이름으로도 보고되고 있습니다.")
    print(f"    한 장비는 이름 하나여야 합니다. --host 를 빼고 다시 돌리면"
          f" 자동으로 '{other}' 를 씁니다.")
PYEOF

if [ "$USE_SYSTEMD" = "1" ]; then
  cat <<UNIT

# --- copy to /etc/systemd/system/aidas-sender.service then: systemctl enable --now ---
[Unit]
Description=AIDAS monitoring sender
After=network-online.target

[Service]
Type=simple
User=$(whoami)
WorkingDirectory=$(pwd)
ExecStart=$(command -v "$PY") -m sender.main
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT
  exit 0
fi

# 4) start in the background (use `bash` so it works even before chmod takes hold)
bash start.sh

# 5) survive a reboot. `nohup &` does not, which is why several nodes on this
# cluster simply stopped reporting weeks ago with nobody noticing. cron needs no
# root and exists everywhere; start.sh is a no-op when the sender is alive, so
# the same line doubles as a watchdog. PYTHON is pinned because cron's PATH is
# minimal and python3 is often outside it (conda, pyenv, ...).
if [ "$AUTOSTART" = "1" ] && [ "$USE_SYSTEMD" != "1" ]; then
  DIR="$(pwd)"
  MARK="# aidas ai-monitoring-send ($DIR)"
  if ! command -v crontab >/dev/null 2>&1; then
    echo "  ⚠ crontab 이 없어 자동 재시작을 등록하지 못했습니다."
    echo "    재부팅하면 수집이 조용히 멈춥니다 — systemd 를 쓰거나(./setup.sh --systemd)"
    echo "    로그인 스크립트에 $DIR/start.sh 를 넣어 두세요."
  elif crontab -l 2>/dev/null | grep -qF "$MARK"; then
    echo "  자동 재시작: 이미 등록돼 있습니다"
  else
    { crontab -l 2>/dev/null
      echo "$MARK"
      echo "@reboot PYTHON=$PY_ABS $DIR/start.sh >/dev/null 2>&1"
      echo "*/10 * * * * PYTHON=$PY_ABS $DIR/start.sh >/dev/null 2>&1"
    } | crontab - && echo "  자동 재시작 등록: 재부팅 후 + 10분마다 점검 (살아 있으면 아무것도 안 함)"
  fi
fi

echo "Done. The central dashboard will show host '$NODE_ID' within ~1 minute."
