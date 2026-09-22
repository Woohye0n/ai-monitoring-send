# ai-monitoring-send (송신 에이전트)

다른 서버에서 **Claude Code + Codex** 사용량을 수집해 **Synology NAS로 SSH 전송**하는
경량 에이전트. 서버 간 직접 통신이 안 되고 **NAS도 마운트되어 있지 않은** GPU 서버에서,
`scp`로 NAS에 파일을 떨구면 중앙 대시보드([aidas-ai-monitoring](../aidas-ai-monitoring))가
NAS 마운트를 폴링해 읽어갑니다.

- **Python 3 표준 라이브러리만** 사용 (sshpass/paramiko 등 설치 불필요)
- `setup.sh` **하나만 실행**하면 설정 생성 + 백그라운드 시작
- **SSH 비밀번호 자동 입력**(stdlib `pty` 사용) — 또는 SSH 키 사용 가능
- Claude + Codex 를, **터미널이든 VSCode 사이드바든 데스크탑 앱이든** 모두 수집
- 설정 디렉토리를 **살아 있는 프로세스에서 찾아냅니다** — 홈 밖이어도, 설정에
  안 적혀 있어도 (아래 "어디를 읽는가")

```
GPU 서버 A ─┐ scp (pw 자동)
GPU 서버 B ─┼─▶ NAS: /volume1/nas-nfs/yunseok/ai-monitoring/inbox/<host>/batch-*.json.gz
GPU 서버 C ─┘                          ▲ (= 중앙의 /mnt/nas/yunseok/... 마운트)
                                       └ 중앙 대시보드가 30초마다 폴링해서 ingest
```

---

## 빠른 시작 (각 송신 서버에서)

```bash
git clone <this-repo> ai-monitoring-send
cd ai-monitoring-send
SSH_PASSWORD='<NAS_PASSWORD>' ./setup.sh        # 끝. 비번 자동입력으로 NAS에 전송 시작
```

> **서버의 모든 사용자에게 한 번에** 켜려면 `sudo ./scripts/bootstrap.sh` 한 줄이면
> 됩니다. 노드 이름과 NAS 자격증명은 기존 설치에서 물려받고, 알아낼 수 없을 때만
> 그때 물어봅니다. 자세한 건 [ROLLOUT.md](ROLLOUT.md).


> `-bash: ./setup.sh: Permission denied` 가 뜨면 클론/복사 과정에서 실행 비트가
> 떨어진 것입니다. 다음 중 하나로 해결하세요 (setup.sh가 이후 나머지 스크립트
> 권한은 자동 복구합니다):
> ```bash
> bash setup.sh                        # 실행 비트 없이 바로 실행, 또는
> chmod +x *.sh && ./setup.sh
> ```

`SSH_PASSWORD`를 안 주면 한 번 물어봅니다(이후 `config.json`에 저장, chmod 600).
기본 SSH 대상은 `synologynas@aidaslab.synology.me:2244`,
원격 경로는 `/volume1/nas-nfs/yunseok/ai-monitoring` 입니다.

자주 쓰는 옵션:

```bash
./setup.sh --host gpu7 --password '<NAS_PASSWORD>'        # 호스트(노드) 이름 지정
./setup.sh --host gpu7 --claude-dir /data/work/.claude   # .claude 폴더 직접 지정
./setup.sh --host gpu7 --claude-dir ~/.claude --claude-dir ~/.claude2  # 여러 개(반복)
./setup.sh --key ~/.ssh/id_ed25519                  # 비번 대신 SSH 키 사용(권장)
./setup.sh --ssh-host 10.8.0.1 --ssh-port 22        # SSH 대상 변경
./setup.sh --local --nas /mnt/nas/yunseok/ai-monitoring  # NAS가 마운트된 서버
./setup.sh --systemd                                # systemd 유닛 출력
```

> `--claude-dir` / `--codex-dir` 는 **"여기도 꼭 보라"는 추가 지정**입니다(반복 가능).
> 예전처럼 지정한 폴더만 보는 게 아니라, 자동으로 찾은 곳과 **합쳐서** 읽습니다.
> 보통은 안 줘도 됩니다 — 홈 밖 디렉토리도 살아 있는 프로세스에서 찾아냅니다.
> 지금 꺼져 있고 앞으로도 안 뜰 디렉토리만 지정하면 됩니다.

