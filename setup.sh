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
NODE_ID="${HOST_ID:-${NODE_ID:-$(hostname)}}"
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
    --host) NODE_ID="$2"; shift 2;;
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

# A node name must identify the MACHINE, not the person installing it. Three
# users on one box each picked their own --host, so one server showed up as
# three on the dashboard. Reject names that cannot possibly be a server name.
case "$(printf '%s' "$NODE_ID" | tr 'A-Z' 'a-z')" in
  ""|localhost|localhost.localdomain|ubuntu|debian|server|servername|node|host|pc|desktop)
    echo "ERROR: 호스트 이름이 '$NODE_ID' 라 노드 이름으로 쓸 수 없습니다." >&2
    echo "       관리자가 정한 서버 이름으로 --host <이름> 을 주세요." >&2
    exit 1;;
esac

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

echo
echo "  노드 이름: $NODE_ID   (전송: $MODE)"
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
  echo "config.json already exists — leaving it as-is (delete it to reconfigure)"
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

# 2b) Is this machine already reporting under a different name? Every batch now
# carries a hashed machine id, so when the NAS is readable we can simply look.
# Catching it here is the only cheap moment: afterwards it is a dashboard that
# quietly shows one server as several.
if [ "$MODE" = "local" ]; then
  "$PY" - "$NAS_ROOT" "$NODE_ID" <<'PYEOF' || true
import glob, gzip, hashlib, json, os, socket, sys

nas, node = sys.argv[1], sys.argv[2]


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


mine, fqdn, ip = machine_id(), socket.getfqdn(), local_ip()
for d in sorted(glob.glob(os.path.join(nas, "inbox", "*"))):
    other = os.path.basename(d)
    if other == node or not os.path.isdir(d):
        continue
    batches = sorted(glob.glob(os.path.join(d, "batch-*.json*")))[-1:]
    if not batches:
        continue
    try:
        f = batches[0]
        data = json.load(gzip.open(f) if f.endswith(".gz") else open(f, "rb"))
    except Exception:                                       # noqa: BLE001
        continue
    # machine_id only exists in batches written by an upgraded sender, so on the
    # first rollout fall back to fqdn+ip -- which is what actually exposed the
    # three names on this cluster in the first place.
    same = (mine and data.get("machine_id") == mine) or (
        fqdn and ip and data.get("fqdn") == fqdn and data.get("ip") == ip)
    if same:
        print(f"  ⚠ 이 장비는 이미 '{other}' 라는 이름으로 보고되고 있습니다"
              f" (os_user={data.get('os_user')}).")
        print(f"    같은 이름을 쓰세요:  rm -f config.json && ./setup.sh --host {other}")
PYEOF
fi

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
