"""Find every place this user's Claude/Codex sessions are actually written.

The sender used to collect only what ``config.json`` named, with a
``~/.claude*`` glob as the fallback.  Both assumptions break in practice:

  * ``setup-accounts.py`` rewrites ``config_dirs`` to the lab directories
    *only*.  Anything that escapes the launcher -- a VSCode sidebar whose
    extension update dropped the wrapper, a tmux pane opened before setup, a
    plain ``claude`` -- is then not collected at all, and silence on the
    dashboard is indistinguishable from "this person did not work today".
  * A config dir need not live under ``$HOME``.  On our own A100 one user runs
    with ``CLAUDE_CONFIG_DIR=/mnt/data/<user>/.claude-<user>`` and
    ``CODEX_HOME=/mnt/nvme1/<user>/.codex-<user>``.  The glob finds neither, so
    that node reported an account with zero usage for weeks while the person
    used both tools daily.

Running processes are the ground truth: a tool writes where the environment it
was started with says, and ``/proc/<pid>/environ`` of our *own* processes is
readable (other users' is not, which is exactly the boundary we want).
Discovery therefore unions three sources -- live processes, filesystem globs
and the configured list -- and never lets the configured list hide a directory
that is demonstrably in use.
"""
from __future__ import annotations

import glob
import os
import re
import subprocess

CLAUDE, CODEX = "claude", "codex"
PROVIDERS = (CLAUDE, CODEX)

# Normalized surfaces.  The point of this vocabulary is that a terminal user and
# a VSCode sidebar user must be separable on the dashboard; both tools already
# record which one they are, the sender just never passed it on.
TERMINAL, VSCODE, DESKTOP, SDK, WEB, UNKNOWN = (
    "terminal", "vscode", "desktop", "sdk", "web", "unknown")

# Files/dirs that prove a path is *that* tool's config home.  Nothing here may
# appear in both layouts: a path is now reached by evidence (a process, a
# command line) rather than by its name, so "has sessions/" -- which both tools
# have -- would file every Claude directory as a Codex one as well.
_MARKERS = {
    CLAUDE: ("projects", ".claude.json", "shell-snapshots", "statsig", "todos"),
    CODEX: ("auth.json", "archived_sessions", "session_index.jsonl", "config.toml"),
}
_ENV_KEY = {CLAUDE: "CLAUDE_CONFIG_DIR", CODEX: "CODEX_HOME"}
_DEFAULT_BASENAME = {CLAUDE: ".claude", CODEX: ".codex"}
_GLOBS = {CLAUDE: (".claude", ".claude*"), CODEX: (".codex", ".codex*")}

# A config dir embedded in a command line, e.g.
#   /home/x/.local/bin/claude daemon run --json-path /mnt/data/x/.claude-x/daemon.json
# Used when a process has no explicit env var but still names its dir in argv.
_DIR_IN_ARGV = re.compile(r"(/[^\s:\"']*/\.(?:claude|codex)[A-Za-z0-9._-]*)")

# Paths under a config dir that are not themselves config dirs.
_NOT_A_DIR_SUFFIX = (".lock", ".bak", ".json", ".log", ".tmp", ".real")


# ----------------------------------------------------------------- surfaces
_TERMINAL_WORDS = frozenset({"cli", "terminal", "tui", "shell"})


def normalize_surface(*values):
    """Map a tool's own entrypoint/originator strings onto a shared vocabulary.

    Unknown values are passed through as ``other:<raw>`` instead of being
    guessed at.  A surface we have not seen before should appear on the
    dashboard under its own name, not be silently folded into "terminal".
    """
    for raw in values:
        # Codex puts a dict in ``source`` for subagent-spawned threads
        # ({"subagent": {...}}).  That says nothing about the surface, so fall
        # through to the next candidate instead of stringifying a payload.
        if not isinstance(raw, str):
            continue
        s = raw.strip()
        if not s:
            continue
        low = s.lower()
        if "vscode" in low or "vs_code" in low or low in ("ide", "extension", "editor"):
            return VSCODE
        if "desktop" in low or low == "app":
            return DESKTOP
        # Match on the tokens, not the whole string: Codex reports its terminal
        # UI as "codex-tui", which an equality test against "tui" misses and
        # which then shows up on the dashboard as a bogus "other:" surface.
        parts = set(re.split(r"[_\-\s]", low))
        if low in _TERMINAL_WORDS or parts & _TERMINAL_WORDS:
            return TERMINAL
        if low.startswith("sdk") or low in ("mcp", "api", "agent"):
            return SDK
        if "web" in low or "browser" in low:
            return WEB
        return f"other:{s}"
    return UNKNOWN


