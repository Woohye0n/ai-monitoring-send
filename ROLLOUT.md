# 배포와 사용자 공지

여러 서버·여러 사용자에게 sender 를 깔 때의 절차입니다. 단계별 설치 내용은
[DEPLOY-NEW-SERVER.md](DEPLOY-NEW-SERVER.md), 코드 변경 이력은
[REDEPLOY.md](REDEPLOY.md) 를 보세요. 여기서는 **누가 무엇을 실행하는지**만 다룹니다.

핵심 전제 두 가지입니다.

* **sender 는 OS 사용자마다 하나씩** 필요합니다. 자기 홈의 기록만 읽기 때문입니다
  (`/proc` 권한이 남의 것을 막아 줍니다 — 그게 맞는 경계입니다).
  **한 대에 한 개 깔면 그 사람 것만 잡힙니다.**
* **노드 이름은 장비 이름입니다.** 사용자마다 다른 이름을 주면 대시보드에 서버가
  여러 대로 보입니다. 실제로 한 장비가 `ADS-A100`·`aidas-a100`·`hgkim_AIDAS_A100`
  세 대로 보이고 있었습니다.

---

## 0. 관리자: 서버마다 한 번 정할 것

```bash
hostname                                   # 이게 쓸 만하면 그대로 쓰면 됩니다
ls -d /mnt/nas/yunseok/ai-monitoring 2>/dev/null && echo "NAS 마운트 있음"
```

| 확인 | 결과에 따라 |
|---|---|
| **노드 이름** | `hostname` 이 `aidas`, `gpu7` 처럼 장비를 가리키면 **아무 것도 지정하지 마세요**(기본값이 곧 `hostname`). `localhost`·`ubuntu` 같으면 setup 이 거부하니 이름을 정해 공지하세요 |
| **NAS 마운트** | 있으면 **비밀번호가 전혀 필요 없습니다**(자동으로 local 모드). 없으면 아래 1-B |

> 이미 이 장비에서 다른 이름으로 보고 중이면 `setup.sh` 가 설치 도중에 알려줍니다
> (`fqdn`+`ip`, 업그레이드 후에는 `machine_id` 로 판정). 그 이름으로 맞추세요.

---

## 다른 서버에 켜기 — 한 줄

```bash
curl -fsSL https://woohye0n.github.io/gpu-grants-dashboard/install-sender.sh | sudo bash
```

이게 전부입니다. 코드 내려받기부터 전원 설치까지 합니다.

**이미 송신기가 돌던 서버에서는 아무것도 묻지 않습니다** — 그 서버의 `config.json`
에 NAS 접근 정보가 이미 들어 있기 때문입니다. 없을 때만 **실행 도중에** 물어봅니다.

> 접속 정보(주소·포트·계정·경로)는 기존 설치에서 배워 오고, 없으면 기본값을
> 씁니다. **비밀번호만** 그때 물어봅니다 — 설치기에는 담겨 있지 않습니다.
> 다른 NAS 를 쓰려면 `SSH_HOST=… SSH_USER=… | sudo -E bash` 로 덮어쓰세요.

먼저 확인만:
```bash
curl -fsSL https://woohye0n.github.io/gpu-grants-dashboard/install-sender.sh | sudo bash -s -- --dry-run
```

### 인터넷이 막힌 서버

GPU 서버 중에는 GitHub 에 나가지 못하는 곳이 있습니다(`SSL_connect: Connection
reset`). 그래도 **NAS 에는 닿습니다** — 송신기가 거기로 배치를 보내니까요.

```bash
# NAS 가 마운트된 서버
cp -r /mnt/nas/yunseok/ai-monitoring-send-dist ~/aidas-sender && \
  sudo bash ~/aidas-sender/scripts/bootstrap.sh

# 마운트도 없는 서버 (송신기가 쓰는 그 NAS 계정 그대로, 비번 1회)
scp -P 2244 -r synologynas@aidaslab.synology.me:/volume1/nas-nfs/yunseok/ai-monitoring-send-dist \
    ~/aidas-sender && sudo bash ~/aidas-sender/scripts/bootstrap.sh
```

`bootstrap.sh` 는 그 서버의 기존 `config.json` 에서 노드 이름과 자격증명을
물려받으므로, **scp 에서 한 번 친 비밀번호 외에는 아무것도 묻지 않습니다.**

`bootstrap.sh` 가 **물어보지 않아도 되는 건 묻지 않습니다.**

| | 어떻게 알아내나 |
|---|---|
| 노드 이름 | 기존 설치의 `config.json` → 없으면 NAS 기록에서 `fqdn`+`ip` 로 같은 장비를 찾아 그 이름 → 없으면 `hostname` |
| NAS 자격증명 | NAS 가 마운트돼 있으면 **불필요** → 없으면 기존 설치의 `config.json` 에서 가져옴 → 둘 다 없을 때만 **그때 터미널에서** 입력받음 |
| 누구에게 설치 | `/etc/passwd` 의 실제 사용자 중 claude/codex 흔적이 있는 사람. 홈을 공유하면 한 번만 |

