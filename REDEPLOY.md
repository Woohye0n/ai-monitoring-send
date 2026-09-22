# ai-monitoring-send 재배포 가이드

원격 GPU 서버에서 sender 코드를 갱신하고 재시작하는 절차.

> 여러 서버·여러 사용자에게 **처음 배포**하는 절차와 사용자 공지문은
> **[ROLLOUT.md](ROLLOUT.md)** 를 보세요.

> ⚠️ **중앙 서버(ADS-A100)에서는 sender를 실행하지 마세요.** ADS-A100은
> `backend.server`가 로컬(~/.claude2, ~/.codex) + NAS를 직접 수집하는 중앙입니다.
> 여기 `~/Workspace/ai-monitoring-send`는 **편집·배포용 원본 사본**이라 `config.json`이
> 없고(gitignore), `./start.sh`를 돌리면 기본 ssh 트랜스포트가 비밀번호를 요구하며
> `ValueError: transport.mode=ssh needs ssh_password or ssh_key` 로 즉시 죽습니다.
> 실수로 띄웠다면 `./stop.sh` 로 정리하세요. 아래 절차는 **원격 서버 전용**입니다.

## 신규 서버 세팅: 계정 분리 자동화 (`scripts/`)

> 처음부터 끝까지의 단계별 절차와 각 단계의 **예상 출력**, 이미 열려 있는
> 세션 처리까지는 **[DEPLOY-NEW-SERVER.md](DEPLOY-NEW-SERVER.md)** 를 보세요.
> 아래는 각 스크립트가 무엇을 하는지에 대한 설명입니다.

사람별·계정별로 사용량이 집계되려면 계정마다 설정 디렉토리가 분리돼 있어야 합니다.
ADS-A100에서 손으로 했던 작업 전부를 두 프로그램이 대신합니다.

### `scripts/setup-accounts.py` — 디렉토리 리팩토링 + 환경변수/alias + 사이드바

```bash
cd ~/ai-monitoring-send
python3 scripts/setup-accounts.py --dry-run    # 무엇이 바뀌는지 먼저 확인
python3 scripts/setup-accounts.py              # 적용
source ~/.bashrc
```

한 번에 처리하는 것:

| # | 작업 | 결과물 |
|---|---|---|
| 1 | 계정별 설정 디렉토리 생성 (0700) | `~/.claude-lab1`, `~/.codex-lab1`, `~/.claude-lab2`, `~/.codex-lab2` |
| 2 | PATH 실행 파일 | `~/.local/bin/lab1`·`lab2` 런처, `codex-lab1`·`claude-lab1` 래퍼 |
| 3 | 원격 클라이언트 진입점 | `~/.local/bin/codex` 디스패처 (`app-server`만 랩으로) |
| 4 | 셸 함수(alias) 자동 등록 | `~/.bashrc` 의 `lab1 <cmd>` / `lab2 <cmd>` |
| 5 | VSCode 사이드바 계정 지정 | 확장 번들 바이너리 래핑 + `settings.json` |
| 6 | sender 수집 경로 **추가** | `config.json` 의 `claude.config_dirs`, `codex.dirs` (덮어쓰지 않음) |

**멱등**합니다 — 두 번째 실행부터는 전부 `= 이미 최신`, 변경 0건. 바꾸는 파일은 모두
`.bak-<타임스탬프>` 로 백업합니다. 마커 없이 손으로 넣어둔 예전 `lab1()/lab2()` 정의가
있으면 마커 블록으로 통합해 중복을 없앱니다.

주요 옵션: `--labs lab1,lab2` `--sidebar lab1` `--no-vscode` `--no-bashrc`
`--no-sender-config` `--claude-bin/--codex-bin`(PATH에 없을 때)

마지막에 **대화형 로그인 명령**을 출력합니다. 로그인만 사람이 직접 하면 됩니다:
```bash
CODEX_HOME=~/.codex-lab1 codex login
CLAUDE_CONFIG_DIR=~/.claude-lab1 claude auth login
```
> `~/.bashrc` 에 `claude()`/`codex()` 오버라이드가 이미 있으면 스크립트가 경고합니다.
> 그 경우 로그인은 **전체 경로**(`~/.local/bin/claude`)로 실행하세요. 함수가
> 환경변수를 덮어써 엉뚱한 디렉토리에 로그인됩니다.

