#!/usr/bin/env bash
# github.io 가 막힌 서버에서 NAS 만으로 송신기를 갱신한다.
#
#   sudo bash scripts/update-from-nas.sh
#
# 카카오 b200 클러스터는 바깥으로 HTTPS 가 나가지 않는다
# (curl: (35) Connection reset by peer). 하지만 NAS 는 열려 있다 — 배치를
# 거기로 보내고 있으니까. 자격증명은 이미 config.json 에 있으므로 비밀번호를
# 다시 받을 필요도 없다.
set -uo pipefail
DIST_NAME="ai-monitoring-send-dist"
NAS_MOUNT="${NAS_ROOT:-/mnt/nas/yunseok/ai-monitoring}"
say() { printf '%s\n' "$*"; }

[ "$(id -u)" = "0" ] || { say "root 로 실행해야 합니다:  sudo bash $0 $*"; exit 1; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# 1) NAS 가 마운트돼 있으면 그냥 복사한다. 제일 단순한 길을 먼저 본다.
DIST_MOUNT="$(dirname "$NAS_MOUNT")/$DIST_NAME"
if timeout 5 ls -d "$DIST_MOUNT" >/dev/null 2>&1; then
  say "NAS 마운트에서 배포본을 가져옵니다: $DIST_MOUNT"
  cp -a "$DIST_MOUNT" "$WORK/dist" || { say "복사 실패"; exit 1; }
else
  # 2) 아니면 이미 깔려 있는 송신기의 자격증명으로 scp 한다. 그 송신기의
  #    sshcmd.py 가 Synology chroot(-O 필요)와 비밀번호 pty 를 이미 처리한다.
  SRC=""
  for c in /home/*/ai-monitoring-send/config.json /root/ai-monitoring-send/config.json; do
    [ -f "$c" ] || continue
    python3 -c "
import json,sys
t=json.load(open('$c')).get('transport') or {}
sys.exit(0 if t.get('ssh_host') and (t.get('ssh_password') or t.get('ssh_key')) else 1)" 2>/dev/null \
      && { SRC="$(dirname "$c")"; break; }
  done
  [ -n "$SRC" ] || {
    say "NAS 마운트도 없고, 자격증명이 든 기존 설치도 못 찾았습니다."
    say "  찾아본 곳: /home/*/ai-monitoring-send/config.json"
    exit 1; }

  say "기존 설치의 자격증명을 씁니다: $SRC/config.json  (비밀번호를 다시 묻지 않습니다)"
  PYTHONPATH="$SRC" DEST="$WORK/dist" SRC="$SRC" DIST_NAME="$DIST_NAME" \
    python3 - <<'PY' || { say "내려받기 실패"; exit 1; }
import json, os, sys

src, dest = os.environ["SRC"], os.environ["DEST"]
sys.path.insert(0, src)
from sender import sshcmd                                   # noqa: E402

tr = json.load(open(os.path.join(src, "config.json")))["transport"]
remote = os.path.dirname(tr["remote_root"]) + "/" + os.environ["DIST_NAME"]
sshcmd.scp_get(tr, remote, dest, recursive=True)
PY
fi

[ -f "$WORK/dist/scripts/bootstrap.sh" ] || { say "받은 배포본이 이상합니다"; exit 1; }
say "버전: $(cat "$WORK/dist/VERSION" 2>/dev/null || echo '(알 수 없음)')"
exec bash "$WORK/dist/scripts/bootstrap.sh" "$@"
