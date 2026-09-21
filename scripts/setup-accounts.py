#!/usr/bin/env python3
"""계정별 디렉토리 리팩토링 — 배포용.

한 서버에서 랩 계정과 개인 계정을 섞어 쓰면 사용량 귀속이 오염됩니다
(같은 설정 디렉토리 안에서 로그아웃/로그인하면 과거 기록이 현재 계정으로
재라벨됨). 이 스크립트는 "디렉토리 하나 = 계정 하나" 구조를 만들어 그 문제를
구조적으로 없애고, sender 가 랩 디렉토리만 수집하도록 설정을 맞춥니다.

만드는 것:
  ~/.claude-<lab>  ~/.codex-<lab>      계정별 설정 디렉토리 (비어 있음; 로그인은 사람이)
  ~/.local/bin/<lab>                   계정 전환 런처 — `lab1 codex resume`
  ~/.local/bin/codex-<lab>, claude-<lab>  도구별 래퍼 (자동화·cron 용)
  ~/.bashrc 의 lab1/lab2 함수          같은 일을 하는 셸 함수 (런처가 주 경로)
  사이드바 계정                        sidebar-account.py 에 위임 (Claude=설정,
                                       Codex=확장 번들 바이너리 래핑)
  sender config.json                   claude.config_dirs / codex.dirs 를 랩 디렉토리로

건드리지 않는 것:
  ~/.claude, ~/.codex (개인 계정 경로) — 기존 기록·로그인 그대로 둡니다.
  로그인 — 대화형이라 사람이 직접 해야 합니다. 마지막에 명령을 출력합니다.

    python3 scripts/setup-accounts.py --dry-run     # 무엇이 바뀌는지만 출력
    python3 scripts/setup-accounts.py               # 적용 (수정 전 .bak 백업)
    python3 scripts/setup-accounts.py --labs lab1   # 랩 계정 1개만
    python3 scripts/setup-accounts.py --no-vscode   # VSCode 설정은 건너뜀
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASHRC = os.path.join(HOME, ".bashrc")
BEGIN = "# >>> aidas lab account switcher >>>"
END = "# <<< aidas lab account switcher <<<"

changes: list[str] = []
warnings: list[str] = []


def say(msg):
    print(msg)


def backup(path):
    """Copy path aside once per run so a re-run never destroys prior state."""
    if os.path.exists(path):
        dst = f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(path, dst)
        return dst
    return None


def which(name):
    for d in (os.path.join(HOME, ".local", "bin"), "/usr/local/bin", "/usr/bin"):
        p = os.path.join(d, name)
        if os.access(p, os.X_OK):
            return p
    return shutil.which(name)


# ---------------------------------------------------------------- directories
def make_dirs(labs, dry):
    for lab in labs:
        for base in (".claude-", ".codex-"):
            d = os.path.join(HOME, base + lab)
            if os.path.isdir(d):
                say(f"  = 이미 있음  {d}")
                continue
            changes.append(f"mkdir {d}")
            if not dry:
                os.makedirs(d, mode=0o700, exist_ok=True)
            say(f"  + 생성      {d}")


# ------------------------------------------------------------------- wrappers
LAUNCHER = """#!/bin/sh
# 계정 전환 런처 — `{lab} <명령> [인자...]` 를 {lab} 계정으로 실행합니다.
#
#   {lab} codex resume     {lab} claude     {lab} codex exec ...
#
# ~/.bashrc 의 {lab}() 함수와 같은 일을 하지만 **실제 실행 파일**입니다. 함수는
# rc 파일을 읽은 셸에서만 살아 있어서, 설정 전에 열어둔 탭이나 tmux pane,
# 비대화형 셸, 스크립트에서는 "command not found" 가 납니다. 이 파일은 PATH 에
# 있으면 어디서든 동작합니다.
exec env CODEX_HOME="$HOME/.codex-{lab}" CLAUDE_CONFIG_DIR="$HOME/.claude-{lab}" "$@"
"""

TOOL_WRAPPER = """#!/bin/sh
# {tool} 래퍼 — {lab} 계정({var}=~/{dirname})으로 고정 실행.
#
# CODEX_HOME/CLAUDE_CONFIG_DIR 을 상속받지 못하는 호출자(cron, 스크립트,
# 다른 도구가 띄우는 프로세스)가 맨 `{tool}` 을 쓰면 개인 계정으로 떨어져 랩
# 사용량이 집계에서 샙니다. 그런 곳에서는 `{tool}` 대신 이 경로를 부르세요.
exec env {var}="$HOME/{dirname}" {bin} "$@"
"""


def _write_exec(path, body, dry, label):
    if os.path.exists(path) and open(path, encoding="utf-8",
                                     errors="replace").read() == body:
        say(f"  = 이미 최신  {path}")
        return
    changes.append(f"write {path}")
    if not dry:
        backup(path)
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        os.chmod(path, 0o755)
    say(f"  + {label}  {path}")


def make_wrappers(labs, codex_bin, claude_bin, dry):
    """PATH 에 놓는 실행 파일들: labN 런처 + codex-labN / claude-labN 래퍼."""
    out = {}
    bindir = os.path.join(HOME, ".local", "bin")
    if not dry:
        os.makedirs(bindir, exist_ok=True)
    if bindir not in (os.environ.get("PATH") or "").split(os.pathsep):
        warnings.append(f"{bindir} 가 PATH 에 없습니다. lab1/codex-lab1 을 이름으로 "
                        f"부를 수 없으니 ~/.bashrc 에 "
                        f'export PATH="$HOME/.local/bin:$PATH" 를 추가하세요.')
    for lab in labs:
        # 어느 셸에서든 되는 런처. 함수가 안 잡히는 상황의 해결책.
        _write_exec(os.path.join(bindir, lab), LAUNCHER.format(lab=lab), dry, "런처  ")
        for tool, binpath, var in (("codex", codex_bin, "CODEX_HOME"),
                                   ("claude", claude_bin, "CLAUDE_CONFIG_DIR")):
            if not binpath:
                warnings.append(f"{tool} 실행 파일을 찾지 못해 {tool}-{lab} 래퍼를 "
                                f"만들지 못했습니다 (--{tool}-bin 으로 지정하세요).")
                continue
            p = os.path.join(bindir, f"{tool}-{lab}")
            if tool == "codex":
                out[lab] = p
            _write_exec(p, TOOL_WRAPPER.format(
                tool=tool, lab=lab, var=var, bin=binpath,
                dirname=f".{tool}-{lab}"), dry, "래퍼  ")
    return out


# ---------------------------------------------------------------- dispatcher
DISPATCHER = """#!/bin/sh
# AIDAS: codex 진입점 디스패처.
#
# 데스크탑 앱이 SSH 로 붙으면 sshd 는
#   PATH="${{CODEX_INSTALL_DIR:-$HOME/.local/bin}}:$PATH"; codex app-server proxy
# 를 실행합니다. 그 셸에는 CODEX_HOME 을 넣을 방법이 없습니다
# (PermitUserEnvironment 는 보통 꺼져 있고, .bashrc 는 비대화형에서 즉시 return).
# 그래서 IDE·원격 클라이언트 전용 진입점인 `app-server` 서브커맨드일 때에 한해
# 랩 계정으로 고정합니다. 터미널 대화형(codex, codex resume)은 손대지 않으므로
# 맨 codex 는 그대로 개인 계정(~/.codex)입니다.
#
# 데스크탑 앱은 CODEX_HOME 을 비워두지 않고 개인 기본값(~/.codex)을 명시적으로
# 넘깁니다. 그래서 "미설정일 때만" 으로는 안 잡히고, 개인 기본값과 같을 때도
# 갈아끼웁니다. 반대로 lab1/lab2 처럼 의도적으로 지정한 값은 그대로 존중합니다.
#
# 원복: ln -sfn {real} {path}
REAL="{real}"
DEFAULT_APP_SERVER_HOME="{home}"
PERSONAL_HOME="$HOME/.codex"

