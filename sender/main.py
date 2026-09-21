"""Sender main loop.

Collects Claude + Codex usage from this server and delivers a batch to the NAS
every `interval_seconds`. Two delivery transports (see config.transport.mode):
  - "ssh"   : server has NO NAS mount -> scp the batch to the Synology over SSH
              (password auto-typed). This is the default for GPU servers.
  - "local" : server HAS the NAS mounted -> write straight into the mount.

Batches are first written to a local outbox (data/outbox/), then delivered.
A failed delivery leaves the batch queued and is retried next cycle, so a NAS
outage never loses data. Delivered batches are removed locally.

    PYTHONPATH=. python3 -m sender.main            # loop, uses ./config.json
    PYTHONPATH=. python3 -m sender.main --once     # one batch then exit (testing)
"""
from __future__ import annotations

import argparse
import collections
import copy
import getpass
import hashlib
import json
import os
import socket
import sys
import time
import traceback

try:
    from .claude_collector import ClaudeCollector
    from .codex_collector import CodexCollector
    from . import discovery, nas_writer, transport
except ImportError:  # pragma: no cover
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from sender.claude_collector import ClaudeCollector
    from sender.codex_collector import CodexCollector
    from sender import discovery, nas_writer, transport

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT, "config.json")
STATE_PATH = os.path.join(ROOT, "data", "sender-state.json")
OUTBOX = os.path.join(ROOT, "data", "outbox")
MAX_OUTBOX = 5000  # safety cap if the NAS is unreachable for a very long time

DEFAULTS = {
    "node_id": socket.gethostname(),
    "interval_seconds": 300,
    "retain_hours": 72,
    "compress": True,
    "nas_root": "/mnt/nas/yunseok/ai-monitoring",   # used only by transport.mode=local
    "transport": {
        "mode": "ssh",                              # "ssh" (no mount) or "local"
        "ssh_host": "aidaslab.synology.me",
        "ssh_port": 2244,
        "ssh_user": "synologynas",
        "ssh_password": "",                         # set by setup.sh (gitignored)
        "ssh_key": "",                              # optional: path to a private key instead
        "remote_root": "/volume1/nas-nfs/yunseok/ai-monitoring",
    },
    # config_dirs/dirs ADD to what discovery finds; they no longer replace it.
    # Narrowing collection to the lab directories is what made every session
    # that escaped the launcher (VSCode sidebar after an extension update, a
    # tmux pane opened before setup, a plain `claude`) invisible.
    "claude": {"enabled": True, "config_dirs": []},
    "codex": {"enabled": True, "dirs": ["~/.codex"], "include_archived": False},
    # real 5h/weekly utilization via Anthropic OAuth usage endpoint (per account)
    "claude_usage": {"enabled": True, "interval_seconds": 300},
    # Find config dirs from this user's live tool processes and from the
    # filesystem, every cycle.  Set false to collect only what is configured.
    "discover": True,
    "extra_roots": [],        # extra roots to scan for .claude*/.codex*
    "exclude_dirs": [],       # never collect these (e.g. a personal login)
    # A newly discovered directory's older history is emitted as "assumed" and
    # dropped centrally, so it is skipped rather than uploaded.  0 disables.
    "bootstrap_days": 2,
}


def _primary_ip():
    """Best-effort outbound-facing local IP.

    node_id is often just a hostname (sometimes a generic default like
    "servername"), which is not enough to SSH back to the box. A UDP socket
    connect() sends no packets — it only asks the kernel which local address
    would be used for that route.
    """
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(1)
        s.connect(("8.8.8.8", 53))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        if s is not None:
            try:
                s.close()
            except OSError:
                pass


def _os_user():
    """OS account the sender runs as (it reads THAT user's ~/.claude*/~/.codex).

    getpass.getuser() consults the environment first, so fall back to the real
    uid's passwd entry — a stale SUDO_USER/LOGNAME must not mislabel the node.
    """
    try:
        import pwd
        return pwd.getpwuid(os.getuid()).pw_name
    except Exception:  # noqa: BLE001  (non-POSIX, or no passwd entry)
        try:
            return getpass.getuser()
        except Exception:  # noqa: BLE001
            return None


