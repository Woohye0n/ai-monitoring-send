#!/usr/bin/env bash
# 이 서버의 모든 사용자에게 송신기를 설치/갱신한다. **root 로 실행한다.**
#
#   sudo ./scripts/install-all-users.sh                       # 이름 자동
#   sudo ./scripts/install-all-users.sh --dry-run
#   sudo ./scripts/install-all-users.sh --host <노드이름>   # 새 장비에 이름을 붙일 때
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
PWFILE=""; SSH_KEY="${SSH_KEY:-}"
DEST_NAME="ai-monitoring-send"

while [ $# -gt 0 ]; do
  case "$1" in
    --host) NODE="$2"; shift 2;;
    --src) SRC="$2"; shift 2;;
    --dry-run) DRY=1; shift;;
    --all) ALL=1; shift;;           # AI 도구 흔적이 없는 사용자도 포함
    --password-file) PWFILE="$2"; shift 2;;   # NAS 비밀번호가 담긴 파일
    --key) SSH_KEY="$2"; shift 2;;            # 비밀번호 대신 SSH 키
    *) echo "알 수 없는 인자: $1" >&2; exit 1;;
  esac
done

# --host 는 이제 선택이다. 비워 두면 각 사용자의 setup.sh 가 NAS 에 남은 기록에서
# 이 장비의 이름을 스스로 찾고, 없으면 hostname 을 쓴다. 한 장비의 사용자들이
# 제각기 다른 이름을 고르던 문제도 같이 없어진다.
if [ -z "$NODE" ]; then
  echo "노드 이름을 지정하지 않았습니다 — NAS 기록에서 자동으로 찾습니다."
fi
[ -f "$SRC/setup.sh" ] || { echo "원본이 아닙니다: $SRC" >&2; exit 1; }
if [ "$(id -u)" != "0" ] && [ "$DRY" = "0" ]; then
  echo "root 로 실행하세요 (sudo). --dry-run 은 그냥 됩니다." >&2; exit 1
fi

# NAS 가 마운트돼 있으면 자격증명이 필요 없다. 없으면 SSH 전송이라 필요하다.
# 사람마다 실패를 반복하기 전에 여기서 한 번에 막는다 — 예전에는 setup.sh 가
# 프롬프트를 찍고 EOF 로 죽어, 사용자마다 같은 침묵이 반복됐다.
NAS_MOUNT="${NAS_ROOT:-/mnt/nas/yunseok/ai-monitoring}"
[ -n "$PWFILE" ] && SSH_PASSWORD="$(cat "$PWFILE")"
SSH_PASSWORD="${SSH_PASSWORD:-}"
if ! timeout 5 ls -d "$NAS_MOUNT" >/dev/null 2>&1; then
  if [ -z "$SSH_PASSWORD" ] && [ -z "$SSH_KEY" ]; then
    cat >&2 <<MSG
ERROR: NAS 가 $NAS_MOUNT 에 마운트돼 있지 않아 SSH 전송입니다.
       비밀번호나 키가 필요합니다.

  sudo ./scripts/install-all-users.sh --host $NODE --password-file <비번파일>
  sudo ./scripts/install-all-users.sh --host $NODE --key /path/to/id_ed25519_nas

비번을 파일로 두기 싫으면:
  printf '%s' '<비번>' > /dev/shm/nas.pw && chmod 600 /dev/shm/nas.pw
  sudo ./scripts/install-all-users.sh --host $NODE --password-file /dev/shm/nas.pw
  shred -u /dev/shm/nas.pw
MSG
    exit 1
  fi
  echo "NAS 미마운트 → SSH 전송으로 설치합니다 ($([ -n "$SSH_KEY" ] && echo '키' || echo '비밀번호'))"
else
  echo "NAS 가 $NAS_MOUNT 에 마운트돼 있어 자격증명 없이 설치합니다"
fi

EXCLUDES=(config.json data .git __pycache__)

