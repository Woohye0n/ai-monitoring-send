"""Claude Code collector used by the NAS sender.

The Claude JSONL transcript format has token usage but no account identity.
This collector therefore treats account attribution as a provenance problem:
only bytes appended after a file cursor has been observed under the same
account are assigned an email.  Bootstrap history and the byte range spanning
an account switch are deliberately emitted as ``assumed`` with no email, so a
later login cannot silently relabel a personal session as a monitored account.
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import time
from datetime import datetime

try:
    from . import claude_usage, discovery
except ImportError:  # pragma: no cover - allow running as a loose script
    import claude_usage
    import discovery


def iso_to_ms(ts):
    if not ts:
        return None
    try:
        return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000)
    except (ValueError, TypeError):
        return None


def _timestamp_to_ms(value):
    """Normalize Claude's ISO timestamps and session epoch timestamps."""
    if isinstance(value, (int, float)):
        return int(value * 1000) if value and value < 10_000_000_000 else int(value)
    return iso_to_ms(value)


def project_name(cwd, fallback_dir=""):
    if cwd:
        base = os.path.basename(cwd.rstrip("/"))
        if base:
            return base
    return (fallback_dir or "").split("-")[-1] or fallback_dir


def pid_alive(pid, started_at=None):
    """Best-effort local Claude process liveness without trusting a stale PID.

    Linux PID values can be reused.  When ``/proc`` is available, reject a PID
    that now belongs to an unrelated command or began well after the session
    record.  Other platforms retain the conservative ``os.kill`` check.
    """
    if not pid:
        return None
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, ValueError):
        return None

    proc = f"/proc/{int(pid)}"
    try:
        with open(os.path.join(proc, "cmdline"), "rb") as f:
            cmdline = f.read().replace(b"\0", b" ").decode("utf-8", "replace").lower()
        with open(os.path.join(proc, "stat"), "r", encoding="utf-8") as f:
            stat_tail = f.read().rsplit(")", 1)[1].split()
        if stat_tail and stat_tail[0] == "Z":
            return False
        # Claude Code may be the direct executable or Node with an Anthropic /
        # Claude install path.  An unrelated process with a reused PID is not
        # a live Claude session.
        if cmdline and not any(marker in cmdline for marker in ("claude", "anthropic")):
            return False

        expected_start = _timestamp_to_ms(started_at)
        if expected_start and len(stat_tail) > 19:
            with open("/proc/stat", "r", encoding="utf-8") as f:
                boot = next((int(line.split()[1]) for line in f
                             if line.startswith("btime ")), None)
            ticks = int(stat_tail[19])  # proc stat field 22: starttime
            if boot is not None:
                actual_start = int((boot + ticks / os.sysconf("SC_CLK_TCK")) * 1000)
                # Leave a small grace interval for timestamp rounding/startup.
                if actual_start > expected_start + 5 * 60 * 1000:
                    return False
    except (OSError, StopIteration, ValueError, IndexError):
        # ``os.kill`` already proved the process exists.  If procfs cannot be
        # inspected, do not manufacture a stronger claim than that.
        pass
    return True