def deep_merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_config():
    cfg = json.loads(json.dumps(DEFAULTS))
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = deep_merge(cfg, json.load(f))
    return cfg


def load_state():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            return {}
        # New Claude cursors are dicts (offset + account provenance).  Keep
        # legacy [size, mtime] records readable during a rolling upgrade.
        state = {}
        for path, value in raw.items():
            if isinstance(value, dict):
                state[path] = dict(value)
            elif isinstance(value, (list, tuple)) and len(value) >= 2:
                state[path] = tuple(value)
        return state
    except (OSError, ValueError):
        return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            # json natively serializes legacy tuples as arrays and preserves
            # structured cursor records without flattening their provenance.
            json.dump(state, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, STATE_PATH)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _machine_id():
    """Stable per-machine identifier, hashed.

    Three installs on this cluster report as three node_ids from one box -- one
    per OS user who ran setup.sh -- so the dashboard counts one machine as
    three nodes.  ``node_id`` stays whatever the operator chose (existing
    history is keyed by it), but every batch now also carries a value that is
    equal across those installs.  It is hashed so a raw host fingerprint never
    leaves the machine.
    """
    for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = f.read().strip()
        except OSError:
            continue
        if raw:
            return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return None


class CollectorRegistry:
    """Collectors kept alive across cycles, keyed by (provider, path).

    Discovery reruns every cycle because directories appear at any moment: a
    new login, a VSCode window opening, ``setup-accounts.py`` creating the lab
    dirs.  But a collector proves an account by remembering what it saw at the
    *previous* poll, so rebuilding them each cycle would mark every record
    ``assumed`` forever and nothing would ever be counted.  The instances
    therefore persist; only the set of keys changes.
    """

    def __init__(self):
        self._by_key = {}
        # One usage fetch per login rather than per directory: discovering more
        # directories must not multiply calls to an endpoint that 429s easily.
        self._usage_cache = {}

    def _make(self, cfg, provider, path):
        boot = cfg.get("bootstrap_days", 2)
        if provider == discovery.CLAUDE:
            cu = cfg.get("claude_usage") or {}
            return ClaudeCollector(
                path, host=cfg["node_id"],
                usage_enabled=cu.get("enabled", True),
                usage_interval=cu.get("interval_seconds", 300),
                bootstrap_days=boot, usage_cache=self._usage_cache)
        return CodexCollector(
            path, host=cfg["node_id"], bootstrap_days=boot,
            include_archived=(cfg.get("codex") or {}).get("include_archived", False))

    def sync(self, cfg, disc):
        """Return [(collector, dir record)] for everything discovery found."""
        wanted = {}
        for provider, dirs in disc["dirs"].items():
            for d in dirs:
                wanted[(provider, d["path"])] = dict(d, provider=provider)
        for key in [k for k in self._by_key if k not in wanted]:
            del self._by_key[key]
        entries = []
        for key in sorted(wanted):
            if key not in self._by_key:
                self._by_key[key] = self._make(cfg, key[0], key[1])
            entries.append((self._by_key[key], wanted[key]))
        return entries


def _reset_collector_state(entries, state):
    """Make the durable state the only cursor source for the next poll."""
    for collector, _dirinfo in entries:
        collector.file_state = copy.deepcopy(state)


def _dedupe_usage(records):
    """Drop a turn we read twice, keeping the copy that can prove its account.

    One login often lives in more than one collected directory -- most commonly
    because ``migrate-history.py`` copied a history instead of moving it, which
    leaves the same transcript in both.  Now that everything in use is
    collected, both copies are read and the same turn is reported twice.  The
    uuid IS the turn's identity, so the repeat can be dropped safely; which
    copy survives matters, because one directory may be able to attribute the
    turn while the other cannot.
    """
    seen, keyless = {}, []
    for r in records:
        uid = r.get("uuid")
        if not uid:
            keyless.append(r)
            continue
        prev = seen.get(uid)
        if prev is None or (prev.get("assumed") and not r.get("assumed")):
            seen[uid] = r          # replacing keeps the original position
    out = list(seen.values()) + keyless
    return out, len(records) - len(out)


