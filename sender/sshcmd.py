"""Non-interactive ssh/scp helpers (stdlib only).

Password auth is automated with a pseudo-terminal (the `pty` module) — we spawn
ssh/scp, watch its output, and type the password when it prompts. This is what
`sshpass` does, but without needing sshpass/paramiko installed (GPU servers
usually have neither, only the openssh client).

Key auth (transport.ssh_key set, no password) skips the pty and runs in
BatchMode. Host-key prompts are suppressed (StrictHostKeyChecking=no with a
repo-local known_hosts) so nothing ever blocks on a tty.
"""
from __future__ import annotations

import hashlib
import os
import pty
import select
import signal
import subprocess
import tempfile
import time

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
KNOWN_HOSTS = os.path.join(DATA_DIR, "known_hosts")


class SshError(RuntimeError):
    pass


# ssh appends ~17 chars to ControlPath while the socket is being set up, and
# sun_path caps the whole thing at 104 bytes — so the repo's data/ dir is often
# too long. Keep a margin for that suffix plus "/cm-" + 8 hex.
_CONTROL_BUDGET = 104 - 30


def _control_dir():
    """Short, private directory for ssh control sockets, or None to skip multiplexing.

    Must be owned by us and not group/world accessible — ssh refuses otherwise,
    and a predictable path in a shared /tmp is worth protecting anyway.
    """
    cands = []
    if os.environ.get("XDG_RUNTIME_DIR"):
        cands.append(os.environ["XDG_RUNTIME_DIR"])
    cands.append(os.path.join(tempfile.gettempdir(), f"aidas-ssh-{os.getuid()}"))
    for d in cands:
        if len(d) > _CONTROL_BUDGET:
            continue
        try:
            os.makedirs(d, mode=0o700, exist_ok=True)
            st = os.stat(d)
        except OSError:
            continue
        if st.st_uid == os.getuid() and not (st.st_mode & 0o077):
            return d
    return None


def _common_opts(cfg, for_scp=False):
    os.makedirs(DATA_DIR, exist_ok=True)
    port = str(cfg.get("ssh_port", 22))
    opts = [
        ("-P" if for_scp else "-p"), port,
        "-o", "StrictHostKeyChecking=no",
        "-o", f"UserKnownHostsFile={KNOWN_HOSTS}",
        "-o", "ConnectTimeout=20",
        "-o", "ServerAliveInterval=10",
        "-o", "LogLevel=ERROR",
    ]
    # Reuse one TCP/auth session across mkdir+scp+mv. Without this a single
    # delivery opened three connections, and flushing a backlog opened them in a
    # burst — the NAS then dropped some during key exchange
    # ("kex_exchange_identification: Connection reset by peer"), leaving a batch
    # uploaded but never renamed. Purely an optimisation: if no short enough
    # private directory exists we skip it rather than fail.
    cdir = _control_dir()
    if cdir:
        tag = hashlib.sha256(
            f"{cfg.get('ssh_user')}@{cfg.get('ssh_host')}:{port}".encode()).hexdigest()[:8]
        opts += ["-o", "ControlMaster=auto",
                 "-o", f"ControlPath={os.path.join(cdir, 'cm-' + tag)}",
                 "-o", "ControlPersist=30"]
    key = cfg.get("ssh_key")
    if key:
        opts += ["-i", os.path.expanduser(key), "-o", "BatchMode=yes",
                 "-o", "PreferredAuthentications=publickey"]
    else:
        opts += ["-o", "NumberOfPasswordPrompts=1",
                 "-o", "PubkeyAuthentication=no",
                 "-o", "PreferredAuthentications=password,keyboard-interactive"]
    return opts


def _run_with_password(argv, password, timeout=120):
    """Run argv in a pty, typing `password` at the first password prompt.

    Returns (exit_code, combined_output_str).
    """
    pid, fd = pty.fork()
    if pid == 0:  # child
        try:
            os.execvp(argv[0], argv)
        finally:
            os._exit(127)

    output = bytearray()
    recent = bytearray()
    pw_sent = 0
    status = None
    deadline = time.time() + timeout
    try:
        while True:
            left = deadline - time.time()
            if left <= 0:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
                return 124, bytes(output).decode("utf-8", "replace")
            r, _, _ = select.select([fd], [], [], min(1.0, left))
            if fd in r:
                try:
                    data = os.read(fd, 4096)
                except OSError:
                    data = b""
                if not data:
                    break
                output += data
                recent += data
                low = bytes(recent).lower()
                if pw_sent < 1 and (b"password:" in low or b"passphrase" in low):
                    os.write(fd, password.encode() + b"\n")
                    pw_sent += 1
                    recent.clear()
                if len(recent) > 256:
                    del recent[:-128]
            wpid, st = os.waitpid(pid, os.WNOHANG)
            if wpid == pid:
                status = st
                break
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    if status is None:
        _, status = os.waitpid(pid, 0)
    if hasattr(os, "waitstatus_to_exitcode"):
        code = os.waitstatus_to_exitcode(status)
    else:  # pragma: no cover
        code = (status >> 8) if os.WIFEXITED(status) else 128
    return code, bytes(output).decode("utf-8", "replace")


def _run_batch(argv, timeout=120):
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    except FileNotFoundError as e:
        raise SshError(f"{argv[0]} not found ({e})")


# transient SSH/network failures worth retrying within a cycle
_TRANSIENT = ("connection reset", "kex_exchange_identification", "connection refused",
              "connection closed", "connection timed out", "timed out",
              "broken pipe", "temporary failure", "no route to host", "reset by peer")
_BACKOFF = (2, 5, 10)