관리: `./status.sh` (상태/최근배치/로그) · `./stop.sh` · `./start.sh`

> **비밀번호보다 SSH 키를 권장**합니다. 한 번만:
> `ssh-keygen -t ed25519 && ssh-copy-id -p 2244 synologynas@aidaslab.synology.me`
> 후 `./setup.sh --key ~/.ssh/id_ed25519` 로 비번 없이 동작합니다.

---

## 동작 방식

매 `interval_seconds`(기본 300초=5분)마다:

0. **읽을 디렉토리를 다시 찾습니다** (아래 "어디를 읽는가")
1. 찾은 디렉토리에서 **변경된 파일만** 증분 파싱 (size/mtime 스킵)
2. 계정·세션·토큰 사용량을 하나의 배치(JSON gzip)로 묶어 **로컬 outbox**에 기록
3. outbox의 배치들을 **SSH로 NAS에 전송**:
   `scp` 로 `up-<name>` 임시 업로드 → 원격 `mv` 로 `batch-<name>` 원자적 전환
   (중앙은 `batch-*` 만 읽으므로 반쪽 파일을 보지 않음)
4. 전송 성공한 배치는 로컬에서 삭제, 실패하면 **outbox에 남겨 다음 주기에 재시도**
   (NAS/네트워크 일시 장애에도 데이터 유실 없음). 원격의 오래된 배치는 자동 정리.

전송 모드는 `transport.mode`:
- **`ssh`** (기본): NAS 미마운트 서버 → `scp`로 업로드, 비번 자동입력 또는 키
- **`local`**: NAS가 마운트된 서버 → 마운트 경로에 직접 기록

---

## 어디를 읽는가

**설정에 적힌 목록은 울타리가 아니라 힌트입니다.** 매 주기마다 세 곳을 합쳐
읽을 디렉토리를 정합니다.

| 출처 | 무엇을 보나 | 왜 필요한가 |
|---|---|---|
| `process` | 이 사용자의 살아 있는 claude/codex 프로세스의 `CLAUDE_CONFIG_DIR`·`CODEX_HOME` (`/proc/<pid>/environ`) | **유일하게 확실한 근거.** 도구는 기동 시점의 환경이 정한 곳에 씁니다 |
| `glob` | `~/.claude*`, `~/.codex*` (+`extra_roots`) | 지금 꺼져 있는 계정도 놓치지 않기 위해 |
| `config` | `claude.config_dirs`, `codex.dirs` | 사람이 꼭 보라고 지정한 곳 |

찾은 경로는 **그 도구의 디렉토리가 맞는지 확인한 뒤에만** 씁니다(`projects/`·
`auth.json` 같은 표식, 둘 다 가진 `sessions/` 는 내용 모양으로 구분). 다른 사용자의
프로세스는 `/proc` 권한이 막아주므로 **자기 사용자 것만** 봅니다.

### 왜 이렇게 바꿨나

예전에는 `config.json` 에 적힌 것만 읽었고, `setup-accounts.py` 가 그 목록을 랩
디렉토리로 **덮어썼습니다.** 그래서 이런 것들이 통째로 사라졌습니다.

* 확장 업데이트로 래퍼가 풀린 **VSCode 사이드바** → `~/.codex` 로 떨어지는데 수집 밖
* 설정 전에 열어둔 tmux pane, 맨 `claude`
* **홈 밖 설정 디렉토리** — 실제로 한 사용자가
  `CLAUDE_CONFIG_DIR=/mnt/data/<user>/.claude-<user>`, `CODEX_HOME=/mnt/nvme1/...` 로
  씁니다. `~/.claude*` glob 은 둘 다 못 찾아, 그 노드는 몇 주 동안 **계정만 보고하고
  usage=0** 이었습니다.

화면에서는 이 모든 경우가 "그날 아무도 안 썼다" 와 똑같이 보입니다. 그래서 지금은
**넓게 읽고, 못 읽는 것이 있으면 배치에 적어 보냅니다.**

개인 계정을 빼고 싶으면 `exclude_dirs` 에 경로를 적으세요. 어떤 계정을 집계에
넣을지는 중앙의 `tracking.allowed_accounts` 가 정합니다.

### 같은 로그인이 여러 디렉토리에 있을 때