### `scripts/sidebar-account.py` — 사이드바 계정만 바꾸기

터미널(`lab1`/`lab2`)과 무관하게, VSCode 사이드바에 뜨는 계정만 전환합니다.
```bash
python3 scripts/sidebar-account.py --show        # 현재 계정
python3 scripts/sidebar-account.py --lab lab2    # 랩2로
python3 scripts/sidebar-account.py --personal    # 개인 계정(~/.codex, ~/.claude)로 복귀
```
**VSCode 창 리로드**하면 적용됩니다(원격 서버 재시작 불필요).

두 확장의 사정이 다릅니다. Claude 는 `claudeCode.environmentVariables` 라는 정식
설정이 있지만, **Codex 확장에는 계정 설정이 없습니다.** `chatgpt.cliExecutable` 은
app-server 실행에 쓰이지 않고(검증함), 확장은 자기 번들 바이너리
`~/.vscode-server/extensions/openai.chatgpt-*/bin/*/codex` 를 직접 실행하며 계정은
상속받은 `CODEX_HOME`(없으면 `~/.codex`)으로만 정해집니다. 확장 호스트에 그 변수를
넣을 방법도 없어(이 서버 빌드는 `server-env-setup` 미지원), **번들 바이너리 자리에
래퍼를 두고 원본을 `codex.real` 로 옮깁니다.**

> ⚠️ Codex 확장을 업데이트하면 래퍼가 덮어써져 **조용히 개인 계정으로 돌아갑니다.**
> 중앙의 `aidas-ai-monitoring/scripts/check-routing.py` 가 그 상태를 탐지하므로
> 주기적으로 돌리세요. 복구는 `sidebar-account.py --lab <lab>` 재실행.

### 확장/터미널/자동화가 각각 어디에 기록되는지

| 실행 주체 | 보는 값 | 기록 위치 |
|---|---|---|
| VSCode Claude 확장 | `claudeCode.environmentVariables` | 지정한 `~/.claude-labN` |
| VSCode Codex 확장 | 확장 번들 바이너리를 래핑한 `CODEX_HOME` | 지정한 `~/.codex-labN` |
| 터미널 `lab1 codex` | `~/.local/bin/lab1` 런처 (셸 함수도 있음) | `~/.codex-lab1` |
| 터미널 맨 `codex` | 미설정 | `~/.codex` (개인) |
| 데스크탑 앱 (SSH 원격) | `~/.local/bin/codex` 디스패처 | 지정한 `~/.codex-labN` |
| **스크립트·Claude Code 등 자동화** | 미설정 | **`~/.codex` (개인) ⚠** |

마지막 줄이 함정입니다. `CODEX_HOME` 을 상속받지 못하는 자동화(cron, 다른 Claude Code
세션이 Bash로 띄우는 `codex` 등)는 랩 계정으로 잡히지 않습니다. **`codex` 대신
`~/.local/bin/codex-lab1` 을 호출**하게 하세요. 이미 떠 있는 `codex resume` TUI도
전환 이전에 시작됐다면 옛 디렉토리에 계속 씁니다 — `lab1 codex resume` 으로 다시 여세요.

> **2026-09-21 이후**: 위 표에서 `~/.codex`(개인)로 떨어지는 경우도 sender 는 이제
> **읽어서 보냅니다.** 없어지는 것은 "수집 자체가 안 됨" 이고, 남는 것은 "개인 계정
> 으로 기록됨" 입니다. 개인 계정은 중앙 `tracking.allowed_accounts` 가 집계에서
> 빼므로 **여전히 랩 사용량으로는 안 잡힙니다.** 즉 라우팅은 계속 맞춰야 하지만,
> 이제 어긋나면 화면이 침묵하는 대신 `diagnostics` 와 `status.sh` 가 알려줍니다.

데스크탑 ChatGPT 앱이 SSH 로 붙는 경우는 별도 함정입니다. sshd 가
`PATH="$HOME/.local/bin:$PATH"; codex app-server proxy` 를 실행하는데 그 셸에는
`CODEX_HOME` 을 넣을 방법이 없습니다(`PermitUserEnvironment` 는 보통 꺼져 있고
`~/.bashrc` 는 비대화형에서 즉시 return). 그래서 `setup-accounts.py` 가
`~/.local/bin/codex` 를 **디스패처 스크립트**로 바꿔, IDE·원격 클라이언트 전용
진입점인 `app-server` 서브커맨드일 때만 랩 계정으로 고정합니다. 터미널 대화형
(`codex`, `codex resume`)은 그대로 개인 계정입니다.

