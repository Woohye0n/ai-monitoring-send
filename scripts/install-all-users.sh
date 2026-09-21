#!/usr/bin/env bash
# 이 서버의 모든 사용자에게 송신기를 설치/갱신한다. **root 로 실행한다.**
#
#   sudo ./scripts/install-all-users.sh --host <노드이름> --dry-run
#   sudo ./scripts/install-all-users.sh --host <노드이름>
#
# 왜 root 가 필요한가: 송신기는 **그 사용자의 홈만** 읽는다(/proc 권한이 남의
# 것을 막아 준다 — 그게 맞는 경계다). 그래서 사람마다 자기 계정으로 하나씩
# 돌아야 하고, 남의 홈에 설치하려면 root 여야 한다.
#
# 노드 이름은 **장비 이름**이다. 사람마다 다른 이름을 주면 대시보드에 서버가
# 여러 대로 보인다(실제로 한 장비가 세 대로 보이고 있었다). 전원이 같은 이름을
# 쓰도록 이 스크립트가 하나로 강제한다.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="${SRC:-$ROOT}"                 # 배포 원본 (기본: 이 체크아웃)
NODE=""; DRY=0; ALL=0
DEST_NAME="ai-monitoring-send"

while [ $# -gt 0 ]; do
  case "$1" in
    --host) NODE="$2"; shift 2;;
    --src) SRC="$2"; shift 2;;
    --dry-run) DRY=1; shift;;
    --all) ALL=1; shift;;           # AI 도구 흔적이 없는 사용자도 포함
    *) echo "알 수 없는 인자: $1" >&2; exit 1;;
  esac
done

[ -n "$NODE" ] || { echo "--host <노드이름> 이 필요합니다 (이 장비의 이름, 전원 동일)" >&2; exit 1; }
[ -f "$SRC/setup.sh" ] || { echo "원본이 아닙니다: $SRC" >&2; exit 1; }
if [ "$(id -u)" != "0" ] && [ "$DRY" = "0" ]; then
  echo "root 로 실행하세요 (sudo). --dry-run 은 그냥 됩니다." >&2; exit 1
fi

uses_ai() {   # claude/codex 를 쓰는 흔적이 있나
  local home="$1" user="$2"
  compgen -G "$home/.claude*" >/dev/null 2>&1 && return 0
  compgen -G "$home/.codex*" >/dev/null 2>&1 && return 0
  pgrep -u "$user" -f '(^|/)(claude|codex)' >/dev/null 2>&1 && return 0
  return 1
}

if [ "$(id -u)" != "0" ]; then
  echo "⚠ root 가 아니라 남의 홈을 볼 수 없습니다 — '흔적 없음' 판정이 틀릴 수 있습니다."
  echo "  (root 로 돌리면 정확합니다. 실제 설치도 root 여야 합니다.)"
  echo
fi
printf '%-14s %-8s %s\n' "사용자" "상태" "비고"
printf '%s\n' "------------------------------------------------------------"
# 여러 계정이 같은 홈을 쓰는 서버가 있다(kakao 계열은 전원이 /home/jovyan 이다).
# 그런 곳에 사람 수만큼 설치하면 같은 디렉토리를 덮어쓰고, 같은 홈을 읽는 송신기가
# 여러 개 돌아 같은 배치를 중복으로 올린다. 홈 하나당 한 번만 설치한다.
declare -A DONE_HOME=()
ok=0; skip=0; fail=0
while IFS=: read -r user _ uid _ _ home shell; do
  [ "$uid" -ge 1000 ] 2>/dev/null || continue
  [ "$uid" -lt 65534 ] || continue
  case "$shell" in */nologin|*/false) continue;; esac
  [ -d "$home" ] || continue

  real_home="$(readlink -f "$home" 2>/dev/null || echo "$home")"
  if [ -n "${DONE_HOME[$real_home]:-}" ]; then
    printf '%-14s %-8s %s\n' "$user" "건너뜀" "홈을 ${DONE_HOME[$real_home]} 와 공유 ($real_home)"
    skip=$((skip+1)); continue
  fi

  if [ "$ALL" = "0" ] && ! uses_ai "$home" "$user"; then
    printf '%-14s %-8s %s\n' "$user" "건너뜀" "claude/codex 흔적 없음"
    skip=$((skip+1)); continue
  fi

  dest="$home/$DEST_NAME"
  note="새 설치"; [ -d "$dest" ] && note="갱신"
  DONE_HOME[$real_home]="$user"
  if [ "$DRY" = "1" ]; then
    printf '%-14s %-8s %s\n' "$user" "예정" "$note → $dest"
    ok=$((ok+1)); continue
  fi

  # config.json 과 data/ 는 그 서버·그 사용자의 것이므로 건드리지 않는다.
  mkdir -p "$dest"
  if ! rsync -rlt --delete --exclude config.json --exclude data --exclude '.git' \
             --exclude '__pycache__' "$SRC/" "$dest/" >/dev/null 2>&1; then
    printf '%-14s %-8s %s\n' "$user" "실패" "복사 실패"; fail=$((fail+1)); continue
  fi
  chown -R "$user" "$dest" 2>/dev/null
  chmod +x "$dest"/*.sh 2>/dev/null

  # 그 사용자의 환경으로 설치한다. setup.sh 는 이미 있는 config.json 을 건드리지
  # 않고 cron 등록과 재시작만 한다.
  out="$(runuser -u "$user" -- env HOME="$home" bash -c \
        "cd '$dest' && ./setup.sh --host '$NODE' 2>&1")"
  if printf '%s' "$out" | grep -q '^Done\.'; then
    surf="$(printf '%s' "$out" | grep -o 'surfaces\[[^]]*\]' | head -1)"
    printf '%-14s %-8s %s\n' "$user" "OK" "$note ${surf:-}"
    ok=$((ok+1))
  else
    printf '%-14s %-8s %s\n' "$user" "실패" "$(printf '%s' "$out" | tail -1 | cut -c1-60)"
    fail=$((fail+1))
  fi
done < /etc/passwd

printf '%s\n' "------------------------------------------------------------"
echo "완료: OK $ok · 건너뜀 $skip · 실패 $fail"
[ "$DRY" = "1" ] && echo "(--dry-run 이었습니다. 실제로 설치하려면 빼고 다시 실행하세요)"
echo
echo "확인:  python3 $SRC/scripts/fleet-status.py --nas /mnt/nas/yunseok/ai-monitoring"
