"""Codex (OpenAI) collector (sender side).

Reads a Codex config dir (default ~/.codex) and produces a partial CollectResult
tagged provider="codex", so it merges with Claude data on the dashboard.

On-disk format (Codex CLI):
  ~/.codex/auth.json                          -> tokens.id_token (JWT w/ email+plan)
  ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl -> session transcript:
     - {"type":"session_meta","payload":{id,cwd,timestamp,originator,cli_version,...}}
     - {"type":"turn_context","payload":{model,...}}
     - {"type":"event_msg","payload":{"type":"token_count","info":{
          "last_token_usage":{input_tokens,cached_input_tokens,output_tokens,
                              reasoning_output_tokens,total_tokens}, ...}}}

Token mapping to the shared 4-component schema (so totals stay consistent):
  cache_read     = cached_input_tokens
  input          = input_tokens - cached_input_tokens   (uncached portion)
  output         = output_tokens                         (reasoning is a subset)
  cache_creation = 0
  -> total = input + output + cache_read == codex total_tokens

We record per-turn deltas (last_token_usage) with the event timestamp so the
charts work; summed they equal the session's cumulative total.
"""
from __future__ import annotations

import base64
import json
import os
import socket
import time
from datetime import datetime, timezone

try:
    from . import discovery
except ImportError:  # pragma: no cover - allow running as a loose script
    import discovery

RECENT_SESSION_MS = 24 * 3600 * 1000  # re-report a session row if active within this


def _norm_codex_rate_limits(rl, event_ts_ms=None):
    """Map codex rate_limits {primary(300m), secondary(10080m)} to the shared
    {five_hour, seven_day} shape with ISO reset times (the real 5h/weekly %).

    Newer CLIs report an absolute ``resets_at`` (epoch seconds); some builds
    only report a relative ``resets_in_seconds`` from the event time, so both
    are accepted.
    """
    if not isinstance(rl, dict):
        return None
    out = {}
    for slot in ("primary", "secondary"):
        w = rl.get(slot)
        if not isinstance(w, dict) or w.get("used_percent") is None:
            continue
        wm = w.get("window_minutes")
        key = "five_hour" if wm == 300 else ("seven_day" if wm == 10080 else None)
        if not key:
            continue
        iso = None
        epoch = w.get("resets_at")
        if epoch is None and w.get("resets_in_seconds") is not None and event_ts_ms:
            epoch = event_ts_ms / 1000 + w["resets_in_seconds"]
        if epoch:
            try:
                iso = datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()
            except (ValueError, OSError, TypeError):
                iso = None
        out[key] = {"utilization": w["used_percent"], "resets_at": iso}
    return out or None


def iso_to_ms(ts):
    if not ts:
        return None
    try:
        return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000)
    except (ValueError, TypeError):
        return None


def _basename(cwd):
    return os.path.basename((cwd or "").rstrip("/")) or (cwd or "")


def _decode_jwt_claims(token):
    """Return the (unverified) payload claims of a JWT, or {}.

    We only read identity claims (email, plan); we never store the token itself.
    """
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:  # noqa: BLE001
        return {}