즉 **이미 sender 가 돌던 서버에서는 아무것도 묻지 않습니다.** 비밀번호를 미리 파일로
써 두거나 명령줄에 적을 일이 없습니다(`ps` 에 남지도 않습니다).

먼저 확인만:
```bash
sudo ~/aidas-sender/scripts/bootstrap.sh --dry-run
```

### 노드 이름을 직접 주고 싶으면

```bash
sudo ~/aidas-sender/scripts/bootstrap.sh --host kakao-b200-2
```
새 이름을 지으면 대시보드에서 **다른 서버로 보입니다**(이력이 갈립니다). 그래서
기본값은 "이 장비가 이미 쓰던 이름" 입니다. 한 장비가 여러 이름으로 보고 중이면
`scripts/fleet-status.py` 가 알려줍니다.

```
ADS-A100 = aidas-a100 = hgkim_AIDAS_A100     ← 같은 장비
gpu-2-0  = kakao-b200-2                       ← 같은 장비
```

이름을 합치면 중앙 설정(`gpu-grants-dashboard/ai_snapshot.json`)의 `host:` 기반
사람 규칙이 죽습니다. 그 사람의 `cwd_glob` 규칙이 있는지 먼저 보세요.

---

## 1-A. NAS 가 마운트된 서버 — 사용자가 직접

공지문만 뿌리면 끝입니다(아래 2절). 자격증명이 필요 없어 관리자가 할 일이 없습니다.

## 1-B. NAS 가 마운트 안 된 서버 — 자격증명이 필요

NAS 비밀번호를 열 몇 명에게 나눠주지 마세요. 둘 중 하나를 고릅니다.

**① 관리자가 각 사용자 계정으로 대신 설치 (권장)**

```bash
NODE=gpu7                      # 이 장비의 이름 — 전 사용자 동일
read -rs NAS_PW                # 화면에 안 보이게 입력

for u in $(getent passwd | awk -F: '$3>=1000 && $7!~/nologin|false/ {print $1}'); do
  sudo -u "$u" -H --preserve-env=NAS_PW NAS_PW="$NAS_PW" bash -lc '
    cd ~ || exit 0
    [ -d ai-monitoring-send ] || git clone -q https://github.com/AIDASLab/ai-monitoring-send.git
    cd ~/ai-monitoring-send && git pull -q
    SSH_PASSWORD="$NAS_PW" ./setup.sh --host '"$NODE"' >/dev/null 2>&1 \
      && echo "  OK   $USER" || echo "  FAIL $USER"'
done
```
설치했더라도 **공지는 반드시 보내세요**(2절). 모르는 사이에 깔린 에이전트는
그 자체가 문제입니다.

**② 사용자별 SSH 키** — 각자 키를 만들어 공개키를 관리자에게 보내고, 관리자가 NAS
`~/.ssh/authorized_keys` 에 추가합니다. 그 뒤 사용자는
`./setup.sh --key ~/.ssh/id_ed25519_nas` 로 설치합니다. 안전하지만 사람당 왕복이
한 번 더 생깁니다.

## 1-C. 이미 sender 가 돌고 있는 서버

```bash
cd ~/ai-monitoring-send && git pull && ./stop.sh && ./start.sh && ./status.sh
```
설정은 그대로 두면 됩니다. 자동 재시작(cron)이 아직 없다면 한 번 더:
```bash
./setup.sh --host <기존과 같은 이름>    # config.json 이 있으면 건드리지 않고 cron 만 등록
```

---

## 2. 사용자에게 보낼 공지문 (그대로 복사해서 쓰세요)

> ### AI 사용량 집계 에이전트를 설치해 주세요 (2분)
>
> 랩 Claude/Codex 계정의 사용량·한도를 한곳에서 보기 위한 것입니다.
> **각자 계정에 하나씩** 있어야 그 사람 작업이 집계됩니다.
>
> ```bash
> git clone https://github.com/AIDASLab/ai-monitoring-send.git ~/ai-monitoring-send
> cd ~/ai-monitoring-send
> ./setup.sh
> ```
> <!-- NAS 마운트가 없는 서버면 위 마지막 줄을 아래로 바꿔 공지하세요
>      SSH_PASSWORD='<NAS 비밀번호>' ./setup.sh
>      hostname 이 장비 이름이 아니면  ./setup.sh --host <관리자가 공지한 이름>  -->
>
> 끝입니다. **평소처럼 쓰시면 됩니다** — 터미널이든 VSCode 사이드바든,
> `claude`·`codex` 를 어떻게 실행하든 알아서 잡습니다. 설정할 것 없습니다.
> 재부팅해도 알아서 다시 뜹니다.
>
> **잘 되는지 확인**
> ```bash
> cd ~/ai-monitoring-send && ./status.sh
> ```
> `running (pid ...)` 이고, `coverage` 절의 claude/codex 프로세스가 전부 `[수집]`
> 이면 정상입니다.
>
> **무엇이 보내지나요**
> 보냅니다: 토큰 사용량 숫자, 모델명, 계정 이메일·요금제, 세션 ID, **작업 디렉토리
> 경로**와 git 브랜치 이름, 실행 표면(터미널/사이드바/데스크탑).
> 보내지 않습니다: **대화 내용, 코드, 파일 내용, API 토큰·비밀번호.**
> 코드는 전부 공개돼 있으니 직접 확인하실 수 있습니다.
>
> **개인 계정을 빼고 싶으면** `~/ai-monitoring-send/config.json` 의
> `"exclude_dirs": []` 에 경로를 적고 `./stop.sh && ./start.sh` 하세요. 예:
> `"exclude_dirs": ["~/.codex"]`
>
> **멈추려면** `cd ~/ai-monitoring-send && ./stop.sh && crontab -e` 에서
> `ai-monitoring-send` 줄을 지우세요.

