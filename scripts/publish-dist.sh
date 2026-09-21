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
rsync -rlt --delete --no-g --no-p --no-o \
      --exclude .git --exclude config.json --exclude data --exclude __pycache__ \
      "$ROOT/" "$DIST/" || exit 1
printf 'commit: %s\n날짜: %s\n출처: Woohye0n/ai-monitoring-send\n' \
  "$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo '?')" \
  "$(TZ=Asia/Seoul date '+%F %H:%M KST')" > "$DIST/VERSION"
echo "배포본 갱신: $DIST"
cat "$DIST/VERSION"
