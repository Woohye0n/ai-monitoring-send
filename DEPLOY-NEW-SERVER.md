# 신규 서버 배포 — 처음부터 끝까지

`~/.codex` 와 `~/.claude` 만 있는 서버를 계정 분리·집계 가능 상태로 만드는 절차.
각 단계에 **실제로 출력되는 내용**을 함께 적었습니다(가짜 홈으로 시뮬레이션해 얻은
출력입니다). 그대로 나오지 않으면 그 단계에서 멈추고 원인을 보세요.

모든 스크립트는 **멱등**합니다. 중간까지 해둔 서버에서 다시 처음부터 실행해도
이미 된 것은 `= 이미 최신` 으로 넘어갑니다.

> 여기는 **한 사람이 한 서버를 계정 분리까지 끝내는** 절차입니다. 여러 서버·여러
> 사용자에게 뿌릴 때 누가 무엇을 실행하는지는 **[ROLLOUT.md](ROLLOUT.md)** 를 보세요.
> 계정을 하나만 쓴다면 Step 2·4 는 건너뛰어도 수집은 됩니다.

**순서가 중요합니다.** 앞 단계의 산출물을 뒤 단계가 씁니다.

| # | 단계 | 왜 이 순서인가 |
|---|---|---|
| 0 | 레포 받기 | — |
| 1 | `setup.sh` | `config.json` 을 만듭니다. 2단계가 그 파일의 수집 경로를 고쳐 씁니다 |
| 2 | `setup-accounts.py` | 계정 디렉토리·런처·사이드바를 만듭니다 |
| 3 | 로그인 (사람이) | 4단계의 계정 일치 검사가 목표 디렉토리의 로그인을 봅니다 |
| 4 | `migrate-history.py` | 기존 이력을 랩 디렉토리로 |
| 5 | sender 재시작 | 2단계에서 바뀐 수집 경로를 반영 |
| 6 | 동작 테스트 | 설정이 아니라 **실제 기록 위치**를 확인 |

---

## Step 0 — 레포 받기

`AIDASLab/ai-monitoring-send` 는 현재 **공개** 저장소라 인증 없이 clone 됩니다.
(비공개로 되돌리면 아래 토큰 방식이 필요합니다.)

```bash
# 처음이면
git clone https://github.com/AIDASLab/ai-monitoring-send.git ~/ai-monitoring-send
cd ~/ai-monitoring-send

# 비공개로 바뀐 뒤라면 — 토큰 방식
# git clone https://<TOKEN>@github.com/AIDASLab/ai-monitoring-send.git ~/ai-monitoring-send

# 이미 받아둔 서버면
cd ~/ai-monitoring-send && git pull
```
> 출력: `Updating <old>..<new>` 또는 `Already up to date.`

토큰을 서버에 두고 싶지 않으면 ADS-A100 에서 밀어넣으세요:
```bash
rsync -av --exclude data --exclude config.json \
      ~/Workspace/ai-monitoring-send/ <원격>:~/ai-monitoring-send/
```

---

## Step 1 — sender 설치·기동

NAS 가 **마운트 안 된** 서버 (기본 = SSH 전송):
```bash
SSH_PASSWORD='<NAS_PASSWORD>' ./setup.sh
```
NAS 가 **마운트된** 서버:
```bash
./setup.sh --local --nas /mnt/nas/yunseok/ai-monitoring
```

| 인자 | 기본값 | 설명 |
|---|---|---|
| `--host` / `HOST_ID` | `$(hostname)` | 대시보드 노드 이름. **반드시 지정**하세요 |
| `--interval` / `INTERVAL` | `300` | 수집 주기(초) |
| `--password` / `SSH_PASSWORD` | — | NAS 비밀번호 |
| `--key` / `SSH_KEY` | — | 비밀번호 대신 SSH 키 (**passphrase 없는 키만**) |
| `--local` + `--nas <경로>` | — | NAS 마운트 서버 |
| `--systemd` | — | systemd 유닛 출력 |

기대 출력의 마지막 줄:
```
[sender] batch batch-....json.gz (...) usage=... delivered=1 queued=0 via ssh:...
```
**`delivered=1 queued=0`** 이면 성공입니다.

### 전송이 실패할 때

- `Permission denied (publickey,password)` → 인증 실패. **재시도하지 않고** 확인
  순서를 출력합니다. 손으로 `ssh -p 2244 <user>@<host> 'echo ok'` 를 해보세요.
  되는데 sender 만 실패하면 `config.json` 의 `transport` 를 보세요.