데스크탑 앱은 `CODEX_HOME` 을 비워 보내지 않고 **개인 기본값(`~/.codex`)을 명시적으로**
넘깁니다(`CODEX_REMOTE_PAYLOAD` 와 함께). 그래서 "미설정일 때만" 규칙으로는 안 잡히고,
개인 기본값과 같을 때도 갈아끼웁니다. `lab1`/`lab2` 처럼 의도적으로 지정한 값은 존중합니다.

> 디스패처를 바꾼 뒤에는 **데스크탑 앱의 원격 연결을 한 번 끊었다 다시 붙여야** 합니다.
> 이미 떠 있는 `app-server proxy` 는 연결 시점의 환경을 그대로 유지합니다.

> ⚠️ codex 가 자기 자신을 업데이트하면 이 파일이 원래 심볼릭 링크로 되돌아가
> 조용히 새기 시작합니다. `check-routing.py` 가 탐지하고, `setup-accounts.py`
> 재실행으로 복구됩니다.

## 이번 변경 (2026-09-21) — 수집 범위와 표면

**증상.** 사람들이 분명히 쓰는데 대시보드에 안 잡히고, 잡혀도 누가 어떻게 썼는지
구분이 안 됐습니다. NAS inbox 를 까 보니 원인이 셋이었습니다.

| # | 무엇이 | 실제 증거 |
|---|---|---|
| 1 | **홈 밖 설정 디렉토리를 못 찾음** | 한 사용자가 `CLAUDE_CONFIG_DIR=/mnt/data/<user>/.claude-<user>`, `CODEX_HOME=/mnt/nvme1/<user>/.codex-<user>` 로 매일 두 도구를 쓰는데, 그 노드의 최신 배치는 `usage=0 sessions=0` |
| 2 | **`setup-accounts.py` 가 수집 경로를 랩 디렉토리로 덮어씀** | VSCode 사이드바가 쓰는 `~/.codex` 가 수집 밖. 한 서버에는 `originator=codex_vscode` 236건 + `Codex Desktop` 64건이 쌓여 있는데 sender 가 아예 없음 |
| 3 | **표면 정보를 버림** | Claude 는 JSONL 줄마다 `entrypoint`, Codex 는 `session_meta` 에 `originator`/`source` 를 적는데 수집기가 읽지 않음 |
| 4 | **codex 세션 행이 `cwd` 를 잃음** | 안 바뀐 롤아웃은 메타를 다시 안 읽어 `cwd=None` → 중앙이 cwd 로 사람을 붙이므로 **미지정 세션 81개 중 79개**가 이것 |
| 5 | **같은 이력이 두 디렉토리에** | 넓게 읽게 되면서 같은 턴이 두 번 전송(토큰 2배). uuid 로 중복 제거 + 경고 |

덤으로, 한 장비에 사용자별 sender 가 여러 개 설치돼 `ADS-A100`·`aidas-a100`·
`hgkim_AIDAS_A100` 이 서로 다른 노드로 보이고 있었습니다(셋 다 `fqdn=aidas`, 같은 IP).

**바꾼 것.**

| 파일 | 내용 |
|---|---|
| `sender/discovery.py` (신규) | 살아 있는 프로세스(`/proc/<pid>/environ`)·파일시스템·설정을 **합쳐** 읽을 디렉토리 결정. 표면(terminal/vscode/desktop) 분류 |
| `sender/main.py` | 매 주기 재탐색 + 수집기 레지스트리(인스턴스는 유지 — 안 그러면 전부 `assumed`), `machine_id`/`home`, 배치에 `diagnostics`, **디렉토리 간 중복 제거**(같은 턴/계정/세션) |
| `sender/claude_collector.py` | 레코드에 `surface`/`entrypoint`, 처음 보는 디렉토리의 과거 이력 스킵, usage 캐시 공유 |
| `sender/codex_collector.py` | 레코드·세션에 `surface`, 같은 가드, **세션 행의 `cwd` 유실 수정**(담당자 미지정의 주원인) |
| `scripts/setup-accounts.py` | 수집 경로를 **덮어쓰지 않고 더함** |
| `scripts/where-landed.py` | 같은 탐색을 사용 + 프로세스 커버리지 출력 |
| `status.sh` | 커버리지·경고를 함께 출력 |
| `tests/test_discovery.py` (신규) | `python3 tests/test_discovery.py` |