def _account_rank(a):
    """Prefer the copy that actually carries limits, then the freshest."""
    return (1 if a.get("rate_limits") else 0, a.get("rate_limits_updated_at") or 0)


def _dedupe_accounts(accounts):
    """One login present in several directories must produce one card.

    Sending it twice leaves the dashboard to choose arbitrarily between two
    entries whose rate limits may differ, so a stale directory could overwrite
    a fresh reading.
    """
    best = {}
    for a in accounts:
        key = (a.get("provider"), a.get("email"), a.get("account_id"))
        prev = best.get(key)
        if prev is None or _account_rank(a) > _account_rank(prev):
            best[key] = a
    return list(best.values())


def _dedupe_sessions(sessions):
    seen = {}
    for s in sessions:
        key = (s.get("provider"), s.get("session_id"))
        if key[1] and (key not in seen or s.get("account_email")):
            seen[key] = s
        elif not key[1]:
            seen[(id(s),)] = s
    return list(seen.values())


def collect_all(cfg, entries, state, disc):
    """Collect a batch without acknowledging its cursors yet.

    Cursor advancement must be committed only after the matching batch exists
    durably in the local outbox.  Otherwise a process crash or disk error can
    skip bytes that were never delivered.  A separate candidate state also
    lets a failed collector roll back any partial in-memory mutation.
    """
    accounts, usage, sessions, dir_report = [], [], [], []
    pending_state = copy.deepcopy(state)
    for collector, dirinfo in entries:
        collector.file_state = pending_state
        before_collector = copy.deepcopy(pending_state)
        row = {"provider": dirinfo["provider"], "path": dirinfo["path"],
               "display": dirinfo["display"], "found_by": dirinfo["source"]}
        try:
            part = collector.collect()
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            pending_state = before_collector
            dir_report.append(dict(row, error="collect failed"))
            continue
        account = part.get("account") or {}
        if account:
            accounts.append(account)
        usage.extend(part.get("usage", []))
        sessions.extend(part.get("sessions", []))
        updated = part.get("file_state") or {}
        if isinstance(updated, dict):
            pending_state.update(updated)
        dir_report.append(dict(
            row, account_email=account.get("email"),
            usage=len(part.get("usage", [])),
            sessions=len(part.get("sessions", []))))

    warnings = list(disc["warnings"])
    usage, duplicates = _dedupe_usage(usage)
    accounts = _dedupe_accounts(accounts)
    sessions = _dedupe_sessions(sessions)
    if duplicates:
        warnings.append(
            f"같은 턴을 여러 디렉토리에서 중복으로 읽었습니다 ({duplicates}건) — "
            "이력을 복사만 하고 원본을 지우지 않았을 수 있습니다 "
            "(scripts/migrate-history.py 는 --move 로 쓰세요). 중복은 보내지 않았습니다")

    collected = {d["path"] for d in dir_report}
    # Which surfaces this node actually produced.  A node that reports only
    # "terminal" when someone works in the sidebar all day is the symptom this
    # whole change exists to make visible.
    surfaces = collections.Counter(u.get("surface") or "unknown" for u in usage)
    return {
        "schema": 1, "host": cfg["node_id"],
        "generated_at": int(time.time() * 1000),
        # Self-identification: which install on which account is reporting.
        # Without this a node is only known by node_id, and finding the sender
        # again (to redeploy or debug) means hunting the filesystem by hand.
        "sender_root": ROOT,
        "os_user": _os_user(),
        "home": os.path.expanduser("~"),
        "machine_id": _machine_id(),
        "fqdn": socket.getfqdn(),
        "ip": _primary_ip(),
        "accounts": accounts, "usage": usage, "sessions": sessions,
        # Coverage, not payload: what is being read, what is writing, and what
        # is writing somewhere nobody reads.  Silence used to be the only
        # symptom of a broken install; now the batch says so itself.
        "diagnostics": {
            "dirs": dir_report,
            "surfaces": dict(surfaces),
            "processes": [{"pid": p["pid"], "provider": p["provider"],
                           "surface_hint": p["surface_hint"],
                           "config_dir": p["config_dir"],
                           "collected": p["config_dir"] in collected}
                          for p in disc["processes"]],
            "uncollected": sorted({p["config_dir"] for p in disc["uncollected"]}),
            "duplicates_dropped": duplicates,
            "warnings": warnings,
        },
    }, pending_state