# 플래그를 건너뛰고 첫 서브커맨드를 찾는다 (-c KEY=VAL 처럼 값을 먹는 플래그 주의).
sub=""; skip=0
for a in "$@"; do
    if [ "$skip" = 1 ]; then skip=0; continue; fi
    case "$a" in
        -c|--config|--cd|-C|--model|-m) skip=1 ;;
        -*) ;;
        *) sub="$a"; break ;;
    esac
done

if [ "$sub" = "app-server" ] &&
   {{ [ -z "$CODEX_HOME" ] || [ "$CODEX_HOME" = "$PERSONAL_HOME" ]; }}; then
    exec env CODEX_HOME="$DEFAULT_APP_SERVER_HOME" "$REAL" "$@"
fi
exec "$REAL" "$@"
"""


def install_dispatcher(sidebar_lab, dry):
    """PATH 의 codex 를 디스패처로 바꿔 원격 클라이언트만 랩으로 돌린다."""
    path = os.path.join(HOME, ".local", "bin", "codex")
    if not os.path.exists(path):
        say("  = PATH 에 codex 없음 — 건너뜀")
        return
    home = os.path.join(HOME, f".codex-{sidebar_lab}")
    if os.path.islink(path):
        real = os.path.realpath(path)
    else:
        m = re.search(r'REAL="([^"]+)"', open(path, encoding="utf-8",
                                              errors="replace").read(4096))
        if not m:
            warnings.append(f"{path} 가 심볼릭 링크도 디스패처도 아닙니다. "
                            f"덮어쓰지 않았습니다 — 직접 확인하세요.")
            return
        real = m.group(1).replace("$HOME", HOME)
    if not os.access(real, os.X_OK):
        warnings.append(f"codex 실제 실행 파일을 찾지 못했습니다: {real}")
        return
    body = DISPATCHER.format(real=real, home=home, path=path)
    if not os.path.islink(path) and open(path, encoding="utf-8",
                                         errors="replace").read() == body:
        say(f"  = 이미 최신  {path}")
        return
    changes.append(f"write {path}")
    if not dry:
        # 심볼릭 링크를 먼저 지운다. 안 지우고 쓰면 링크를 따라가 실제 바이너리를
        # 덮어쓴다(실행 중이면 ETXTBSY 로 실패, 아니면 300MB 바이너리가 날아간다).
        if os.path.islink(path):
            os.symlink(real, f"{path}.symlink-backup") if not os.path.lexists(
                f"{path}.symlink-backup") else None
            os.unlink(path)
        else:
            backup(path)
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        os.chmod(path, 0o755)
    say(f"  + 디스패처  {path}  (app-server → ~/.codex-{sidebar_lab})")


# --------------------------------------------------------------------- bashrc
def bashrc_block(labs):
    lines = [BEGIN,
             "# 디렉토리 하나 = 계정 하나. export 하지 마세요(개인 작업이 랩으로 기록됨).",
             "#   lab1 claude / lab1 codex -> 랩1,   claude / codex -> 개인",
             "# `command` 를 쓰는 이유: claude() 같은 기존 함수 오버라이드를 우회해",
             "# 여기서 지정한 CLAUDE_CONFIG_DIR 가 덮어써지지 않게 하기 위함입니다."]
    for lab in labs:
        lines.append(
            f'{lab}() {{ CODEX_HOME=~/.codex-{lab} CLAUDE_CONFIG_DIR=~/.claude-{lab} command "$@"; }}')
    lines.append(END)
    return "\n".join(lines) + "\n"


def patch_bashrc(labs, dry):
    block = bashrc_block(labs)
    old = open(BASHRC, encoding="utf-8").read() if os.path.exists(BASHRC) else ""
    if BEGIN in old and END in old:
        new = re.sub(re.escape(BEGIN) + r".*?" + re.escape(END) + r"\n?", block, old, flags=re.S)
        verb = "갱신"
    else:
        # 마커 없이 손으로 넣었던 예전 블록이 있으면 걷어낸다. 우리가 만든 정확한
        # 형태(labN() { CODEX_HOME=... })와 그 머리말 주석만 지우므로 사용자가 쓴
        # 다른 설정은 건드리지 않는다. 안 지우면 정의가 중복된다.
        cleaned = re.sub(
            r"^\s*#\s*-+\s*AIDAS lab account switcher\s*-+\n(?:^\s*#.*\n)*", "",
            old, flags=re.M)
        cleaned = re.sub(
            r"^lab\w+\(\)\s*\{\s*CODEX_HOME=[^\n]*\}\s*\n", "", cleaned, flags=re.M)
        if cleaned != old:
            say("  ~ 기존 lab 함수 정의를 마커 블록으로 통합")
            old = cleaned
        new = old + ("\n" if old and not old.endswith("\n") else "") + "\n" + block
        verb = "추가"
    if new == old:
        say("  = 이미 최신  ~/.bashrc")
        return
    changes.append(f"{verb} ~/.bashrc")
    if not dry:
        b = backup(BASHRC)
        with open(BASHRC, "w", encoding="utf-8") as f:
            f.write(new)
        say(f"  + {verb}      ~/.bashrc  (백업: {os.path.basename(b) if b else '-'})")
    else:
        say(f"  + {verb}      ~/.bashrc")

    # 기존 오버라이드가 있으면 알려준다 — 로그인 명령이 엉뚱한 곳으로 갈 수 있음
    for m in re.finditer(r"^\s*(claude|codex)\s*\(\)\s*\{", old, flags=re.M):
        warnings.append(
            f"~/.bashrc 에 이미 {m.group(1)}() 함수 오버라이드가 있습니다. "
            f"로그인·수동 실행은 전체 경로로 하세요(그 함수가 환경변수를 덮어씁니다).")
        break


# --------------------------------------------------------------------- vscode
def patch_vscode(claude_lab, codex_lab, wrappers, dry):
    """사이드바 계정 지정은 sidebar-account.py 에 위임한다.

    Claude 는 정식 설정(claudeCode.environmentVariables)으로 되지만 Codex 는
    확장 번들 바이너리를 래핑해야 해서 로직이 길다. 두 곳에 복제하지 않고
    한쪽에만 두고, 여기서는 그걸 호출한다.
    """
    # 설치 형태(Remote 서버 / 로컬 / Insiders / code-server)를 모두 확인한다.
    if not any(os.path.isdir(os.path.join(HOME, d)) for d in
               (".vscode-server", ".vscode-server-insiders", ".vscode",
                os.path.join(".local", "share", "code-server"))):
        say("  = VSCode 설치 없음 — 건너뜀 (터미널 lab1/lab2 만 사용)")
        return
    helper = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "sidebar-account.py")
    if not os.path.exists(helper):
        warnings.append(f"{helper} 가 없어 사이드바 계정을 설정하지 못했습니다.")
        return
    # 두 확장이 서로 다른 랩을 써야 하면 한쪽씩 따로 호출한다. 같으면 한 번이면 된다.
    jobs = ([(claude_lab, None)] if claude_lab == codex_lab
            else [(claude_lab, "--claude-only"), (codex_lab, "--codex-only")])
    for lab, only in jobs:
        cmd = [sys.executable, helper, "--lab", lab] + ([only] if only else [])
        label = {"--claude-only": "claude", "--codex-only": "codex"}.get(only, "claude+codex")
        if dry:
            say(f"  ~ 실행 예정  sidebar-account.py --lab {lab}"
                f"{' ' + only if only else ''}  ({label})")
            changes.append(f"run {helper} --lab {lab} {only or ''}".strip())
            continue
        r = subprocess.run(cmd, capture_output=True, text=True)
        for line in (r.stdout or "").splitlines():
            if line.strip().startswith(("+", "-", "=", "!", "⚠")):
                say(f"  [{label}] {line.strip()}")
        if r.returncode != 0:
            warnings.append(f"사이드바({label}) 설정 실패: "
                            f"{(r.stderr or r.stdout).strip()[:200]}")
        elif "변경 없음" not in r.stdout:
            changes.append(f"sidebar {label}")


# --------------------------------------------------------------- sender config
def patch_sender_config(labs, dry):
    """랩 디렉토리를 수집 목록에 **더한다**.

    예전에는 이 목록을 랩 디렉토리로 통째로 덮어썼습니다. 그러면 런처를 거치지
    않은 세션 — 확장 업데이트로 래퍼가 풀린 VSCode 사이드바, 설정 전에 열어둔
    tmux pane, 맨 `claude` — 이 전부 수집 밖으로 떨어지고, 화면에는 "그날 아무도
    안 썼다" 와 똑같이 보였습니다. 지금은 sender 가 살아 있는 프로세스와
    파일시스템에서 쓰이는 디렉토리를 매 주기 찾아내므로, 이 목록은 "여기도 꼭
    보라" 는 힌트일 뿐 울타리가 아닙니다.
    """
    path = os.path.join(ROOT, "config.json")
    if not os.path.exists(path):
        warnings.append("sender config.json 이 아직 없습니다. setup.sh 로 만든 뒤 "
                        "이 스크립트를 다시 실행하면 수집 경로가 맞춰집니다.")
        return
    cfg = json.load(open(path, encoding="utf-8"))

    def merged(existing, wanted):
        out = list(existing or [])
        for d in wanted:
            if d not in out:
                out.append(d)
        return out

    before = (list(cfg.get("claude", {}).get("config_dirs") or []),
              list(cfg.get("codex", {}).get("dirs") or []))
    claude_dirs = merged(before[0], [f"~/.claude-{l}" for l in labs])
    codex_dirs = merged(before[1], [f"~/.codex-{l}" for l in labs])
    cfg.setdefault("claude", {})["config_dirs"] = claude_dirs
    cfg.setdefault("codex", {})["dirs"] = codex_dirs
    # 예전 설치본은 이 키가 없어 기본값(켜짐)으로 돌지만, 누군가 꺼두었다면
    # 랩 디렉토리만 보게 되므로 명시적으로 켠다.
    if cfg.get("discover") is False:
        cfg["discover"] = True
        say("  + discover  다시 켬 (랩 밖 세션도 수집)")
    if (before == (claude_dirs, codex_dirs)) and cfg.get("discover", True):
        say("  = 이미 최신  sender config.json")
        return
    changes.append("update sender config.json")
    if not dry:
        backup(path)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
            f.write("\n")
    say(f"  + 수집 경로  claude={claude_dirs}  codex={codex_dirs}")
    say("    (이 목록 밖이어도 실제로 쓰이는 디렉토리는 sender 가 찾아서 함께 수집합니다)")


# ------------------------------------------------------------------------ main
def main(argv=None):
    p = argparse.ArgumentParser(description="계정별 디렉토리 리팩토링")
    p.add_argument("--labs", default="lab1,lab2",
                   help="랩 계정 이름들 (쉼표 구분, 기본 lab1,lab2)")
    p.add_argument("--sidebar-claude", default=None, metavar="LAB",
                   help="Claude 사이드바만 다른 랩으로 (기본: --sidebar 값)")
    p.add_argument("--sidebar-codex", default=None, metavar="LAB",
                   help="Codex 사이드바·원격 진입점만 다른 랩으로 (기본: --sidebar 값)")
    p.add_argument("--sidebar", default=None,
                   help="VSCode 사이드바가 쓸 랩 (기본: 첫 번째)")
    p.add_argument("--claude-bin", default=None)
    p.add_argument("--codex-bin", default=None)
    p.add_argument("--no-vscode", action="store_true")
    p.add_argument("--no-bashrc", action="store_true")
    p.add_argument("--no-sender-config", action="store_true")
    p.add_argument("--dry-run", action="store_true", help="변경 없이 계획만 출력")
    a = p.parse_args(argv)

    labs = [x.strip() for x in a.labs.split(",") if x.strip()]
    if not labs:
        print("--labs 가 비었습니다", file=sys.stderr)
        return 2
    sidebar = a.sidebar or labs[0]
    # 도구마다 다른 계정을 쓰는 서버가 있다 (codex 는 랩2, claude 는 랩1 처럼).
    sidebar_claude = a.sidebar_claude or sidebar
    sidebar_codex = a.sidebar_codex or sidebar
    for name, lab in (("--sidebar-claude", sidebar_claude), ("--sidebar-codex", sidebar_codex)):
        if lab not in labs:
            warnings.append(f"{name}={lab} 이 --labs({','.join(labs)}) 에 없습니다. "
                            f"그 디렉토리는 만들어지지 않습니다.")
    claude_bin = a.claude_bin or which("claude")
    codex_bin = a.codex_bin or which("codex")
    dry = a.dry_run

    say(f"{'[DRY-RUN] ' if dry else ''}계정별 디렉토리 리팩토링  labs={labs}  "
        f"사이드바 claude={sidebar_claude} codex={sidebar_codex}")
    say(f"  claude={claude_bin or '못 찾음'}   codex={codex_bin or '못 찾음'}")
    say("\n[1] 설정 디렉토리")
    make_dirs(labs, dry)
    say("\n[2] PATH 실행 파일 (labN 런처 + codex/claude 래퍼)")
    wrappers = make_wrappers(labs, codex_bin, claude_bin, dry)
    say("\n[3] 원격 클라이언트 진입점 (~/.local/bin/codex)")
    install_dispatcher(sidebar_codex, dry)
    if not a.no_bashrc:
        say("\n[4] 셸 함수 (~/.bashrc)")
        patch_bashrc(labs, dry)
    if not a.no_vscode:
        say("\n[5] VSCode 사이드바 계정")
        patch_vscode(sidebar_claude, sidebar_codex, wrappers, dry)
    if not a.no_sender_config:
        say("\n[6] sender 수집 경로")
        patch_sender_config(labs, dry)

    say("\n" + "=" * 68)
    if warnings:
        say("확인 필요:")
        for w in warnings:
            say(f"  ⚠ {w}")
        say("")
    if dry:
        say(f"변경 예정 {len(changes)}건 — 실제 적용하려면 --dry-run 없이 실행하세요.")
        return 0
    say("남은 작업: 계정별 로그인 (대화형이라 직접 실행해야 합니다)")
    for lab in labs:
        say(f"\n  # {lab}")
        say(f"  CODEX_HOME=~/.codex-{lab} {codex_bin or 'codex'} login")
        say(f"  CLAUDE_CONFIG_DIR=~/.claude-{lab} {claude_bin or 'claude'} auth login")
    say("\n로그인 확인:")
    for lab in labs:
        say(f"  CODEX_HOME=~/.codex-{lab} {codex_bin or 'codex'} login status")
        say(f"  CLAUDE_CONFIG_DIR=~/.claude-{lab} {claude_bin or 'claude'} auth status")
    say("\n적용: 새 셸을 열거나 `source ~/.bashrc` → lab 함수 사용")
    say("      VSCode 는 창 리로드 후 사이드바가 지정한 계정으로 뜹니다")
    say("      sender 재시작: ./stop.sh && ./start.sh")
    return 0


if __name__ == "__main__":
    sys.exit(main())