- **키를 쓰는데 실패** → 그 키에 passphrase 가 걸려 있으면 `setup.sh` 가 미리
  막습니다. 손으로 ssh 할 때는 ssh-agent 가 풀어줘서 되는 것처럼 보이지만,
  sender 는 백그라운드에서 agent 없이 돌아 실패합니다. NAS 전용 키를 만드세요:
  ```bash
  ssh-keygen -t ed25519 -N '' -f ~/.ssh/id_ed25519_nas -q
  cat ~/.ssh/id_ed25519_nas.pub    # NAS 의 ~/.ssh/authorized_keys 에 추가
  rm -f config.json && ./setup.sh --key ~/.ssh/id_ed25519_nas
  ```
- `kex_exchange_identification: Connection reset by peer` → 연결 폭주. 최신
  코드는 배송 1건이 SSH 연결 1개만 쓰므로(다중화) 이 오류는 `git pull` 후 사라집니다.

---

## Step 2 — 계정 분리

```bash
python3 scripts/setup-accounts.py --dry-run    # 무엇이 바뀌는지
python3 scripts/setup-accounts.py              # 적용
```

**출력 (신규 서버 기준):**
```
[1] 설정 디렉토리
  + 생성      ~/.claude-lab1
  + 생성      ~/.codex-lab1
  + 생성      ~/.claude-lab2
  + 생성      ~/.codex-lab2

[2] PATH 실행 파일 (labN 런처 + codex/claude 래퍼)
  + 런처      ~/.local/bin/lab1
  + 래퍼      ~/.local/bin/codex-lab1
  + 래퍼      ~/.local/bin/claude-lab1
  + 런처      ~/.local/bin/lab2
  + 래퍼      ~/.local/bin/codex-lab2
  + 래퍼      ~/.local/bin/claude-lab2

[3] 원격 클라이언트 진입점 (~/.local/bin/codex)
  + 디스패처  ~/.local/bin/codex  (app-server → ~/.codex-lab1)

[4] 셸 함수 (~/.bashrc)
  + 추가      ~/.bashrc  (백업: .bashrc.bak-YYYYMMDD-HHMMSS)

[5] VSCode 사이드바 계정
  + 래퍼      ~/.vscode-server/extensions/openai.chatgpt-*/bin/*/codex → ~/.codex-lab1
  ...또는  = VSCode 설치 없음 — 건너뜀 (터미널 lab1/lab2 만 사용)

[6] sender 수집 경로
  + 수집 경로  claude=['~/.claude-lab1', '~/.claude-lab2']  codex=['~/.codex-lab1', '~/.codex-lab2']
    (이 목록 밖이어도 실제로 쓰이는 디렉토리는 sender 가 찾아서 함께 수집합니다)
```

> 이 목록은 **울타리가 아니라 힌트**입니다. sender 는 매 주기 살아 있는 프로세스의
> `CLAUDE_CONFIG_DIR`/`CODEX_HOME` 과 파일시스템도 함께 훑어 읽을 곳을 정합니다.
> 그래서 라우팅이 어긋나도 **사용량이 사라지지는 않습니다** — 다만 개인 계정으로
> 기록되어 랩 집계에는 안 들어가므로, 아래 단계는 그대로 해야 합니다.

주요 옵션: `--labs lab1`(랩 1개만) `--sidebar lab2` `--no-vscode` `--no-bashrc`
`--no-sender-config` `--claude-bin/--codex-bin`(CLI 가 PATH 밖일 때)

### 도구마다 다른 계정을 쓰는 서버

기존 `~/.codex` 가 랩2 계정이고 `~/.claude` 가 랩1 계정인 서버처럼, 도구별로 계정이
갈리는 경우가 있습니다. 그때는 사이드바를 따로 지정하세요.

```bash
python3 scripts/setup-accounts.py --sidebar-claude lab1 --sidebar-codex lab2
```
`--sidebar-codex` 는 Codex 확장 래퍼와 원격 진입점 디스패처를 함께 움직입니다.
지정하지 않은 쪽은 `--sidebar`(기본 첫 번째 랩) 값을 씁니다.

**이력 이관도 도구별로 나눠서** 실행해야 합니다 (Step 4 참고):
```bash
python3 scripts/migrate-history.py --to lab2 --only codex  --move
python3 scripts/migrate-history.py --to lab1 --only claude --move
```
계정이 어긋나면 스크립트가 알아서 그 쪽을 건너뛰므로, 랩을 잘못 지정해도 남의
계정으로 이력이 넘어가지는 않습니다.

### `lab1` 은 셸 함수가 아니라 실행 파일입니다

`~/.local/bin/lab1` 이 본체이고 `.bashrc` 함수는 부차적입니다. 함수는 rc 파일을
읽은 셸에만 있어서 **설정 전에 열어둔 탭이나 tmux pane 에서는 안 잡힙니다.**
실행 파일이라 `source` 없이 어디서든 동작합니다.

