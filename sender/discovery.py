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
        if low in ("cli", "terminal", "tui", "shell") or "cli" in re.split(r"[_\-\s]", low):
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


def looks_like(provider, path):
    if not path or not os.path.isdir(path):
        return False
    if path.endswith(_NOT_A_DIR_SUFFIX):
        return False
    if any(os.path.exists(os.path.join(path, m)) for m in _MARKERS[provider]):
        return True
    return _sessions_shape(path) == provider


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
            result[provider] = [{"path": fallback, "source": "default",
                                 "display": fallback.replace(home, "~", 1)}]

    # A live process writing somewhere nobody collects is the exact failure this
    # module exists to surface.  It must be reported, not just fixed silently.
    collected = {d["path"] for p in PROVIDERS for d in result[p]}
    uncollected = [p for p in processes
                   if p["config_dir"] and p["config_dir"] not in collected]
    for proc in uncollected:
        warnings.append(
            f"pid {proc['pid']} ({proc['provider']}) 가 {proc['config_dir']} 에 쓰는데 "
            "수집 대상이 아닙니다")

    return {"dirs": result, "processes": processes,
            "uncollected": uncollected, "warnings": warnings}
