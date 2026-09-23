#!/usr/bin/env bash
# 송신기를 멈춘다. pid 파일에 적힌 것뿐 아니라, 같은 사용자로 돌고 있는
# 다른 송신기도 함께 정리한다.
#
# 왜: 예전에는 data/sender.pid 만 봤다. 그런데 설치 경로가 바뀌거나 pid 파일이
# 지워지면 옛 프로세스가 추적에서 벗어나 영원히 살아남는다 — 실제로 gpu-2-0
# 에서 설치본은 하나인데 송신기가 4개 돌고 있었고, 그중 하나가 없어진 옛
# 이름('gpu-2-0')으로 계속 보고해 대시보드에 한 노드가 둘로 보였다.
# 한 사용자에게 송신기는 하나여야 한다.
set -uo pipefail
cd "$(dirname "$0")"

ME="$(id -un)"

# argv 를 정확히 본다. pgrep -f 는 커맨드라인 '문자열'을 훑기 때문에, 하필
# 'sender.main' 이라는 말이 들어간 셸 명령까지 잡는다 — 실제로 그렇게 엉뚱한
# 셸을 죽였다. argv[0] 이 python 이고 argv[1..2] 가 '-m sender.main' 인
# 프로세스만 송신기로 친다(start.sh 가 띄우는 모양 그대로).
is_sender() {
  local p="$1" a0 a1 a2
  { IFS= read -r -d '' a0 && IFS= read -r -d '' a1 && IFS= read -r -d '' a2; } \
    < "/proc/$p/cmdline" 2>/dev/null || return 1
  case "${a0##*/}" in python*) ;; *) return 1;; esac
  [ "$a1" = "-m" ] && [ "$a2" = "sender.main" ]
}

TRACKED=""
if [ -f data/sender.pid ]; then
  TRACKED="$(cat data/sender.pid 2>/dev/null || true)"
  if [ -n "$TRACKED" ] && kill "$TRACKED" 2>/dev/null; then
    echo "stopped pid $TRACKED"
  else
    echo "not running (pid $TRACKED)"
  fi
  rm -f data/sender.pid
else
  echo "no pid file (data/sender.pid)"
fi

# 내 것이면서 추적되지 않던 송신기. -u 로 범위를 내 계정에 묶는다 — 공용
# 서버에서 남의 송신기를 죽이면 안 된다.
STRAY=""
for p in $(pgrep -u "$ME" -f 'sender\.main' 2>/dev/null); do
  [ "$p" = "$TRACKED" ] && continue
  is_sender "$p" || continue
  STRAY="$STRAY $p"
done
if [ -n "$STRAY" ]; then
  echo "정리: 추적되지 않던 송신기$STRAY"
  # shellcheck disable=SC2086
  kill $STRAY 2>/dev/null || true
  sleep 1
  for p in $STRAY; do
    kill -0 "$p" 2>/dev/null && { echo "  pid $p 가 안 죽어 -9 로 보냅니다"; kill -9 "$p" 2>/dev/null || true; }
  done
fi
exit 0