# ------------------------------------------------------------------ procfs
def _own_pids():
    uid = os.getuid()
    try:
        entries = os.listdir("/proc")
    except OSError:
        return
    for name in entries:
        if not name.isdigit():
            continue
        try:
            if os.stat(os.path.join("/proc", name)).st_uid != uid:
                continue
        except OSError:
            continue
        yield int(name)


def _read_cmdline(pid):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            raw = fh.read()
    except OSError:
        return None
    return raw.replace(b"\0", b" ").decode("utf-8", "replace").strip()


def _read_environ(pid):
    try:
        with open(f"/proc/{pid}/environ", "rb") as fh:
            raw = fh.read()
    except OSError:
        return {}
    env = {}
    for chunk in raw.split(b"\0"):
        if b"=" in chunk:
            k, v = chunk.split(b"=", 1)
            env[k.decode("utf-8", "replace")] = v.decode("utf-8", "replace")
    return env


def _read_cwd(pid):
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return None


def provider_of(cmdline):
    """Which tool a command line belongs to, or None.

    Matching has to be loose: the same tool shows up as a bare ``codex``, as an
    extension-bundled ``.../bin/linux-x86_64/codex``, as ``claude bg-pty-host``
    and as a versioned binary under ``.local/share/claude``.  Only argv[0] is
    inspected for path matches so that a shell whose *arguments* merely mention
    a transcript path is not mistaken for the tool itself.
    """
    if not cmdline:
        return None
    argv0 = cmdline.split(" ", 1)[0]
    base = os.path.basename(argv0).lower()
    low = argv0.lower()
    if base.startswith("codex") or "/codex" in low:
        return CODEX
    if base.startswith("claude") or "/claude" in low or "/anthropic" in low:
        return CLAUDE
    return None


def _has_tty(pid):
    """Whether the process has a controlling terminal (stat field 7, tty_nr)."""
    try:
        with open(f"/proc/{pid}/stat", "r", encoding="utf-8") as fh:
            fields = fh.read().rsplit(")", 1)[1].split()
        return int(fields[4]) != 0      # 0-based after the comm field
    except (OSError, IndexError, ValueError):
        return None


def _surface_hint(pid, env, cmdline):
    """Best guess from the process alone (diagnostics only).

    The authoritative surface comes from the transcript the tool writes, which
    is why this never reaches a usage record.  It only has to be good enough
    for the coverage report, where a process may not have produced a turn yet.

    A controlling terminal is checked first on purpose: ``TERM_PROGRAM=vscode``
    is also set in VSCode's *integrated terminal*, where the user is a terminal
    user.  The sidebar's extension host has no tty.
    """
    if _has_tty(pid):
        return TERMINAL
    if env.get("TERM_PROGRAM", "").lower() == "vscode" or any(
            k.startswith("VSCODE_") for k in env):
        return VSCODE
    if "/.vscode" in (cmdline or "") or "vscode-server" in (cmdline or ""):
        return VSCODE
    if env.get("TMUX") or env.get("STY") or env.get("SSH_TTY"):
        return TERMINAL
    return UNKNOWN


def iter_processes():
    """Live Claude/Codex processes owned by this user, plus directory hints.

    Returns ``(processes, hinted_dirs)``.  Each process entry says where that
    process writes, which is what turns "the dashboard shows nothing" into
    "this pid writes to a directory nobody collects".

    ``hinted_dirs`` comes from *any* of this user's command lines that mention
    a ``.claude*``/``.codex*`` path -- a shell running a Claude shell-snapshot,
    a ``#!/bin/sh`` launcher whose argv[0] is the interpreter.  Those are not
    the tool, so they are not reported as processes, but the path they name is
    still evidence of a config dir that no glob would find.  Every hint is
    validated against the on-disk markers before it is used.
    """
    out = []
    hints = set()
    seen_procfs = False
    for pid in _own_pids():
        seen_procfs = True
        cmdline = _read_cmdline(pid)
        if cmdline:
            hints.update(_DIR_IN_ARGV.findall(cmdline))
        provider = provider_of(cmdline)
        if not provider:
            continue
        env = _read_environ(pid)
        home = env.get("HOME") or os.path.expanduser("~")
        explicit = (env.get(_ENV_KEY[provider]) or "").strip()
        config_dir = explicit or os.path.join(home, _DEFAULT_BASENAME[provider])
        out.append({
            "pid": pid,
            "provider": provider,
            "config_dir": os.path.realpath(os.path.expanduser(config_dir)),
            "config_dir_source": "env" if explicit else "default",
            "home": home,
            "surface_hint": _surface_hint(pid, env, cmdline),
            "cmd": (cmdline or "")[:160],
            "cwd": _read_cwd(pid),
            "argv_dirs": sorted({os.path.realpath(m)
                                 for m in _DIR_IN_ARGV.findall(cmdline or "")}),
        })
    if not seen_procfs:
        procs, ps_hints = _iter_processes_ps()
        out.extend(procs)
        hints.update(ps_hints)
    return out, {os.path.realpath(h) for h in hints}