흔합니다. `~/.claude` 와 `~/.claude-lab1` 이 같은 계정으로 로그인돼 있거나,
`migrate-history.py` 를 `--move` 없이 돌려 **같은 이력이 양쪽에 남은** 경우입니다.
넓게 읽게 되면서 생긴 위험이라 배치를 만들 때 정리합니다.

| 상황 | 결과 |
|---|---|
| 같은 턴(uuid)이 두 디렉토리에서 읽힘 | **한 번만 보냅니다.** 계정을 증명할 수 있는 쪽을 남깁니다 |
| 같은 계정인데 디렉토리마다 한 일이 다름 | 둘 다 보냅니다 — 중복이 아니라 서로 다른 작업입니다 |
| 같은 로그인이 여러 디렉토리에 | 계정 카드는 **하나**로 합칩니다(한도를 가진, 더 최근 쪽) |
| 계정 이메일은 같은데 `account_id` 가 다름 | 다른 계정이므로 따로 둡니다 |

중복을 버릴 때는 몇 건인지(`diagnostics.duplicates_dropped`)와 함께 경고를 남깁니다 —
대개는 이력을 복사만 하고 원본을 안 지운 것이라, 고칠 데가 따로 있다는 신호입니다.

작업 디렉토리(`cwd`)는 레코드마다 그대로 실려 갑니다. 같은 계정으로 여러 프로젝트를
오가도 중앙의 사람별 집계(cwd 규칙)는 그대로 동작합니다.

## 사람은 무엇으로 갈리나 — `cwd`

중앙은 세션의 **작업 디렉토리**를 `people.rules` 의 glob 에 맞춰 사람을 정합니다
(`*/yunseok*` → yunseok). OS 계정을 여럿이 함께 쓰는 서버에서도 각자 자기 디렉토리
아래에서 일하면 이것으로 갈립니다. **그래서 `cwd` 가 비면 그 세션은 누구에게도
붙지 않습니다.**

codex 세션 행이 바로 그랬습니다. 롤아웃 파일이 그 주기에 안 바뀌었으면 메타데이터를
다시 읽지 않고 빈 값으로 행을 만들어, `cwd`·`version`·`started_at` 이 전부 `None` 인
채 나갔습니다. 직전 스냅샷의 **담당자 미지정 세션 81개 중 79개가 규칙이 모자라서가
아니라 이것** 때문이었습니다. 지금은 필요한 순간에 파일 머리에서 `session_meta` 를
다시 읽습니다(캐시하며, sender 를 재시작해도 동작합니다).

남는 한계는 분명합니다. **여러 사람이 같은 OS 계정으로 들어와 같은 디렉토리에서
일하면 데이터에 둘을 가를 근거가 없습니다.** 그때의 선택지는 셋입니다.

1. 사람마다 자기 하위 디렉토리에서 작업 (가장 간단하고, 지금 규칙이 그대로 동작)
2. 사람마다 다른 랩 계정으로 로그인 (계정으로 갈림)
3. 중앙 `people.rules` 에 `host`+`provider` 폴백 (그 노드에서 도구당 한 사람일 때만)

## 터미널이냐 사이드바냐

모든 usage 레코드와 세션 행에 `surface` 가 붙습니다. 두 도구가 **이미 기록하고
있던 값**을 그대로 실어 보내는 것뿐입니다.

| surface | Claude (`entrypoint`) | Codex (`originator` / `source`) |
|---|---|---|
| `terminal` | `cli` | `codex_cli_rs` |
| `vscode` | `vscode` | `codex_vscode` |
| `desktop` | — | `Codex Desktop` |
| `sdk` | `sdk-*`, `mcp` | — |
| `other:<원본값>` | 모르는 값은 **그대로 통과** | 〃 |

> 데스크탑 앱은 `source` 에도 `vscode` 를 넣습니다. 그래서 `originator` 를 먼저
> 봅니다 — `source` 를 믿으면 데스크탑 사용이 전부 사이드바로 뭉개집니다.

배치 로그에도 바로 나옵니다:
```
[sender] batch ... surfaces[terminal=7 vscode=140] ...
```

## 수집이 새고 있으면 배치가 말합니다

모든 배치에 `diagnostics` 가 들어갑니다. **침묵이 유일한 증상이던 것**을 데이터로
바꾸는 것이 목적입니다.

