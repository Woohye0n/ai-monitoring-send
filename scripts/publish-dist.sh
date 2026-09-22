#!/usr/bin/env bash
# 이 체크아웃을 NAS 배포본으로 내보낸다 (다른 서버들이 여기서 받아간다).
#
#   ./scripts/publish-dist.sh
#
# rsync --delete 를 쓰므로 VERSION 을 따로 쓰는 것을 잊으면 지워진다 — 실제로
# 한 번 지워져서 받아간 쪽이 버전을 알 수 없었다. 그래서 한 스크립트로 묶는다.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST="${DIST:-/mnt/nas/yunseok/ai-monitoring-send-dist}"
[ -d "$(dirname "$DIST")" ] || { echo "NAS 가 안 보입니다: $DIST" >&2; exit 1; }
mkdir -p "$DIST"
# --delete-excluded 가 없으면 '제외' 는 '보내지 않는다' 일 뿐, 받는 쪽에 이미
# 있는 것은 지켜진다. 배포본에서 테스트를 한 번 돌렸더니 __pycache__ 가 생겼고,
# 그 뒤로 계속 남아 각 서버로 실려 갔다(홈 쿼터가 빠듯한 노드에는 부담이다).
rsync -rlt --delete --delete-excluded --no-g --no-p --no-o \
      --exclude .git --exclude config.json --exclude data --exclude __pycache__ \
      "$ROOT/" "$DIST/" || exit 1
# NAS 에서는 SSH 계정(synologynas)이 이 파일들을 읽어 가야 한다. rsync 가 남기는
# 기본 권한은 0600 이라 그 계정이 못 읽고, scp 가 "Permission denied" 로 끝난다.
# 실제로 그렇게 실패했다 — 받는 쪽에서 원인을 알아보기 어려운 종류의 실패다.
chmod -R a+rX "$DIST" 2>/dev/null

printf 'commit: %s\n날짜: %s\n출처: Woohye0n/ai-monitoring-send\n' \
  "$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo '?')" \
  "$(TZ=Asia/Seoul date '+%F %H:%M KST')" > "$DIST/VERSION"
echo "배포본 갱신: $DIST"
cat "$DIST/VERSION"