`⚠ ~/.local/bin 가 PATH 에 없습니다` 경고가 뜨면 이름으로 호출할 수 없습니다:
```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc   # 후 새 창
~/.local/bin/lab1 codex resume                             # 급하면 전체 경로
```

### 그 밖에 나올 수 있는 경고

- `⚠ ~/.bashrc 에 이미 claude() 오버라이드가 있습니다` → 로그인은 **전체 경로**로
  하세요. 그 함수가 `CLAUDE_CONFIG_DIR` 를 덮어씁니다.
- `= PATH 에 codex 없음 — 건너뜀` → codex 가 `~/.local/bin` 밖에 설치됨. 데스크탑
  원격 경로가 안 잡히니 설치 위치를 확인하세요.

---

## Step 3 — 로그인 (사람이 직접)

대화형이라 자동화할 수 없습니다. 스크립트가 출력한 명령을 그대로 쓰세요.
```bash
lab1 codex login
lab1 claude auth login
lab2 codex login
lab2 claude auth login
```
확인:
```bash
lab1 codex login status        # → Logged in using ChatGPT
lab1 claude auth status        # → 계정 이메일
```
**랩 계정 주소가 맞는지 꼭 확인하세요.** 여기서 틀리면 이후 전부 틀어집니다.

---

## Step 4 — 기존 이력을 랩 디렉토리로

```bash
python3 scripts/migrate-history.py --to lab1 --dry-run
python3 scripts/migrate-history.py --to lab1 --move
```

`--to lab2` 로 랩2 계정에 넣을 수도 있습니다. `--move` 는 원본을 비우고,
빼면 복사(원본 유지)입니다.

**옮기는 것**: codex `sessions/`·`archived_sessions/`·`session_index.jsonl`·`history.jsonl`,
claude `projects/`·`history.jsonl`·`todos/`·`tasks/`.
`--extras` 로 `config.toml`·`skills/`·`plugins/`·`settings.json`·`CLAUDE.md` 도 포함.
**절대 안 옮김**: `auth.json`, `.credentials.json`, `packages/`, 캐시류.

> ⚠️ **원본과 목표의 로그인 계정이 다르면 그 쪽은 건너뜁니다.** 개인 계정 이력을
> 랩으로 옮기면 그 사용량이 랩 계정 사람에게 귀속되기 때문입니다. codex/claude 를
> 각각 판정하므로 한쪽만 랩 계정인 서버에서는 그쪽만 옮겨집니다. 한쪽만 하려면
> `--only codex`. 정말 강행해야 하면 `--force`.

`--move` 는 같은 파일시스템이면 rename 이라 추가 용량이 안 듭니다. 복사는 용량이
두 배 들고, 여유가 부족하면 스크립트가 미리 막습니다.

옮겨도 **과거 사용량은 집계되지 않습니다** — 수집기는 처음 읽는 기록을 계정
미확정으로 버리고, 대시보드는 `tracking.count_from_ms` 이후만 셉니다. 목적은
`resume` 로 이어 쓰는 것입니다.

### 왜 `--move` 를 권하는가

복사만 하면 같은 이력이 양쪽에 남습니다. 원본(`~/.codex`)이 **랩 계정으로 로그인된
상태**면, 거기서 맨 `codex` 를 쓴 작업은 랩 사용량인데 수집 경로 밖이라 **집계에서
빠집니다.** `--move` 로 비우면 실수할 여지가 없어집니다. 원본을 개인 계정으로
재로그인해 두는 것도 방법입니다:
```bash
CODEX_HOME=~/.codex codex logout && CODEX_HOME=~/.codex codex login
```

---

## Step 5 — sender 재시작

```bash
./stop.sh && ./start.sh && ./status.sh
```
> `[sender] batch ... accounts=[...]` 에 랩 계정이 보이면 성공입니다.

---

## Step 6 — 실제 동작 테스트

설정이 맞아 보여도 실제 기록 위치는 프로세스가 뜰 때의 환경이 정합니다. 눈으로
확인하는 것이 유일하게 확실합니다.

**창 A**
```bash
python3 scripts/where-landed.py --watch
```
**창 B**
```bash
lab1 codex resume        # thread 선택 → 프롬프트 하나 전송
```

창 A 의 기대 출력:
```
→ .codex-lab1 에 쌓였습니다  [thread 이름]
   rollout-....jsonl
   계정 aidaslab.snu@gmail.com
   수집 대상입니다 ✅  이관·라우팅 정상
```
`⚠ 수집 대상이 아닙니다` 는 `exclude_dirs` 로 뺐거나 `discover: false` 인 경우에만
뜹니다. 그 외에는 어디에 쌓이든 수집은 됩니다 — 확인할 것은 **계정 줄**입니다.
개인 계정으로 찍혀 있으면 랩 집계에 안 들어가므로 `lab1 codex` 로 다시 여세요.