| 필드 | 내용 |
|---|---|
| `dirs[]` | 읽은 디렉토리, **어떻게 찾았는지**(`found_by`), 계정, 건수 |
| `surfaces` | 이번 배치의 표면별 턴 수 |
| `processes[]` | 살아 있는 도구 프로세스와 그 기록 위치, `collected` 여부 |
| `uncollected[]` | **쓰이고 있는데 아무도 안 읽는 디렉토리** |
| `warnings[]` | 없는 경로 지정 등 |

같은 내용을 서버에서 바로 보려면:
```bash
./status.sh                          # 커버리지 + 경고 + 최근 로그
python3 scripts/where-landed.py      # 디렉토리별 계정·기록수·수집여부
python3 scripts/fleet-status.py      # (NAS 가 보이는 곳에서) 노드 전체 상태
```

여러 서버·여러 사용자에게 배포하는 절차와 **그대로 복사해 쓰는 사용자 공지문**은
[ROLLOUT.md](ROLLOUT.md) 에 있습니다.

## 한 머신이 여러 노드로 보이던 문제

sender 는 **OS 사용자마다 하나씩** 설치됩니다. 그래서 한 대에서 세 사람이 설치하면
`node_id` 가 서로 달라 대시보드에는 서버가 세 대로 보였습니다(실제로
`ADS-A100`·`aidas-a100`·`hgkim_AIDAS_A100` 이 모두 같은 장비입니다).

`node_id` 는 기존 이력의 키라 그대로 두고, 모든 배치에 아래를 함께 실어 보냅니다.
중앙에서 이 값으로 묶으면 한 대로 보입니다.

| 필드 | 값 |
|---|---|
| `machine_id` | `/etc/machine-id` 의 해시 (같은 장비면 사용자가 달라도 동일) |
| `os_user` | sender 가 도는 OS 계정 |
| `home` | 그 계정의 `$HOME` (컨테이너에서 여러 사람이 같은 경로를 쓰는 경우 구분용) |
| `fqdn`, `ip` | 기존 필드 |

### Codex 토큰 매핑

`~/.codex/sessions/**/rollout-*.jsonl` 의 `token_count` 이벤트에서 턴별
사용량(`last_token_usage`)을 읽어 공용 4-요소 스키마로 환산:

| 공용 필드 | Codex 소스 |
|---|---|
| `cache_read_tokens` | `cached_input_tokens` |
| `input_tokens` | `input_tokens − cached_input_tokens` |
| `output_tokens` | `output_tokens` (reasoning 포함) |
| `cache_creation_tokens` | 0 |

→ `total = input + output + cache_read = codex total_tokens`. 계정 식별(email/plan)은
`~/.codex/auth.json`의 id_token(JWT)에서 **email·plan만** 추출하며,
**토큰/비밀번호/대화 본문은 NAS로 전송하지 않습니다.**

---

## 설정 (`config.json`, setup.sh가 생성·chmod 600)

```json
{
  "node_id": "gpu7",
  "interval_seconds": 300,
  "retain_hours": 72,
  "compress": true,
  "nas_root": "/mnt/nas/yunseok/ai-monitoring",
  "transport": {
    "mode": "ssh",
    "ssh_host": "aidaslab.synology.me",
    "ssh_port": 2244,
    "ssh_user": "synologynas",
    "ssh_password": "********",
    "ssh_key": "",
    "remote_root": "/volume1/nas-nfs/yunseok/ai-monitoring"
  },
  "discover": true,
  "extra_roots": [],
  "exclude_dirs": [],
  "bootstrap_days": 2,
  "claude": { "enabled": true, "config_dirs": [] },
  "codex":  { "enabled": true, "dirs": ["~/.codex"], "include_archived": false }
}
```

- `discover`(기본 `true`) — 살아 있는 프로세스와 파일시스템에서 설정 디렉토리를
  매 주기 찾습니다. `false` 로 두면 **`config_dirs`/`dirs` 에 적힌 것만** 읽습니다
  (예전 동작). 끄면 랩 밖 세션이 다시 안 보이게 되므로 권하지 않습니다.
- `config_dirs`/`dirs` — 자동 탐색에 **더해지는** 목록입니다. 덮어쓰지 않습니다.
- `extra_roots` — `~` 말고 더 훑을 상위 경로 (예: `["/mnt/data/myname"]`).
  프로세스가 떠 있으면 없어도 찾지만, 꺼져 있을 때도 보이게 하려면 적어두세요.