**설정 변경은 필요 없습니다.** 새 키(`discover`/`extra_roots`/`exclude_dirs`/
`bootstrap_days`)는 전부 기본값이 있고 기존 `config.json` 을 그대로 씁니다.
`setup-accounts.py` 가 예전에 좁혀 놓은 `config_dirs` 도 이제 울타리가 아닙니다.

**첫 배치가 커지지 않습니다.** 새로 보이게 된 디렉토리의 과거 이력은
`bootstrap_days`(기본 2일) 가드로 건너뜁니다 — 어차피 계정을 증명할 수 없어
중앙에서 버려지는 기록입니다. 실측으로 21만 턴(111MB) → 0.4MB.

### 재배포 후 확인

```bash
cd ~/ai-monitoring-send && git pull && ./stop.sh && ./start.sh && ./status.sh
```

`status.sh` 의 새 `coverage` 절에서:

- 살아 있는 claude/codex 프로세스가 전부 `[수집]` 인지 (`[누락]` 이 있는데
  `exclude_dirs` 로 뺀 게 아니면 버그입니다 — 그대로 알려주세요)
- 로그의 `surfaces[...]` 에 그 사람이 실제로 쓰는 표면이 보이는지
  (사이드바만 쓰는 사람인데 `terminal` 만 나오면 아직 뭔가 새고 있는 것)

```bash
python3 tests/test_discovery.py      # 서버에서 그대로 돌려도 됩니다
```

---

## 이전 변경 (2026-07-21) — 한도 파싱

수정된 파일은 **3개뿐**:

| 파일 | 내용 |
|---|---|
| `sender/claude_usage.py` | usage API 401/403(정지)·429(스로틀) 구분, 신 스키마 `limits[]`에서 Fable 주간 한도 파싱 |
| `sender/claude_collector.py` | 정지/인증오류 상태(`usage_status`)를 배치에 포함, stale 재도장 방지 |
| `sender/codex_collector.py` | **rate_limits를 윈도우별로 병합**(5h가 사라지던 버그 수정), `resets_in_seconds` 지원, 비-UTF8 rollout 방어, 계정 전환 시 캐시 격리 |

특히 **codex 5시간 한도가 대시보드에 안 뜨던 문제**는 `codex_collector.py`의 병합 버그가
원인입니다(최신 이벤트가 weekly만 담고 있으면 5h를 통째로 덮어써 잃어버림). 이 파일이
핵심입니다.

## 활성 서버: 코드 갱신 + 재시작

각 원격 서버에 접속해 sender 디렉토리(예: `~/ai-monitoring-send`)에서 실행합니다.

### 방법 A — git 사용 시
```bash
cd ~/ai-monitoring-send
git pull
./stop.sh && ./start.sh
./status.sh          # 프로세스 + 최근 로그 + NAS 최신 배치 확인
```

### 방법 B — git 미사용 시 (ADS-A100에서 파일 동기화)
2026-09-21 변경은 파일 몇 개만 고른 동기화로는 안 됩니다(`sender/discovery.py` 가
새로 생기고 `scripts/`·`status.sh` 도 함께 바뀝니다). 디렉토리째 밀어넣으세요 —
`config.json` 과 `data/` 는 서버마다 다르므로 반드시 제외합니다.

```bash
# ADS-A100에서 실행 (원격에 SSH 접근이 되는 경우)
SRC=~/Workspace/ai-monitoring-send
for host in <원격1> <원격2> ...; do
  rsync -av --exclude config.json --exclude data --exclude '.git' \
        "$SRC/" "$host:~/ai-monitoring-send/"
done
```
그 후 각 원격에서:
```bash
cd ~/ai-monitoring-send && ./stop.sh && ./start.sh && ./status.sh
```

원격→ADS-A100 방향만 되는 경우(원격에서 pull):
```bash
# 각 원격 서버에서
cd ~ && rsync -av --exclude config.json --exclude data --exclude '.git' \
    <ads-a100>:~/Workspace/ai-monitoring-send/ ai-monitoring-send/
cd ai-monitoring-send && ./stop.sh && ./start.sh && ./status.sh
```