class CodexCollector:
    def __init__(self, codex_dir="~/.codex", host=None, file_state=None,
                 include_archived=False, bootstrap_days=2):
        self.codex_dir = os.path.expanduser(codex_dir)
        self.host = host or socket.gethostname()
        self.file_state = file_state if file_state is not None else {}
        self.include_archived = include_archived
        # See ClaudeCollector: a newly discovered directory can hold months of
        # rollouts whose turns are all unattributable.  One real directory on
        # this cluster holds 214k such turns.
        self.bootstrap_days = max(0, bootstrap_days or 0)
        # Rate limits belong to an account, not to the directory forever: a
        # login switch must not leave the previous user's percentage on the new
        # user's dashboard card.  Cached PER WINDOW so a newer event that only
        # carries the weekly window can't erase the last known 5h value.
        self._rl_cache = {}  # {(account_id, email): {window: (ts_ms, data)}}
        # Account-boundary protection.  A rollout carries no account of its own,
        # and a changed file is re-parsed in full, so labelling every turn with
        # whatever account is logged in *now* would retroactively relabel turns
        # written under a previous login.  A turn is only attributable when its
        # own event timestamp falls after the previous poll AND the account was
        # unchanged across those two polls -- no other account could have
        # written it in that window.
        self._last_poll_ms = 0
        self._last_account_identity = None
        # rollout path -> its session_meta payload.  A file that did not change
        # this cycle is not re-parsed, and the session row was then built from
        # an empty meta -- so every such row went out with cwd=None.  The
        # dashboard maps a session to a person BY cwd, so those rows could not
        # be attributed to anyone: 79 of the 81 unattributed sessions in the
        # last snapshot were exactly this, not a missing rule.
        self._meta_by_path = {}

    @staticmethod
    def _state_size_mtime(value):
        if isinstance(value, dict):
            return value.get("size"), value.get("mtime")
        if isinstance(value, (list, tuple)) and len(value) >= 2:
            return value[0], value[1]
        return None, None

    @staticmethod
    def _state_for(st, account):
        return {
            "size": st.st_size,
            "mtime": st.st_mtime,
            # Codex rollout parsing is still UUID-idempotent; it has a
            # different cumulative event format, so no byte cursor is used.
            "offset": None,
            "account_email": account.get("email") if account else None,
            "account_id": account.get("account_id") if account else None,
            "inode": getattr(st, "st_ino", None),
        }

    # -- account --
    def read_account(self):
        path = os.path.join(self.codex_dir, "auth.json")
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                auth = json.load(f)
        except (OSError, ValueError):
            return None
        tokens = auth.get("tokens") or {}
        account_id = tokens.get("account_id")
        claims = _decode_jwt_claims(tokens.get("id_token", ""))
        oauth = claims.get("https://api.openai.com/auth", {}) if isinstance(claims, dict) else {}
        email = claims.get("email") or account_id
        plan = oauth.get("chatgpt_plan_type")
        orgs = oauth.get("organizations") or []
        org_name = None
        for o in orgs:
            if o.get("is_default"):
                org_name = o.get("title")
                break
        if not email:
            return None
        return {
            "provider": "codex",
            "email": email,
            "account_id": oauth.get("chatgpt_account_id") or account_id,
            "org_type": ("chatgpt_" + plan) if plan else "chatgpt",
            "rate_limit_tier": plan,
            "display_name": org_name,
            "org_name": org_name,
        }

    # -- rollout files --
    def _iter_rollouts(self):
        roots = [os.path.join(self.codex_dir, "sessions")]
        if self.include_archived:
            roots.append(os.path.join(self.codex_dir, "archived_sessions"))
        for root in roots:
            if not os.path.isdir(root):
                continue
            for dirpath, _dirs, files in os.walk(root):
                for fn in files:
                    if fn.startswith("rollout-") and fn.endswith(".jsonl"):
                        yield os.path.join(dirpath, fn)

    def _read_meta(self, path):
        """Return this rollout's ``session_meta`` payload, reading if needed.

        Only the head of the file is scanned (session_meta is the first
        record), which is cheap enough to do for a recently-active rollout that
        would otherwise be reported with no working directory at all.  Cached
        because the metadata of a given rollout never changes.
        """
        cached = self._meta_by_path.get(path)
        if cached is not None:
            return cached
        meta = {}
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for i, line in enumerate(f):
                    if i > 20:
                        break
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    if d.get("type") == "session_meta":
                        meta = d.get("payload") or {}
                        break
        except OSError:
            meta = {}
        self._meta_by_path[path] = meta
        return meta

    def _parse_rollout(self, path, email, attributable_after_ms=None):
        """Parse one rollout.

        ``attributable_after_ms`` is the cutoff described in ``__init__``: turns
        at or after it belong to ``email``; earlier ones are emitted as
        ``assumed`` (no account) so ingestion drops them instead of crediting
        them to the wrong login.  ``None`` makes every turn assumed.
        """
        meta = {}
        model = None
        source = None
        surface = discovery.UNKNOWN
        usage = []
        seq = 0
        win_latest = {}       # {window: (event_ts_ms, {utilization, resets_at})}
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    t = d.get("type")
                    p = d.get("payload") or {}
                    if t == "session_meta":
                        meta = p
                        # Which surface opened this thread.  Codex records both
                        # ``originator`` ("codex_vscode" / "codex_cli_rs" /
                        # "Codex Desktop") and ``source`` ("vscode").
                        # ``originator`` wins: the desktop app reports
                        # source="vscode" too, so trusting ``source`` first
                        # would file every desktop turn under the sidebar.
                        # ``source`` is a dict for subagent threads -- carry
                        # only the string form so no payload lands in a record.
                        raw_source = p.get("source")
                        source = raw_source if isinstance(raw_source, str) else None
                        surface = discovery.normalize_surface(
                            p.get("originator"), source)
                    elif t == "turn_context":
                        model = p.get("model") or model
                    elif t == "event_msg" and p.get("type") == "token_count":
                        ts = iso_to_ms(d.get("timestamp"))
                        rl = p.get("rate_limits")        # real 5h/weekly % live here
                        plan = rl.get("plan_type") if isinstance(rl, dict) else None
                        norm = _norm_codex_rate_limits(rl, ts)
                        for k, v in (norm or {}).items():
                            prev = win_latest.get(k)
                            if prev is None or (ts or 0) >= prev[0]:
                                win_latest[k] = ((ts or 0), v)
                        info = p.get("info")
                        if not info:
                            continue
                        lu = info.get("last_token_usage") or {}
                        cached = lu.get("cached_input_tokens", 0) or 0
                        inp = lu.get("input_tokens", 0) or 0
                        out = lu.get("output_tokens", 0) or 0
                        if (inp + out) == 0:
                            continue
                        sid = meta.get("id") or _sid_from_name(path)
                        seq += 1
                        provable = bool(attributable_after_ms and ts
                                        and ts >= attributable_after_ms)
                        usage.append({
                            "uuid": f"codex:{sid}:{ts}:{seq}",
                            "provider": "codex",
                            "session_id": sid,
                            "project": _basename(meta.get("cwd")),
                            "cwd": meta.get("cwd"),
                            "git_branch": None,
                            "model": model,
                            "ts": ts,
                            "input_tokens": max(0, inp - cached),
                            "output_tokens": out,
                            "cache_creation_tokens": 0,
                            "cache_read_tokens": cached,
                            "service_tier": plan,
                            "request_id": None,
                            "version": meta.get("cli_version"),
                            "entrypoint": meta.get("originator"),
                            "source": source,
                            "surface": surface,
                            "account_email": email if provable else None,
                            "assumed": not provable,
                        })
        except OSError:
            return {}, [], {}
        return meta, usage, win_latest

    def collect(self):
        account = self.read_account()
        email = account["email"] if account else None
        account_key = ((account.get("account_id") or "", email) if account else None)
        # Only a login that was already in place at the previous poll can claim
        # turns; right after a switch nothing is attributable until the next
        # cycle establishes the new account as stable.
        account_stable = bool(account_key and account_key == self._last_account_identity)
        attributable_after_ms = self._last_poll_ms if account_stable else None
        now_ms = int(time.time() * 1000)
        cutoff_ms = (now_ms - self.bootstrap_days * 86400 * 1000
                     if self.bootstrap_days else 0)
        records, updated, sessions = [], {}, []
        for path in self._iter_rollouts():
            try:
                st = os.stat(path)
            except OSError:
                continue
            mtime_ms = int(st.st_mtime * 1000)
            prev_size, prev_mtime = self._state_size_mtime(self.file_state.get(path))
            changed = not (prev_size == st.st_size and prev_mtime == st.st_mtime)
            meta, usage = (None, [])
            first_sight = prev_size is None and prev_mtime is None
            skipped_backlog = bool(changed and first_sight and cutoff_ms
                                   and mtime_ms < cutoff_ms)
            if skipped_backlog:
                state = self._state_for(st, account)
                self.file_state[path] = state
                updated[path] = state
            elif changed:
                meta, usage, rl_wins = self._parse_rollout(
                    path, email, attributable_after_ms)
                self._meta_by_path[path] = meta or {}
                # ``originator`` is always a string; keep the row consistent.
                records.extend(usage)
                state = self._state_for(st, account)
                self.file_state[path] = state
                updated[path] = state
                if rl_wins and account_key is not None:
                    slot = self._rl_cache.setdefault(account_key, {})
                    for k, (ts_w, v) in rl_wins.items():
                        prev = slot.get(k)
                        if prev is None or ts_w >= prev[0]:
                            slot[k] = (ts_w, v)
            # only emit a session row for files that changed or were recently
            # active — avoids re-sending hundreds of old rollouts every cycle.
            if (skipped_backlog or not changed) and (now_ms - mtime_ms) > RECENT_SESSION_MS:
                continue
            if meta is None:
                meta = self._read_meta(path)
            sid = (meta or {}).get("id") or _sid_from_name(path)
            cwd = (meta or {}).get("cwd")
            sessions.append({
                "session_id": sid, "provider": "codex", "cwd": cwd,
                "project": _basename(cwd), "version": (meta or {}).get("cli_version"),
                "kind": "interactive", "entrypoint": (meta or {}).get("originator"),
                "surface": discovery.normalize_surface(
                    (meta or {}).get("originator"), (meta or {}).get("source")
                    if isinstance((meta or {}).get("source"), str) else None),
                "status": "active", "pid": None, "pid_alive": None,
                "started_at": iso_to_ms((meta or {}).get("timestamp")),
                "updated_at": int(st.st_mtime * 1000),
                "account_email": email,
            })
        cached = self._rl_cache.get(account_key) if account_key else None
        if account and cached:
            rl_out = {"source": "codex_rollout"}
            newest = 0
            for k, (ts_w, v) in cached.items():
                rl_out[k] = v
                newest = max(newest, ts_w)
            account["rate_limits"] = rl_out
            # The timestamp comes from the rollout event, not from every
            # collector pass that happens to reuse the cache.
            account["rate_limits_updated_at"] = newest or now_ms
        self._last_account_identity = account_key
        self._last_poll_ms = now_ms
        return {
            "account": account, "usage": records, "sessions": sessions,
            "file_state": updated,
        }


def _sid_from_name(path):
    # rollout-2026-05-21T07-49-20-019e4982-a891-70f2-87a8-729d0ca9ff79.jsonl
    base = os.path.basename(path)[:-6]  # strip .jsonl
    parts = base.split("-")
    if len(parts) >= 5:
        return "-".join(parts[-5:])
    return base