def _iter_processes_ps():
    """Fallback for systems without procfs (macOS).

    No environment is available there, so only the directories named in argv
    can be recovered -- still enough to notice a non-default config dir.
    """
    try:
        raw = subprocess.run(["ps", "-u", str(os.getuid()), "-o", "pid=,args="],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return [], set()
    out, hints = [], set()
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        pid, _, cmdline = line.partition(" ")
        hints.update(_DIR_IN_ARGV.findall(cmdline))
        provider = provider_of(cmdline.strip())
        if not provider or not pid.isdigit():
            continue
        out.append({
            "pid": int(pid), "provider": provider, "config_dir": None,
            "config_dir_source": "unknown", "home": None,
            "surface_hint": _surface_hint(int(pid), {}, cmdline),
            "cmd": cmdline.strip()[:160], "cwd": None,
            "argv_dirs": sorted({os.path.realpath(m)
                                 for m in _DIR_IN_ARGV.findall(cmdline)}),
        })
    return out, hints


# ------------------------------------------------------------------- dirs
def _sessions_shape(path):
    """Which tool owns a ``sessions/`` directory, by what is inside it.

    Claude writes ``sessions/<pid>.json``; Codex writes
    ``sessions/YYYY/MM/DD/rollout-*.jsonl``.  This is the tiebreaker for a
    freshly created directory that has no other marker yet.
    """
    try:
        names = os.listdir(os.path.join(path, "sessions"))[:50]
    except OSError:
        return None
    if any(n.endswith(".json") for n in names):
        return CLAUDE
    if any(len(n) == 4 and n.isdigit() for n in names):      # year directory
        return CODEX
    return None


def reject_reason(provider, path):
    """왜 이 경로를 설정 디렉토리로 인정하지 않는지. 인정하면 None.

    권한이 없어 안을 못 보는 경우와 그냥 다른 디렉토리인 경우는 증상이 같다 —
    마커 파일이 하나도 '존재하지 않는 것처럼' 보인다. 실제로 kakao-b200-3 에서
    claude 프로세스 10개가 /home/jovyan/.claude 에 쓰는데 수집은 0건이었고,
    경고는 "수집 대상이 아닙니다" 라고만 해서 원인을 알 수 없었다. 같은 사용자가
    같은 경로를 쓰는 다른 세 노드는 멀쩡했으니 디렉토리 모양 문제가 아니었다.
    """
    if not path:
        return "경로 없음"
    if path.endswith(_NOT_A_DIR_SUFFIX):
        return "설정 디렉토리가 아닌 파일"
    if not os.path.isdir(path):
        return "디렉토리가 없음"
    # 안을 들여다볼 수 있는지 먼저 확인한다. 못 보면 마커 판정은 의미가 없다.
    try:
        os.listdir(path)
    except PermissionError:
        return "읽기 권한 없음"
    except OSError as e:
        return f"읽을 수 없음({e.errno})"
    if any(os.path.exists(os.path.join(path, m)) for m in _MARKERS[provider]):
        return None
    if _sessions_shape(path) == provider:
        return None
    return f"{provider} 설정 디렉토리의 흔적이 없음"


def looks_like(provider, path):
    return reject_reason(provider, path) is None


def _glob_dirs(provider, roots):
    found = []
    for root in roots:
        for pattern in _GLOBS[provider]:
            for path in glob.glob(os.path.join(os.path.expanduser(root), pattern)):
                if looks_like(provider, path):
                    found.append(path)
    return found


def discover(cfg, home=None):
    """Return {provider: [dir record]} plus the process inventory and warnings.

    ``source`` records how each directory was found, so the coverage report can
    say *why* something is collected -- and ``config.json`` naming a directory
    that no longer exists becomes a warning instead of a silent gap.
    """
    home = home or os.path.expanduser("~")
    claude_cfg = cfg.get("claude") or {}
    codex_cfg = cfg.get("codex") or {}
    configured = {
        CLAUDE: list(claude_cfg.get("config_dirs") or []),
        CODEX: list(codex_cfg.get("dirs") or []),
    }
    enabled = {CLAUDE: claude_cfg.get("enabled", True),
               CODEX: codex_cfg.get("enabled", True)}
    discover_on = bool(cfg.get("discover", True))
    extra_roots = [home] + [os.path.expanduser(r) for r in (cfg.get("extra_roots") or [])]
    excluded = {os.path.realpath(os.path.expanduser(p))
                for p in (cfg.get("exclude_dirs") or [])}

    processes, hinted = iter_processes() if discover_on else ([], set())
    warnings = []

    # 걷을 디렉토리를 적어 두고 수집은 꺼 둔 설정. 둘 중 하나는 실수다.
    # kakao-b200-3 이 정확히 이 상태로 몇 주를 돌았고, claude 사용량이 통째로
    # 빠지는 동안 아무것도 그것을 말해 주지 않았다.
    for provider in PROVIDERS:
        if not enabled[provider] and configured[provider]:
            warnings.append(
                f"config.json: {provider} 수집이 꺼져 있는데 "
                f"config_dirs 에 {len(configured[provider])}개가 적혀 있습니다 "
                f"({', '.join(configured[provider][:3])}) — 둘 중 하나는 의도가 아닐 수 있습니다")
    result = {CLAUDE: [], CODEX: []}

    for provider in PROVIDERS:
        if not enabled[provider]:
            continue
        # source -> ordered candidates.  Configured first so an operator's
        # explicit choice keeps its label when several sources agree.
        candidates = []
        for raw in configured[provider]:
            path = os.path.realpath(os.path.expanduser(raw))
            if looks_like(provider, path):
                candidates.append((path, "config"))
            elif os.path.isdir(path):
                candidates.append((path, "config"))   # exists but still empty
            elif path != os.path.join(home, _DEFAULT_BASENAME[provider]):
                # The default path is in every config file whether or not the
                # tool is installed; only a path someone deliberately typed is
                # worth a warning.  Warning on the default would fire on every
                # machine without Codex, every cycle, and teach people to
                # ignore the line that matters.
                warnings.append(
                    f"config.json 의 {provider} 경로 {raw} 가 없습니다 — 오타이거나 "
                    "아직 만들어지지 않았습니다")
        if discover_on:
            for proc in processes:
                if proc["provider"] != provider:
                    continue
                for path in filter(None, [proc["config_dir"]] + proc["argv_dirs"]):
                    if looks_like(provider, path):
                        candidates.append((path, "process"))
            for path in hinted:
                if looks_like(provider, path):
                    candidates.append((path, "process"))
            for path in _glob_dirs(provider, extra_roots):
                candidates.append((path, "glob"))

        seen = {}
        for path, source in candidates:
            real = os.path.realpath(path)
            if real in excluded or real in seen:
                continue
            seen[real] = {"path": real, "source": source,
                          "display": real.replace(home, "~", 1)}
        result[provider] = list(seen.values())

        if not result[provider] and enabled[provider]:
            fallback = os.path.join(home, _DEFAULT_BASENAME[provider])
            # 폴백이 exclude_dirs 를 무시하면 안 된다. 제외한 경로가 그 provider 의
            # 유일한 후보일 때 기본값으로 도로 들어와, 설정이 아무 일도 하지 않은
            # 것처럼 보였다 — 끄려고 적은 사람 입장에서는 조용한 배신이다.
            if os.path.realpath(fallback) not in excluded:
                result[provider] = [{"path": fallback, "source": "default",
                                     "display": fallback.replace(home, "~", 1)}]

    # A live process writing somewhere nobody collects is the exact failure this
    # module exists to surface.  It must be reported, not just fixed silently.
    collected = {d["path"] for p in PROVIDERS for d in result[p]}
    uncollected = [p for p in processes
                   if p["config_dir"] and p["config_dir"] not in collected]
    for proc in uncollected:
        prov, path = proc["provider"], proc["config_dir"]
        # 설정으로 꺼 둔 것과 고장난 것은 전혀 다른 일인데, 예전에는 똑같이
        # "수집 대상이 아닙니다" 로만 보였다. kakao-b200-3 의 claude 사용량이
        # 통째로 빠지는데도 디렉토리는 멀쩡해서 원인을 좁힐 수 없었다.
        if not enabled[prov]:
            why = f"config.json 에서 {prov} 수집이 꺼져 있음 (\"{prov}\": {{\"enabled\": false}})"
        elif os.path.realpath(path) in excluded:
            why = "config.json 의 exclude_dirs 에 들어 있음"
        else:
            why = reject_reason(prov, path) or "원인 불명"
        warnings.append(
            f"pid {proc['pid']} ({prov}) 가 {path} 에 쓰는데 수집하지 못합니다 — {why}")

    return {"dirs": result, "processes": processes,
            "uncollected": uncollected, "warnings": warnings}