### 랩 공용 계정을 쓰는 사람만 추가로

개인 계정으로 로그인한 사용량은 수집은 되지만 **랩 집계에는 안 들어갑니다**(중앙이
계정 allowlist 로 거릅니다). 랩 계정으로 일해야 한다면 한 번 더:

```bash
cd ~/ai-monitoring-send
python3 scripts/setup-accounts.py        # 랩 계정용 디렉토리·런처 생성
lab1 codex login                         # 랩 계정으로 로그인
lab1 claude auth login
```
이후 `lab1 codex`, `lab1 claude` 로 실행하고, VSCode 사이드바는
`python3 scripts/sidebar-account.py --lab lab1` 후 **창 리로드**.
자세한 건 [DEPLOY-NEW-SERVER.md](DEPLOY-NEW-SERVER.md).

> 계정을 하나만 쓴다면 이 단계는 필요 없습니다. 어느 디렉토리에 쌓이든 그 디렉토리에
> 로그인된 계정으로 집계되기 때문입니다.

---

## 3. 관리자: 배포 후 점검

**서버에서** — 도구를 쓰는 사람 대비 sender 가 얼마나 깔렸는지:

```bash
echo "도구 사용자:"; ps -eo user:16,args | grep -iE "(^|/)claude|codex" \
  | grep -v grep | grep -v ai-monitoring | awk '{print $1}' | sort -u
echo "sender 실행 중:"; ps -eo user:16,args | grep "[s]ender.main" | awk '{print $1}' | sort -u
```

**NAS 가 보이는 곳에서** — 노드 전체를 한 화면에:

```bash
python3 scripts/fleet-status.py --nas /mnt/nas/yunseok/ai-monitoring
```
```
노드                         최근  os_user     dirs  표면                       상태
------------------------------------------------------------------------------------
ADS-A100                   3분  jhkim          2  terminal=18
aidas-a100                11일  skjinlee       -  -                        멈춤 구버전
hgkim_AIDAS_A100           3분  hgkim          3  vscode=204 terminal=9

같은 장비가 여러 노드로 보고 중 — 이름을 하나로 통일하세요:
  ADS-A100 = aidas-a100 = hgkim_AIDAS_A100
```
문제가 있으면 종료코드 1 이라 cron 알림에 그대로 쓸 수 있습니다.
`구버전` 은 아직 새 sender 가 아니라는 뜻입니다(그 노드는 `dirs`·`표면`이 비어 있습니다).

| 증상 | 원인 / 조치 |
|---|---|
| 최신 배치가 며칠 전 | sender 가 죽었고 cron 도 없음 → `./setup.sh --host <같은이름>` 로 워치독 등록 |
| `diagnostics.uncollected` 가 비지 않음 | 쓰이는데 아무도 안 읽는 디렉토리 — `exclude_dirs` 로 뺀 게 아니면 버그이니 알려주세요 |
| `surfaces` 에 `terminal` 만 | 사이드바를 쓰는 사람인데 안 잡히는 중. 그 서버에서 `python3 scripts/where-landed.py` |
| 같은 장비가 여러 노드 | 이름 통일 후 재설치. 새 배치의 `machine_id` 가 같으면 같은 장비입니다 |
| `accounts` 가 개인 계정 | 랩 계정 로그인 필요 (2절 마지막) |

---

## 4. 알아둘 제약

* **사람마다 설치해야 합니다.** 관리자가 한 번에 끝낼 방법은 1-B ① 뿐입니다.
* **과거 사용량은 안 잡힙니다.** 설치 시점 이전 기록은 계정을 증명할 수 없어
  집계에서 빠집니다(설치 후 2일치까지만 읽고 그 이전은 커서만 찍습니다).
* **첫 배치부터 2회차까지는 `assumed`** 로 나갑니다. 계정이 두 번 연속 같아야
  귀속하기 때문이며, 정상입니다.
* **cron 이 없는 환경**에서는 자동 재시작이 등록되지 않습니다. setup 이 경고하며,
  그때는 `./setup.sh --systemd` 출력물을 쓰세요.