# rsync 가 없는 서버가 있다(최소 구성 컨테이너). tar 로 대신한다 — 다만 tar 는
# 지우지 못하므로 위에 덮어쓰기만 된다(없어진 파일이 남을 수 있다).
copy_tree() {
  local src="$1" dest="$2" err
  if command -v rsync >/dev/null 2>&1; then
    local args=(-rlt --delete)
    local e; for e in "${EXCLUDES[@]}"; do args+=(--exclude "$e"); done
    err="$(rsync "${args[@]}" "$src/" "$dest/" 2>&1)" && return 0
    # 마지막 줄은 보통 "error in file IO (code 11)" 같은 총평이라 쓸모가 적다.
    # 원인이 적힌 첫 줄을 함께 보여준다.
    printf 'rsync: %s' "$(printf '%s' "$err" | grep -m1 -v '^$' | cut -c1-120)"
    printf ' | %s\n' "$(printf '%s' "$err" | tail -1 | cut -c1-60)"
    return 1
  fi
  local targs=(); local e
  for e in "${EXCLUDES[@]}"; do targs+=(--exclude "$e"); done
  err="$( { tar -C "$src" "${targs[@]}" -cf - . | tar -C "$dest" -xf - ; } 2>&1 )" && return 0
  echo "tar: $(printf '%s' "$err" | grep -m1 -v '^$' | cut -c1-140)"; return 1
}

# runuser 가 없는 서버도 있다.
run_as() {
  local user="$1" home="$2" cmd="$3"
  if command -v runuser >/dev/null 2>&1; then
    runuser -u "$user" -- env HOME="$home" bash -c "$cmd" 2>&1
  else
    su -s /bin/bash "$user" -c "export HOME='$home'; $cmd" 2>&1
  fi
}

# 키가 안 먹는 걸 그 자리에서 알았을 때, 죽기 전에 한 번 물어본다. 한 번만 묻고
# 나머지 사용자에게도 그대로 쓴다.
ensure_password() {
  [ -n "${SSH_PASSWORD:-}" ] && return 0
  { : > /dev/tty; } 2>/dev/null || return 1
  printf '     NAS 비밀번호 (SSH 키를 쓸 수 없어 필요합니다): ' > /dev/tty
  read -rs SSH_PASSWORD < /dev/tty; echo > /dev/tty
  [ -n "$SSH_PASSWORD" ]
}

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
#
# **누구로 설치하느냐가 중요하다.** /etc/passwd 순서대로 첫 사람을 잡으면 실제로
# 그 홈을 쓰는 사람이 아닌 옛 계정(jovyan)이 걸린다. 그러면 0700 인 ~/.ssh 를
# 읽지 못해 "ssh key not found" 로 끝난다 — 실제로 그렇게 실패했다.
# 홈 디렉토리의 **소유자**를 고른다.
declare -A DONE_HOME=() OWNER_OF=() IS_CAND=()
while IFS=: read -r u _ uid _ _ h sh; do
  [ "$uid" -ge 1000 ] 2>/dev/null || continue
  [ "$uid" -lt 65534 ] || continue
  case "$sh" in */nologin|*/false) continue;; esac
  [ -d "$h" ] || continue
  IS_CAND[$u]=1
  rh="$(readlink -f "$h" 2>/dev/null || echo "$h")"
  [ -n "${OWNER_OF[$rh]:-}" ] && continue
  # 홈 디렉토리 소유자가 곧 그 사람은 아니다. kakao 계열은 홈이 jovyan 소유인데
  # 실제로 쓰는 사람은 wjk9904 이고, ~/.ssh 와 ~/.codex 는 wjk9904 소유 0700 이다.
  # 송신기는 **기록을 읽을 수 있어야** 하므로, 도구 디렉토리의 주인을 먼저 본다.
  own=""
  for td in "$rh"/.claude* "$rh"/.codex*; do
    [ -d "$td" ] || continue
    own="$(stat -c %U "$td" 2>/dev/null)"
    [ -n "$own" ] && [ "$own" != "UNKNOWN" ] && break
    own=""
  done
  [ -n "$own" ] || own="$(stat -c %U "$rh" 2>/dev/null)"
  [ -n "$own" ] && [ "$own" != "UNKNOWN" ] && OWNER_OF[$rh]="$own"
done < /etc/passwd
# 소유자가 설치 후보가 아니면(root 소유, nologin 계정 등) 소유자 규칙을 버린다.
# 안 그러면 그 홈에는 아무도 설치되지 않는다.
for rh in "${!OWNER_OF[@]}"; do
  [ -n "${IS_CAND[${OWNER_OF[$rh]}]:-}" ] || unset 'OWNER_OF[$rh]'