- `exclude_dirs` — 절대 읽지 않을 경로. 개인 계정을 빼는 유일한 수단입니다.
- `bootstrap_days`(기본 2) — **처음 보는** 디렉토리에서 이보다 오래된 기록은 읽지
  않고 커서만 찍습니다. 그 기록들은 어차피 계정을 증명할 수 없어 `assumed` 로
  전송되고 중앙에서 버려지므로, 올려봐야 대역폭만 씁니다. 실제로 한 디렉토리는
  21만 턴(비압축 111MB)이었고, 이 가드로 첫 배치가 0.4MB 로 줄었습니다. `0` 이면 전부.
- 계정 식별: 기본 `~/.claude` 만 기존 `~/.claude.json` 로그인 정보를 읽고,
  별도 `CLAUDE_CONFIG_DIR`(예: `~/.ys-claude`)는 **자기 디렉터리 안의**
  `.claude.json`이 있어야 계정을 식별합니다. 별도 프로필이 기본 계정 정보를
  빌려 쓰는 fallback은 하지 않습니다.
- Claude JSONL에는 계정이 없으므로 송신기는 파일별 byte cursor와 계정 정보를
  함께 저장합니다. 처음 발견한 기존 기록, 로그인 전환을 가로지르는 기록, 전환
  직후 첫 폴링은 `assumed`/무계정으로 전송되어 중앙에서 집계하지 않습니다.
  전환 후 새 세션을 시작하면 이후에 추가된 바이트부터 안전하게 귀속됩니다.
- `ssh_key`를 지정하면 비밀번호 대신 키 인증(BatchMode).
- `remote_root`는 **NAS상의 절대경로**(= 중앙 마운트 `/mnt/nas/yunseok/ai-monitoring`).
- Codex 과거 전체를 한 번 백필하려면 `codex.include_archived: true`.

---

## 보안 / 개인정보

- NAS로 가는 것은 **사용량 메타데이터 + 계정 email/plan** 뿐. 토큰·키·대화 본문 없음.
- `config.json`(비밀번호 포함)은 `chmod 600` + `.gitignore`. 키 인증이 더 안전합니다.
- 첫 접속 시 NAS 호스트키를 `data/known_hosts`에 저장(이후 검증).

---

## 파일 구조

```
ai-monitoring-send/
  setup.sh            # ★ 하나만 실행
  start.sh stop.sh status.sh
  config.example.json
  sender/
    main.py            # 수집 루프 + 수집기 레지스트리 + outbox + 전송
    discovery.py       # 읽을 디렉토리 찾기 + 표면(터미널/사이드바) 분류
    claude_collector.py
    codex_collector.py
    nas_writer.py      # 배치 gzip 생성
    transport.py       # local / ssh 전송 (원자적 업로드 + 보존정리)
    sshcmd.py          # pty 기반 ssh/scp 비밀번호 자동입력
  scripts/             # 계정 분리·사이드바·이관·검증·플릿 상태 도구
  tests/
    test_discovery.py  # python3 tests/test_discovery.py (의존성 없음)
  data/                # outbox/상태/로그/known_hosts (gitignore)
```

## 테스트

```bash
python3 tests/test_discovery.py
```
표면 분류, 홈 밖 디렉토리 탐지, 설정이 다른 디렉토리를 가리지 않는지,
Claude/Codex 디렉토리 오인, 부트스트랩 가드를 검사합니다. 표준 라이브러리만
쓰므로 어느 서버에서나 그대로 돕니다.

### 노드 이름은 자동입니다

`--host` 는 선택입니다. 이름은 이 순서로 정해집니다:

1. 기존 `config.json` 의 `node_id` — 업그레이드로 이름이 바뀌지 않게
2. **NAS 기록** — 이 장비가 이미 쓰던 이름을 물려받습니다
3. `hostname` — 처음 등록하는 장비

판별은 `fqdn` 으로 합니다. `machine_id` 는 보강으로만 씁니다 — 같은 컨테이너
이미지로 뜬 파드 4개가 `/etc/machine-id` 를 공유해 값이 전부 같았고,
그것만 보면 서로 다른 노드가 하나로 합쳐집니다.

새 장비에 원하는 이름을 붙일 때만 `--host <이름>` 을 주세요.