def _outbox_batches():
    os.makedirs(OUTBOX, exist_ok=True)
    return sorted(f for f in os.listdir(OUTBOX) if f.startswith("batch-"))


def _cap_outbox():
    files = _outbox_batches()
    if len(files) > MAX_OUTBOX:
        for fn in files[:len(files) - MAX_OUTBOX]:
            try:
                os.remove(os.path.join(OUTBOX, fn))
            except OSError:
                pass
        print(f"[sender] WARNING: outbox over {MAX_OUTBOX}; dropped oldest",
              file=sys.stderr)


def run_once(cfg, registry, state, tport):
    # Rediscover every cycle: a directory can appear at any time, and a config
    # that was right at startup is not a promise about now.
    disc = discovery.discover(cfg)
    entries = registry.sync(cfg, disc)
    result, pending_state = collect_all(cfg, entries, state, disc)
    ms = int(time.time() * 1000)
    name = f"batch-{ms:013d}.json" + (".gz" if cfg["compress"] else "")
    local_path = os.path.join(OUTBOX, name)
    try:
        nbytes = nas_writer.build_gz(local_path, result, cfg["compress"])
        # The outbox file is durable before its byte cursors are acknowledged.
        # A delivery failure is fine: the durable batch is retried below.
        save_state(pending_state)
    except Exception:
        # Do not let a failed local transaction advance in-memory cursors.  If
        # state persistence failed, discard this unsafely-unacknowledged batch
        # so the next poll recollects it from the last durable cursor.
        try:
            os.remove(local_path)
        except OSError:
            pass
        _reset_collector_state(entries, state)
        raise
    state.clear()
    state.update(pending_state)
    _reset_collector_state(entries, state)
    _cap_outbox()

    # flush the outbox oldest-first; stop on first failure (preserve order, retry)
    pending = _outbox_batches()
    sent, failed = 0, None
    for fn in pending:
        path = os.path.join(OUTBOX, fn)
        try:
            tport.deliver(path, cfg["node_id"], fn)
            os.remove(path)
            sent += 1
        except Exception as e:  # noqa: BLE001
            failed = str(e)
            break
    remaining = len(_outbox_batches())
    if sent:
        try:
            tport.prune(cfg["node_id"], cfg["retain_hours"])
        except Exception:  # noqa: BLE001
            pass

    accts = ", ".join(sorted({a.get("email", "?") for a in result["accounts"]})) or "none"
    diag = result["diagnostics"]
    surf = " ".join(f"{k}={v}" for k, v in sorted(diag["surfaces"].items())) or "none"
    msg = (f"[sender] batch {name} ({nbytes} B) usage={len(result['usage'])} "
           f"sessions={len(result['sessions'])} accounts=[{accts}] "
           f"dirs={len(diag['dirs'])} surfaces[{surf}] "
           f"delivered={sent} queued={remaining} via {tport.describe()}")
    if failed:
        msg += f"  DELIVERY FAILED: {failed}"
    print(msg, file=sys.stderr, flush=True)
    # Warnings go on their own lines so status.sh shows them without parsing.
    for warning in diag["warnings"]:
        print(f"[sender] WARN {warning}", file=sys.stderr, flush=True)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="AIDAS monitoring sender")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=int, default=None)
    args = parser.parse_args(argv)

    cfg = load_config()
    if args.interval:
        cfg["interval_seconds"] = args.interval
    state = load_state()
    registry = CollectorRegistry()
    tport = transport.make_transport(cfg)

    print(f"[sender] node={cfg['node_id']} user={_os_user()} "
          f"transport={tport.describe()} interval={cfg['interval_seconds']}s "
          f"discover={'on' if cfg.get('discover', True) else 'off'}",
          file=sys.stderr)

    if args.once:
        run_once(cfg, registry, state, tport)
        return
    while True:
        try:
            run_once(cfg, registry, state, tport)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        time.sleep(cfg["interval_seconds"])


if __name__ == "__main__":
    main()