# Authentication failures must NOT be retried. ssh exits 255 for both network and
# auth errors, so treating 255 as transient burned three password attempts per
# call — and every cycle re-queued, so a wrong password or an IP-restricted
# account turned into a flood of failed logins. NAS login protection (Synology
# Auto Block) then blacklists the source address and the problem becomes
# permanent and self-inflicted. Fail fast instead and say what to do.
_AUTH_FAIL = ("permission denied", "authentication failed",
              "too many authentication failures", "no supported authentication",
              "account is locked", "access denied")


def _is_auth_failure(out):
    return any(t in (out or "").lower() for t in _AUTH_FAIL)


def _exec_once(cfg, argv):
    password = cfg.get("ssh_password")
    if password and not cfg.get("ssh_key"):
        return _run_with_password(argv, password)
    return _run_batch(argv)


def _exec(cfg, argv, retries=2):
    """Run an ssh/scp command, retrying transient connection errors with backoff."""
    code, out = 255, ""
    for attempt in range(retries + 1):
        code, out = _exec_once(cfg, argv)
        if code == 0:
            return code, out
        if _is_auth_failure(out):
            break                      # retrying only piles up failed logins
        low = (out or "").lower()
        transient = code == 255 or any(t in low for t in _TRANSIENT)
        if attempt < retries and transient:
            time.sleep(_BACKOFF[min(attempt, len(_BACKOFF) - 1)])
            continue
        break
    return code, out


_AUTH_HELP = (
    "인증 실패 — 자격증명이 거부됐습니다. 재시도하지 않았습니다"
    "(반복 시도는 NAS 로그인 차단만 유발합니다). 확인 순서:\n"
    "  1) 같은 자격증명으로 손으로 접속되는지:\n"
    "       ssh -p {port} {user}@{host} 'echo ok'\n"
    "     → 되는데 sender 만 실패하면 config.json 의 transport 값을 보세요.\n"
    "     → 손으로도 안 되면 비밀번호가 틀렸거나 NAS 가 이 서버 IP 를 막고 있습니다\n"
    "       (DSM > 제어판 > 보안 > 보호 > 허용/차단 목록에서 이 서버 IP 해제).\n"
    "  2) 이 서버에서 비밀번호 인증이 막혀 있으면 키 방식으로 전환하세요:\n"
    "       ./setup.sh --key ~/.ssh/id_ed25519 --host <서버이름>\n"
    "     (키가 없으면 ssh-keygen -t ed25519 후 공개키를 NAS 의\n"
    "      ~/.ssh/authorized_keys 에 등록 — DSM 파일 관리자로도 가능)")


def auth_help(cfg):
    return _AUTH_HELP.format(port=cfg.get("ssh_port", 22),
                             user=cfg.get("ssh_user", "?"),
                             host=cfg.get("ssh_host", "?"))


def ssh_exec(cfg, remote_cmd):
    """Run a shell command on the remote host."""
    target = f"{cfg['ssh_user']}@{cfg['ssh_host']}"
    argv = ["ssh"] + _common_opts(cfg) + [target, remote_cmd]
    code, out = _exec(cfg, argv)
    if code != 0:
        detail = out.strip()[:300]
        if _is_auth_failure(out):
            raise SshError(f"ssh '{remote_cmd}' failed: {detail}\n{auth_help(cfg)}")
        raise SshError(f"ssh '{remote_cmd}' failed (code {code}): {detail}")
    return out


def scp_put(cfg, local_path, remote_path):
    """Copy a local file to remote_path.

    Uses `scp -O` to force the legacy (shell-based) SCP protocol. Newer OpenSSH
    (9.0+) defaults to SFTP, and if the server's SFTP subsystem is chrooted to
    the user's home (common on Synology), an absolute dest path like
    /volume1/... resolves INSIDE the chroot and fails with
    "dest open ...: No such file or directory". The shell-based protocol respects
    real absolute paths (same context as our mkdir/mv). Falls back to plain scp
    on very old clients that lack -O.
    """
    target = f"{cfg['ssh_user']}@{cfg['ssh_host']}:{remote_path}"
    base = _common_opts(cfg, for_scp=True)
    code, out = _exec(cfg, ["scp", "-O"] + base + [local_path, target])
    if code != 0 and _opt_unsupported(out):
        code, out = _exec(cfg, ["scp"] + base + [local_path, target])
    if code != 0:
        detail = out.strip()[:300]
        if _is_auth_failure(out):
            raise SshError(f"scp failed: {detail}\n{auth_help(cfg)}")
        raise SshError(f"scp failed (code {code}): {detail}")
    return out


def scp_get(cfg, remote_path, local_path, recursive=False):
    """Copy remote_path down to local_path (mirror of scp_put).

    Used to pull the distribution from the NAS when updating a server: the
    credentials are already in config.json, so an upgrade needs no new secret
    and no second password prompt.
    """
    source = f"{cfg['ssh_user']}@{cfg['ssh_host']}:{remote_path}"
    base = _common_opts(cfg, for_scp=True)
    if recursive:
        base = ["-r"] + base
    code, out = _exec(cfg, ["scp", "-O"] + base + [source, local_path])
    if code != 0 and _opt_unsupported(out):
        code, out = _exec(cfg, ["scp"] + base + [source, local_path])
    if code != 0:
        detail = out.strip()[:300]
        if _is_auth_failure(out):
            raise SshError(f"scp failed: {detail}\n{auth_help(cfg)}")
        raise SshError(f"scp failed (code {code}): {detail}")
    return out


def _opt_unsupported(out):
    """True if scp rejected an option (so we should retry without -O).
    Transfer errors like 'No such file or directory' don't match → they surface.
    """
    low = (out or "").lower()
    return ("unknown option" in low or "illegal option" in low
            or "invalid option" in low or "usage:" in low)


def shquote(s):
    return "'" + str(s).replace("'", "'\\''") + "'"