done

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
  owner="${OWNER_OF[$real_home]:-}"
  if [ -n "$owner" ] && [ "$owner" != "$user" ] && id "$owner" >/dev/null 2>&1; then
    printf '%-14s %-8s %s\n' "$user" "건너뜀" "이 홈의 주인은 $owner 입니다"
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

  # 원본이 목표 안에 있으면 복사가 자기 자신을 덮는다.
  case "$(readlink -f "$SRC")/" in
    "$(readlink -f "$dest")"/*) printf '%-14s %-8s %s\n' "$user" "실패" \
        "원본이 목표 안에 있습니다 ($SRC ⊂ $dest)"; fail=$((fail+1)); continue;;
  esac

  # 파일을 갈아끼우기 **전에** 멈춘다. 돌고 있는 프로세스 밑에서 코드를 바꾸는
  # 창을 아예 없앤다(파이썬이 이미 읽어둔 모듈이라 대개 멀쩡하지만, 대개는
  # 보장이 아니다). setup.sh 가 뒤에서 다시 띄운다.
  if [ -x "$dest/stop.sh" ]; then
    run_as "$user" "$home" "cd '$dest' && ./stop.sh" >/dev/null 2>&1 || true
  fi

  # config.json 과 data/ 는 그 서버·그 사용자의 것이므로 건드리지 않는다.
  mkdir -p "$dest"
  if ! why="$(copy_tree "$SRC" "$dest")"; then
    printf '%-14s %-8s %s\n' "$user" "실패" "복사 실패 — ${why:-원인 미상}"
    case "$why" in
      *quota*|*"No space"*|*"공간"*)
        used="$(du -sh "$dest/data" 2>/dev/null | cut -f1)"
        nout="$(ls "$dest/data/outbox" 2>/dev/null | wc -l)"
        printf '%-14s %-8s %s\n' "" "" \
          "홈 용량이 찼습니다. 이 설치의 data/ 가 ${used:-?} (미전송 배치 ${nout}개)"
        printf '%-14s %-8s %s\n' "" "" \
          "정리: du -sh ~/.codex ~/.claude* $dest/data 로 큰 곳을 먼저 보세요"
        ;;
    esac
    fail=$((fail+1)); continue
  fi
  chown -R "$user" "$dest" 2>/dev/null
  chmod +x "$dest"/*.sh 2>/dev/null

  # 그 사용자의 환경으로 설치한다. setup.sh 는 이미 있는 config.json 을 건드리지
  # 않고 cron 등록과 재시작만 한다.
  # 비밀번호를 명령줄이나 env 로 넘기면 ps 에 보인다. 그 사용자만 읽을 수 있는
  # 임시 파일로 건네고 바로 지운다.
  cred=""
  # 키 경로가 config 에 적혀 있어도 **그 사용자가 읽을 수 있어야** 쓸모가 있다.
  # root 로 검사하면 통과해 버리므로 그 사용자로 확인한다.
  usable_key=""
  if [ -n "$SSH_KEY" ]; then
    if run_as "$user" "$home" "test -r '$SSH_KEY'" >/dev/null 2>&1; then
      usable_key="$SSH_KEY"
    else
      printf '%-14s %-8s %s\n' "$user" "알림" "SSH 키를 못 읽어 비밀번호로 진행 ($SSH_KEY)"
    fi
  fi
  if [ -n "$usable_key" ]; then
    cred="--key '$usable_key'"
  elif [ -z "$SSH_PASSWORD" ] && [ -n "$SSH_KEY" ] && \
       ! timeout 5 ls -d "$NAS_MOUNT" >/dev/null 2>&1 && ! ensure_password; then
    printf '%-14s %-8s %s\n' "$user" "실패" \
      "SSH 키를 못 읽고 비밀번호도 없습니다 — --password-file 로 주세요"
    fail=$((fail+1)); continue
  fi
  if [ -z "$cred" ] && [ -n "$SSH_PASSWORD" ]; then
    pwtmp="$(mktemp)"; chmod 600 "$pwtmp"; printf '%s' "$SSH_PASSWORD" > "$pwtmp"
    chown "$user" "$pwtmp" 2>/dev/null
    cred="--password \"\$(cat '$pwtmp')\""
  fi
  host_arg=""; [ -n "$NODE" ] && host_arg="--host '$NODE'"
  out="$(run_as "$user" "$home" "cd '$dest' && ./setup.sh $host_arg $cred")"
  [ -n "${pwtmp:-}" ] && { rm -f "$pwtmp"; pwtmp=""; }
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