### systemd로 돌리는 경우
```bash
sudo systemctl restart ai-monitoring-send   # 유닛명은 setup.sh --systemd 출력 참고
journalctl -u ai-monitoring-send -n 30 --no-pager
```

## 재시작 확인 포인트

`./status.sh` 또는 `tail -f data/sender.log` 에서:
- `[sender] batch ... accounts=[...]` 에 해당 계정이 보이는지
- `dirs=N surfaces[...]` 가 그 사람의 실제 사용 방식과 맞는지 (2026-09-21 변경)
- `coverage` 절의 프로세스가 전부 `[수집]` 인지 (2026-09-21 변경)
- codex 계정이면 몇 분 뒤 대시보드 카드에 **5시간 + 주간 게이지가 둘 다** 뜨는지
  (병합 수정이 반영됐다는 신호)

## aidaslab2 codex를 쓰는 서버 (`servername`)

이 서버가 aidaslab2 codex의 유일한 활성 보고자입니다. 위 방법으로 `codex_collector.py`를
갱신·재시작하면, 다음 rollout 이벤트부터 5시간 한도가 정상 집계됩니다. codex는 별도의
usage API가 없으므로 값은 rollout 파일의 `rate_limits`(= 실제 API 응답 헤더에서 CLI가 기록)
에서 옵니다 — 즉 "실제 API 호출 기반"이며, 재배포로 그 값을 온전히 읽게 됩니다.

## 만료 서버 (RND1 / RND2 / AIIS) — 재배포 대신 정지

이 3대는 중앙에서 `nas.ignore_hosts`로 무시·정리 중입니다. 각 서버의 sender는 **정지**만
하면 됩니다(계속 배치를 떨궈도 중앙이 버리지만, 불필요한 부하·NAS 쓰기를 없애려면):
```bash
cd ~/ai-monitoring-send && ./stop.sh
# systemd면: sudo systemctl disable --now ai-monitoring-send
```

## 바깥 HTTPS 가 막힌 서버 (카카오 b200 등)

`curl … github.io` 가 `(35) Connection reset by peer` 로 끊기는 클러스터가 있다.
NAS 는 열려 있고 자격증명도 이미 `config.json` 에 있으니 그쪽으로 받는다.

**최초 1회** — 아직 `update-from-nas.sh` 가 없는 노드. 마지막 줄의 `-- --host …`
는 이름을 바꿀 때만 붙인다.

```bash
sudo bash -s <<'SH'
set -uo pipefail
D=/tmp/aidas-dist
rm -rf "$D"                      # 목적지가 있으면 scp -r 이 안에 중첩시킨다
M=/mnt/nas/yunseok/ai-monitoring-send-dist
if timeout 5 ls -d "$M" >/dev/null 2>&1; then
  cp -a "$M" "$D"
else
  D="$D" python3 - <<'PY' || exit 1
import glob, json, os, sys
src = ""
for d in sorted(glob.glob("/home/*/ai-monitoring-send")) + ["/root/ai-monitoring-send"]:
    try:
        t = json.load(open(d + "/config.json"))["transport"]
    except Exception:
        continue
    if t.get("ssh_host") and (t.get("ssh_password") or t.get("ssh_key")):
        src = d
        break
if not src:
    sys.exit("자격증명이 든 기존 설치를 못 찾았습니다")
sys.path.insert(0, src)
from sender import sshcmd
t = json.load(open(src + "/config.json"))["transport"]
sshcmd.scp_get(t, os.path.dirname(t["remote_root"]) + "/ai-monitoring-send-dist",
               os.environ["D"], recursive=True)
print("자격증명 출처:", src)
PY
fi
B="$(find "$D" -maxdepth 3 -name bootstrap.sh -path '*/scripts/*' | head -1)"
[ -n "$B" ] || { echo "배포본이 이상합니다 ($D)"; ls -R "$D" | head; exit 1; }
echo "버전: $(cat "$(dirname "$(dirname "$B")")/VERSION" 2>/dev/null | head -1)"
exec bash "$B" "$@"
SH
```

**그 다음부터** — 위를 한 번 돌린 노드에는 스크립트가 깔려 있다:

```bash
sudo bash /home/<사용자>/ai-monitoring-send/scripts/update-from-nas.sh
```