class ClaudeCollector:
    """Stateful reader for one Claude configuration directory.

    ``file_state`` maps transcript paths to cursor records.  Legacy
    ``(size, mtime)`` values are accepted during upgrade, but intentionally
    treated as an untrusted bootstrap read once instead of retroactively
    attaching them to today's account.
    """

    def __init__(self, config_dir, host=None, file_state=None,
                 usage_enabled=True, usage_interval=300, bootstrap_days=2,
                 usage_cache=None):
        self.config_dir = os.path.expanduser(config_dir)
        self.host = host or socket.gethostname()
        self.file_state = dict(file_state or {})
        self.usage_enabled = usage_enabled
        self.usage_interval = max(180, usage_interval)  # endpoint 429s under ~180s
        # How far back a *newly discovered* transcript is still worth reading.
        # Discovery can hand this collector a directory with months of history;
        # every record in it would be emitted as ``assumed`` (its account cannot
        # be proven) and dropped centrally, so shipping it only costs bandwidth.
        self.bootstrap_days = max(0, bootstrap_days or 0)
        self._last_account_identity = None
        # sessionId -> the tool's own entrypoint string.  Session files carry it
        # for live sessions and most transcript lines repeat it, but a line
        # written by an older build may not -- remembering it per session keeps
        # a terminal turn from being reported as surface "unknown".
        self._entrypoint_by_session = {}
        self._last_poll_ms = 0
        self._account_file_mtime_ms = 0
        # Cache utilization by account and token fingerprint.  A login switch
        # must never put account A's percentage on account B's dashboard card.
        # Shared across collectors when several directories hold the same login,
        # so discovering more directories cannot multiply calls to an endpoint
        # that 429s easily.
        self._usage_cache = usage_cache if usage_cache is not None else {}

    def _version(self):
        root = os.path.join(self.config_dir, "sessions")
        try:
            files = [os.path.join(root, f) for f in os.listdir(root) if f.endswith(".json")]
            with open(max(files, key=os.path.getmtime), "r", encoding="utf-8") as f:
                return json.load(f).get("version") or claude_usage.DEFAULT_VERSION
        except (OSError, ValueError):
            return claude_usage.DEFAULT_VERSION

    @staticmethod
    def _account_identity(account):
        if not account or not account.get("email"):
            return None
        return (account.get("account_id") or "", account["email"])

    def _maybe_fetch_usage(self, account):
        """Return the cached usage entry for exactly ``account`` (or None).

        Entry keys: ``rate_limits`` + ``fetched_at_ms`` (last SUCCESSFUL
        fetch), and ``status`` + ``status_at`` (result of the last attempt:
        "ok", "unauthorized", "network", ...). Failed requests are throttled
        too, but a cached successful response is only reused for the same
        account and same OAuth token fingerprint.
        """
        if not self.usage_enabled or not account or not account.get("email"):
            return None
        token = claude_usage.read_access_token(self.config_dir)
        if not token:
            return None

        now = time.time()
        fingerprint = hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]
        key = (*self._account_identity(account), fingerprint)
        cached = self._usage_cache.get(key)
        if cached and (now - cached["attempted_at"]) < self.usage_interval:
            return cached

        result, error = claude_usage.fetch_usage(token, version=self._version())
        entry = {
            "attempted_at": now,
            "rate_limits": cached.get("rate_limits") if cached else None,
            "fetched_at_ms": cached.get("fetched_at_ms") if cached else None,
            "status": cached.get("status") if cached else None,
            "status_at": cached.get("status_at") if cached else None,
        }
        if result is not None:
            entry.update(rate_limits=result, fetched_at_ms=int(now * 1000),
                         status="ok", status_at=int(now * 1000))
        elif error != "rate_limited":
            # 429 says nothing about the account. Anything else — especially
            # "unauthorized" (revoked token / suspended account) — is a real
            # signal the dashboard should show instead of a stale percentage.
            entry.update(status=error, status_at=int(now * 1000))
        self._usage_cache[key] = entry
        return entry

    # -- account ---------------------------------------------------------
    def _account_file_candidates(self):
        """Return only account metadata valid for this config layout.

        The default ``~/.claude`` layout stores identity in the legacy sibling
        ``~/.claude.json``.  A custom ``CLAUDE_CONFIG_DIR`` must have its own
        ``<config_dir>/.claude.json``; falling back to the default file for a
        custom directory is precisely what causes cross-account attribution.
        """
        config_dir = os.path.normcase(os.path.abspath(self.config_dir))
        default_dir = os.path.normcase(os.path.abspath(os.path.expanduser("~/.claude")))
        candidates = [os.path.join(config_dir, ".claude.json")]
        if config_dir == default_dir:
            candidates.append(os.path.join(os.path.dirname(config_dir), ".claude.json"))
        return list(dict.fromkeys(candidates))

    def read_account(self):
        self._account_file_mtime_ms = 0
        for path in self._account_file_candidates():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, ValueError):
                continue
            # A custom directory can contain settings in .claude.json without
            # oauthAccount.  It is not identity metadata, so keep looking.
            acct = data.get("oauthAccount") or {}
            email = acct.get("emailAddress")
            if not email:
                continue
            try:
                self._account_file_mtime_ms = int(os.path.getmtime(path) * 1000)
            except OSError:
                self._account_file_mtime_ms = 0
            return {
                "provider": "claude",
                "email": email,
                "account_id": acct.get("accountUuid"),
                "org_type": acct.get("organizationType"),
                "rate_limit_tier": acct.get("organizationRateLimitTier"),
                "display_name": acct.get("displayName"),
                "org_name": acct.get("organizationName"),
            }
        return None

    # -- usage -----------------------------------------------------------
    def _iter_jsonl(self):
        root = os.path.join(self.config_dir, "projects")
        if not os.path.isdir(root):
            return
        for proj in os.listdir(root):
            full = os.path.join(root, proj)
            if not os.path.isdir(full):
                continue
            for fn in os.listdir(full):
                if fn.endswith(".jsonl"):
                    yield proj, os.path.join(full, fn)

    def _parse_file(self, proj, path, email, assumed, start_offset=0):
        """Parse complete JSONL records from a byte cursor.

        The cursor stays before a partial final line so a Claude writer racing
        this poll cannot cause the first half of a message to be lost.
        """
        out = []
        next_offset = max(0, int(start_offset or 0))
        try:
            with open(path, "rb") as f:
                f.seek(next_offset)
                while True:
                    raw = f.readline()
                    if not raw:
                        break
                    line_end = f.tell()
                    complete = raw.endswith(b"\n")
                    try:
                        line = raw.decode("utf-8").strip()
                    except UnicodeDecodeError:
                        if not complete:
                            break
                        next_offset = line_end
                        continue
                    if not line:
                        next_offset = line_end
                        continue
                    try:
                        d = json.loads(line)
                    except ValueError:
                        # An invalid complete line cannot become valid.  An
                        # unterminated final line might, so retry it next poll.
                        if not complete:
                            break
                        next_offset = line_end
                        continue
                    next_offset = line_end
                    if d.get("type") != "assistant":
                        continue
                    msg = d.get("message") or {}
                    usage = msg.get("usage")
                    uuid = d.get("uuid")
                    if not usage or not uuid:
                        continue
                    cwd = d.get("cwd")
                    session_id = d.get("sessionId")
                    # Which surface produced this turn.  Claude writes it on the
                    # transcript line itself ("cli" for a terminal, "vscode" for
                    # the sidebar); the sender used to drop it, which is why a
                    # terminal user and a sidebar user were indistinguishable.
                    entrypoint = d.get("entrypoint")
                    if entrypoint and session_id:
                        self._entrypoint_by_session[session_id] = entrypoint
                    elif session_id:
                        entrypoint = self._entrypoint_by_session.get(session_id)
                    out.append({
                        "uuid": uuid,
                        "provider": "claude",
                        "session_id": session_id,
                        "project": project_name(cwd, proj),
                        "cwd": cwd,
                        "git_branch": d.get("gitBranch"),
                        "model": msg.get("model"),
                        "ts": iso_to_ms(d.get("timestamp")),
                        "input_tokens": usage.get("input_tokens", 0) or 0,
                        "output_tokens": usage.get("output_tokens", 0) or 0,
                        "cache_creation_tokens": usage.get("cache_creation_input_tokens", 0) or 0,
                        "cache_read_tokens": usage.get("cache_read_input_tokens", 0) or 0,
                        "service_tier": usage.get("service_tier"),
                        "request_id": d.get("requestId"),
                        "version": d.get("version"),
                        "entrypoint": entrypoint,
                        "surface": discovery.normalize_surface(entrypoint),
                        "user_type": d.get("userType"),
                        "account_email": email,
                        "assumed": assumed,
                    })
        except OSError:
            pass
        return out, next_offset

    @staticmethod
    def _state_parts(value):
        """Normalize cursor records and legacy ``[size, mtime]`` state."""
        if isinstance(value, dict):
            return {
                "size": value.get("size"),
                "mtime": value.get("mtime"),
                "offset": value.get("offset"),
                "account_email": value.get("account_email"),
                "account_id": value.get("account_id"),
                "inode": value.get("inode"),
            }
        if isinstance(value, (list, tuple)) and len(value) >= 2:
            return {
                "size": value[0], "mtime": value[1], "offset": None,
                "account_email": None, "account_id": None, "inode": None,
            }
        return None

    @staticmethod
    def _state_for(st, offset, account):
        return {
            "size": st.st_size,
            "mtime": st.st_mtime,
            "offset": max(0, int(offset or 0)),
            "account_email": account.get("email") if account else None,
            "account_id": account.get("account_id") if account else None,
            "inode": getattr(st, "st_ino", None),
        }

    @staticmethod
    def _valid_offset(prev, st):
        if not prev:
            return None
        offset = prev.get("offset")
        same_inode = prev.get("inode") in (None, getattr(st, "st_ino", None))
        try:
            offset = int(offset)
        except (TypeError, ValueError):
            return None
        return offset if same_inode and 0 <= offset <= st.st_size else None

    def _usage(self, account, account_stable: bool = False):
        """``account_stable`` = the previous poll saw this exact account, which
        is what lets a brand-new file be attributed instead of discarded."""
        records, updated = [], {}
        cutoff_ms = (int(time.time() * 1000) - self.bootstrap_days * 86400 * 1000
                     if self.bootstrap_days else 0)
        account_email = account.get("email") if account else None
        account_identity = self._account_identity(account)
        for proj, path in self._iter_jsonl():
            try:
                st = os.stat(path)
            except OSError:
                continue
            prev = self._state_parts(self.file_state.get(path))
            offset = self._valid_offset(prev, st)
            unchanged = (
                prev and offset is not None and prev.get("size") == st.st_size
                and prev.get("mtime") == st.st_mtime and offset == st.st_size
            )
            if unchanged:
                continue

            if prev is None and cutoff_ms and int(st.st_mtime * 1000) < cutoff_ms:
                # First sight of a transcript last written before we started
                # watching it.  Record the cursor at EOF so anything appended
                # from now on is attributable, and skip the unattributable
                # backlog instead of uploading it to be discarded.
                cursor = self._state_for(st, st.st_size, account)
                self.file_state[path] = cursor
                updated[path] = cursor
                continue

            prior_identity = None
            if prev and prev.get("account_email"):
                prior_identity = (prev.get("account_id") or "", prev["account_email"])
            # New/legacy files, truncation/replacement, logged-out reads, and
            # account boundaries have no provable account for the bytes parsed
            # in this poll.  They are intentionally not relabelled.
            # Exception: a file we have never seen, written entirely since the
            # previous poll, while the account stayed the same across both
            # polls — no other account could have produced it.  Without this a
            # short session (one-shot `claude -p`) is always discarded.
            born_since_last_poll = (
                prev is None and account_stable and self._last_poll_ms
                and int(st.st_mtime * 1000) > self._last_poll_ms
            )
            if not account_identity:
                assumed = True
            elif born_since_last_poll:
                assumed = False
            else:
                assumed = (offset is None or prior_identity != account_identity)
            record_email = account_email if not assumed else None
            parsed, next_offset = self._parse_file(
                proj, path, record_email, assumed, offset or 0)
            records.extend(parsed)
            cursor = self._state_for(st, next_offset, account)
            self.file_state[path] = cursor
            updated[path] = cursor
        return records, updated

    # -- sessions --------------------------------------------------------
    def _sessions(self, email, account_stable):
        root = os.path.join(self.config_dir, "sessions")
        out = []
        if not os.path.isdir(root):
            return out
        for fn in os.listdir(root):
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(root, fn), "r", encoding="utf-8") as f:
                    s = json.load(f)
            except (OSError, ValueError):
                continue
            cwd = s.get("cwd")
            pid = s.get("pid")
            started_at = s.get("startedAt")
            entrypoint = s.get("entrypoint")
            if entrypoint and s.get("sessionId"):
                self._entrypoint_by_session[s["sessionId"]] = entrypoint
            alive = pid_alive(pid, started_at)
            # A one-poll debounce prevents a session that predates a login
            # change from being claimed by the new account.
            session_email = email if account_stable else None
            if (session_email and self._account_file_mtime_ms and
                    (_timestamp_to_ms(started_at) or 0) < self._account_file_mtime_ms):
                session_email = None
            out.append({
                "session_id": s.get("sessionId"),
                "provider": "claude",
                "cwd": cwd,
                "project": project_name(cwd),
                "version": s.get("version"),
                "kind": s.get("kind"),
                "entrypoint": entrypoint,
                "surface": discovery.normalize_surface(entrypoint, s.get("kind")),
                "status": s.get("status"),
                "pid": pid,
                "pid_alive": (None if alive is None else (1 if alive else 0)),
                "started_at": started_at,
                "updated_at": s.get("updatedAt"),
                "account_email": session_email,
            })
        return out

    def collect(self):
        account = self.read_account()
        email = account["email"] if account else None
        if account:
            usage = self._maybe_fetch_usage(account)
            if usage:
                if usage.get("rate_limits"):
                    account["rate_limits"] = usage["rate_limits"]
                    account["rate_limits_updated_at"] = usage.get("fetched_at_ms")
                if usage.get("status"):
                    account["usage_status"] = usage["status"]
                    account["usage_status_at"] = usage.get("status_at")

        identity = self._account_identity(account)
        switched = bool(self._last_account_identity and identity and
                        identity != self._last_account_identity)
        switched_from = (self._last_account_identity[1]
                         if switched and self._last_account_identity else None)
        account_stable = bool(identity and identity == self._last_account_identity)
        # Sessions first: they carry ``entrypoint`` for every live session, so
        # the cache is warm before the transcripts are parsed.
        sessions = self._sessions(email, account_stable)
        usage, updated = self._usage(account, account_stable)
        self._last_account_identity = identity
        self._last_poll_ms = int(time.time() * 1000)
        return {
            "host": self.host,
            "account": account,
            "switched": switched,
            "switched_from": switched_from,
            "usage": usage,
            "sessions": sessions,
            "file_state": updated,
        }