현황만 보려면:
```bash
python3 scripts/where-landed.py            # 프로세스 커버리지 + 디렉토리별 계정·기록수
python3 scripts/where-landed.py --since 10 # 최근 10분에 쓰인 것만
```
첫 절에 **지금 돌고 있는 claude/codex 프로세스가 어디에 쓰는지**가 나옵니다.
`[누락]` 이 있으면 그 pid 의 기록은 아무도 안 읽고 있다는 뜻입니다.

### VSCode 사이드바도 쓰는 서버라면

```bash
python3 scripts/sidebar-account.py --show      # 현재 계정
python3 scripts/sidebar-account.py --lab lab2  # 바꾸기
python3 scripts/sidebar-account.py --personal  # 개인 계정으로
```
**VSCode 창을 리로드**해야 적용됩니다. 그 뒤 사이드바에서 프롬프트를 하나 보내고
`where-landed.py --since 2` 로 lab 디렉토리에 쌓였는지 확인하세요.

Codex 확장에는 계정 설정이 없어서(검증함) 확장 번들 바이너리를 래핑하는 방식을
씁니다. **확장을 업데이트하면 조용히 풀립니다** — `setup-accounts.py` 재실행으로
복구되고, ADS-A100 의 `check-routing.py` 가 그 상태를 탐지합니다.

---

## Step 7 — 중앙에서 확인

중앙(ADS-A100)에서:
```bash
python3 ~/Workspace/aidas-ai-monitoring/scripts/check-routing.py
python3 ~/Workspace/aidas-ai-monitoring/scripts/people-report.py --nodes
```
> `check-routing.py` 는 **로컬 홈만** 검사하므로 원격 서버 검증은 그 서버에서
> `where-landed.py` 로 하세요.
>
> 원격 서버 데이터가 대시보드에 뜨려면 중앙 `config.json` 의 `nas.enabled` 가
> `true` 여야 합니다. 꺼져 있으면 NAS inbox 를 아예 읽지 않습니다.

---

# 이미 열려 있는 세션 처리

`CODEX_HOME`/`CLAUDE_CONFIG_DIR` 는 **기동 시점에만** 읽힙니다. 떠 있는 프로세스는
옛 계정으로 계속 기록하므로 전부 재시작해야 합니다.

| 대상 | 처리 |
|---|---|
| 터미널 codex TUI | 종료 후 `lab1 codex resume` |
| 터미널 claude CLI | 종료 후 `lab1 claude` |
| VSCode 사이드바 | **창 리로드** |
| 데스크탑 앱 SSH 원격 | 연결 끊고 **재연결** |
| cron·스크립트 | 호출을 `codex` → `codex-lab1` 로 |

재시작 대상은 ADS-A100 의 `check-routing.py` 출력에서 `[누락]` 로 표시된 pid 들입니다
(원격 서버에서는 그 서버에서 실행).

같은 thread 를 두 디렉토리에서 열면 **대화가 갈라집니다.** rollout 은 append-only
로그라 안전하게 병합할 수 없습니다. **thread 하나는 디렉토리 하나가 소유**하게
하고, 계정을 옮길 일이 있으면 `migrate-history.py` 로 한 번 옮긴 뒤 옛 사본은
쓰지 마세요.

---

# 설정이 조용히 풀리는 세 가지

정기적으로 확인하세요. 전부 `setup-accounts.py` 재실행으로 복구됩니다.

1. **VSCode Codex 확장 업데이트** → 래핑한 번들 바이너리가 덮어써짐
2. **codex 자체 업데이트** → `~/.local/bin/codex` 디스패처가 심볼릭 링크로 복구됨
3. **환경변수 없이 뜬 새 프로세스** → 개인 경로로 기록

> **더 이상 "조용히" 는 아닙니다.** 2026-09-21 부터 sender 가 개인 경로도 읽고,
> 배치의 `diagnostics` 와 `status.sh` 가 어디에 쓰이는지·어느 표면인지 적어 보냅니다.
> 랩 계정으로 안 찍히는 것은 그대로이니 복구는 여전히 필요하지만, **발견이
> 사람의 기억에 달려 있지 않습니다.**

바꾸는 파일은 모두 `.bak-<타임스탬프>` 로 백업되고, `~/.local/bin/codex` 는
심볼릭 링크 원본을 `codex.symlink-backup` 으로 남깁니다.
