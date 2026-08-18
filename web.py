# web.py
# ─────────────────────────────────────────────────────────────
# Flask web frontend for the VM Agent — MULTI-USER SUPPORT.
# Each browser gets a unique session ID (cookie) with isolated:
#   - SSH connection to host
#   - Session state (host_ip, credentials, config)
#   - VM being built
#
# Extra features:
#   - Real-time log streaming to UI (SSE from installer_autopilot log queue)
#   - Downloadable log archive per VM
#   - Config validation preview (dry-run PCI selection)
#   - Rollback on failure (cleanup partial VMs)
#   - Audit log (append-only log of all actions)
#   - Optional API key authentication
# ─────────────────────────────────────────────────────────────

import sys
import os
import json
import time
import re
import uuid
import traceback
import threading
from datetime import datetime

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from flask import Flask, render_template, request, jsonify, Response, make_response, send_file

from config import VMConfig
import config_validator
import orchestrator
from state import AgentState
from settings import DRY_RUN, AGENT_MODE, ANTHROPIC_API_KEY, KVM_APT_PACKAGES, VM_WORK_DIR, SLACK_WEBHOOK_URL, RESEND_API_KEY, RESEND_FROM, RESEND_API_URL
import input_extractor
import llm_client

# Optional API key (set VM_AGENT_API_KEY env var to enable auth on write endpoints)
API_KEY = os.environ.get("VM_AGENT_API_KEY", "").strip()

# Audit log file
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
AUDIT_LOG_PATH = os.path.join(LOG_DIR, "audit.log")
_AUDIT_LOCK = threading.Lock()

app = Flask(__name__)


def _audit(sid, action, details=""):
    """Append an entry to the audit log (thread-safe)."""
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with _AUDIT_LOCK:
            with open(AUDIT_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(f"{datetime.now().isoformat()} | {sid[:8]} | {action} | {details}\n")
    except Exception:
        pass


def _check_api_key():
    """Check API key from X-API-Key header. Returns True if OK or auth disabled.

    Browser sessions authenticated through the server-credential login are
    already authorized; the header path only matters for scripted access.
    """
    if not API_KEY:
        return True
    sid = _get_session_id()
    session = _get_session(sid)
    if session.get("authed"):
        return True
    provided = request.headers.get("X-API-Key", "").strip()
    return provided == API_KEY

# ── Per-session state storage ────────────────────────────────
# _SESSIONS: {session_id: {config, stage, host_ip, host_username,
#                          host_password, host_resources, ssh_client}}
_SESSIONS = {}
_SESSIONS_LOCK = threading.Lock()

SESSION_COOKIE = "vm_agent_session"
SESSION_TIMEOUT = 3600  # 1 hour idle timeout


def _get_session_id():
    """Get session ID from the X-Session-Token header, ?session= query param,
    or cookie; create a new one if none is known.

    The header/query paths keep sessions working even in sandboxed iframes /
    preview environments where cookies may be dropped or blocked (EventSource
    cannot set custom headers, so SSE streams pass ?session= instead).
    """
    sid = (request.headers.get("X-Session-Token") or "").strip()
    if not sid:
        sid = (request.args.get("session") or "").strip()
    if not sid:
        sid = request.cookies.get(SESSION_COOKIE) or ""
    if not sid or sid not in _SESSIONS:
        sid = str(uuid.uuid4())
    return sid


def _get_session(sid):
    """Get or create session state for the given session ID."""
    with _SESSIONS_LOCK:
        if sid not in _SESSIONS:
            _SESSIONS[sid] = {
                "config": None,
                "stage": "waiting",
                "host_ip": None,
                "host_username": None,
                "host_password": None,
                "host_resources": None,
                "ssh_client": None,
                "cancel_requested": False,
                "authed": False,
                "last_active": time.time(),
            }
        _SESSIONS[sid]["last_active"] = time.time()
        return _SESSIONS[sid]


def _cleanup_stale_sessions():
    """Remove sessions that have been idle beyond SESSION_TIMEOUT.

    Sessions that own a running build are kept alive so a 1-hour build is
    never orphaned from the browser that started it.
    """
    now = time.time()
    with _BUILDS_LOCK:
        busy = {b["sid"] for b in _BUILDS.values() if b["stage"] == "running"}
    with _SESSIONS_LOCK:
        stale = [
            sid for sid, s in _SESSIONS.items()
            if now - s.get("last_active", now) > SESSION_TIMEOUT and sid not in busy
        ]
        for sid in stale:
            ssh = _SESSIONS[sid].get("ssh_client")
            if ssh:
                try:
                    ssh.close()
                except Exception:
                    pass
            del _SESSIONS[sid]


def _make_response_with_cookie(sid, resp):
    """Attach session cookie to response."""
    resp.set_cookie(SESSION_COOKIE, sid, max_age=SESSION_TIMEOUT, httponly=True, samesite="Lax")
    return resp


# ── Concurrent build registry ────────────────────────────────
# Each VM build runs in its own daemon thread and streams events into its
# own buffer, so any number of builds can run at once (a 1-hour build no
# longer blocks starting the next one). Every build is keyed by a unique
# build_id; the browser subscribes per build and can re-attach after a
# page refresh.
#
# _BUILDS: {build_id: {sid, vm_name, stage, cancel_requested, events,
#                      log_lines, queue, plan, progress, started_at, done}}
_BUILDS = {}
_BUILDS_LOCK = threading.Lock()
_BUILD_EVENT_CAP = 5000      # SSE events buffered per build
_BUILD_LINE_CAP = 2000       # log lines kept per build for the log viewer
_MAX_BUILDS_PER_SESSION = 20  # prune history beyond this


def _new_build_id():
    return uuid.uuid4().hex[:12]


def _build_outcome(b):
    """Terminal outcome for a build (complete/failed/cancelled/error).

    Falls back to scanning the event buffer (SSE streams drain it, so the
    persistent flag set in _emit() is the source of truth).
    """
    if b.get("outcome"):
        return b["outcome"]
    for e in reversed(b.get("events", [])):
        if e.get("type") in ("complete", "failed", "cancelled", "error"):
            return e["type"]
    return "running"


def _build_snapshot(b):
    return {
        "build_id": b["id"],
        "vm_name": b["vm_name"],
        "host_ip": b.get("host_ip"),
        "stage": b["stage"],
        "outcome": _build_outcome(b),
        "started_at": b["started_at"],
        "agent_mode": b.get("agent_mode", False),
        "progress": b.get("progress", 0),
        "plan": b.get("plan", []),
    }


def _register_build(b):
    with _BUILDS_LOCK:
        _BUILDS[b["id"]] = b
        # Prune this session's old finished builds so the registry stays small
        mine = sorted(
            (x for x in _BUILDS.values() if x["sid"] == b["sid"]),
            key=lambda x: x["started_at"],
            reverse=True,
        )
        for old in mine[_MAX_BUILDS_PER_SESSION:]:
            if old.get("done"):
                del _BUILDS[old["id"]]


def _send_notification(message):
    """POST a plain-text message to the configured Slack/Teams/Discord webhook.

    Fire-and-forget (daemon thread); never blocks a build and never raises.
    Uses only the standard library.
    """
    if not SLACK_WEBHOOK_URL:
        return
    try:
        import urllib.request
        payload = json.dumps({"text": message}).encode("utf-8")
        req = urllib.request.Request(
            SLACK_WEBHOOK_URL, data=payload, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(f"[NOTIFY] sent (HTTP {resp.getcode()}): {message[:80]}", flush=True)
    except Exception as e:
        print(f"[NOTIFY] failed to send: {e}", flush=True)


def _send_email(to_email, subject, text):
    """Send a plain-text email via Resend (stdlib-only HTTP POST)."""
    if not RESEND_API_KEY or not to_email:
        return
    try:
        import urllib.request
        payload = json.dumps({
            "from": RESEND_FROM,
            "to": [to_email],
            "subject": subject,
            "text": text,
        }).encode("utf-8")
        req = urllib.request.Request(
            RESEND_API_URL,
            data=payload,
            headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            print(f"[NOTIFY][email] sent to {to_email} (HTTP {resp.getcode()}): {subject}", flush=True)
    except Exception as e:
        print(f"[NOTIFY][email] failed to send to {to_email}: {e}", flush=True)


def _build_summary_text(b, kind, obj=None):
    """All the details for a terminal build notification email."""
    cfg = b.get("config")
    vm = b.get("vm_name") or "?"
    host = b.get("host_ip") or "?"
    obj = obj or {}
    lines = [f"VM Agent — {vm} ({kind})", ""]
    lines.append(f"VM name : {vm}")
    lines.append(f"Host    : {host}")
    if obj.get("vm_ip"):
        lines.append(f"IP      : {obj['vm_ip']}")
    if obj.get("vm_mac"):
        lines.append(f"MAC     : {obj['vm_mac']}")
    if obj.get("vm_password") or obj.get("username"):
        lines.append(f"Login   : {obj.get('username') or 'ubuntu'}"
                     + (f" / {obj['vm_password']}" if obj.get("vm_password") else ""))
    if cfg is not None:
        lines.append(f"vCPUs   : {getattr(cfg, 'num_cpu', '?')}")
        lines.append(f"Memory  : {getattr(cfg, 'memory_gb', '?')} GB")
        lines.append(f"Disk    : {getattr(cfg, 'disk_size', '?')}")
        if getattr(cfg, "aic_cards", None):
            lines.append(f"AIC     : {cfg.aic_cards} card(s)")
        lines.append(f"Type    : {getattr(cfg, 'vm_type', 'normal')}")
    lines.append(f"Mode    : {'agent' if b.get('agent_mode') else 'classic'}")
    started = b.get("started_at")
    if started:
        mins, secs = divmod(int(time.time() - started), 60)
        lines.append(f"Duration: {mins}m {secs}s")
    return "\n".join(lines)


def _notify_build_outcome(b, kind, obj=None):
    """Queue notifications when a build reaches a terminal state.

    - Slack/Teams/Discord webhook (global, if SLACK_WEBHOOK_URL is set)
    - Email to the per-build notify email (if RESEND_API_KEY + address set)
    """
    vm = b.get("vm_name") or "?"
    host = b.get("host_ip") or "?"
    if kind == "complete":
        title = "✅ VM build complete"
        body = f"VM **{vm}** is ready on host {host}."
        subj = f"✅ VM Agent: {vm} build complete"
    elif kind == "failed":
        title = "❌ VM build failed"
        body = f"VM **{vm}** failed on host {host}."
        subj = f"❌ VM Agent: {vm} build failed"
    elif kind == "cancelled":
        title = "⏹ VM build cancelled"
        body = f"Build of **{vm}** was cancelled on host {host}."
        subj = f"⏹ VM Agent: {vm} build cancelled"
    else:
        title = "⚠️ VM build error"
        body = f"Build of **{vm}** errored on host {host}."
        subj = f"⚠️ VM Agent: {vm} build error"

    if SLACK_WEBHOOK_URL:
        threading.Thread(target=_send_notification, args=(f"{title}: {body}",), daemon=True).start()

    to_email = (b.get("notify_email") or "").strip()
    if RESEND_API_KEY and to_email:
        summary = _build_summary_text(b, kind, obj)
        threading.Thread(target=_send_email, args=(to_email, subj, summary), daemon=True).start()


def _emit(b, obj):
    """Push an event into a build's buffer (SSE + log lines)."""
    with b["lock"]:
        b["events"].append(obj)
        if len(b["events"]) > _BUILD_EVENT_CAP:
            del b["events"][: len(b["events"]) - _BUILD_EVENT_CAP]
        if obj.get("type") == "log" and obj.get("line"):
            b["log_lines"].append(obj["line"])
            if len(b["log_lines"]) > _BUILD_LINE_CAP:
                del b["log_lines"][: len(b["log_lines"]) - _BUILD_LINE_CAP]
        if obj.get("type") == "progress":
            b["progress"] = obj.get("percent", b.get("progress", 0))
        if obj.get("type") in ("complete", "failed", "cancelled", "error"):
            b["outcome"] = obj["type"]
            if not b.get("notified"):
                b["notified"] = True
                _notify_build_outcome(b, obj["type"], obj)


def _iter_build_events(b, replay=True):
    """Yield a build's events without consuming the shared buffer.

    Every SSE client (the original page's stream, plus streams re-attached
    after logout / page refresh) reads the same history via its own cursor,
    so re-attached clients receive the full buffered log instead of racing
    the original stream with destructive pop(0) calls. When the cap trims
    old events, a cursor that falls off the front simply restarts.
    """
    cursor = 0 if replay else len(b["events"])
    while True:
        with b["lock"]:
            events = b["events"]
            if cursor > len(events):
                cursor = 0  # history trimmed by the cap — restart
            while cursor < len(events):
                yield events[cursor]
                cursor += 1
            done = b.get("done", False)
        if done:
            break
        time.sleep(0.3)


def _new_build(sid, config, agent_mode, plan):
    import queue as _queue
    return {
        "id": _new_build_id(),
        "sid": sid,
        "vm_name": config.vm_name,
        "stage": "running",
        "cancel_requested": False,
        "events": [],
        "log_lines": [],
        "lock": threading.Lock(),
        "queue": _queue.Queue(maxsize=1000),  # installer log lines for this build
        "plan": plan,
        "progress": 0,
        "agent_mode": bool(agent_mode),
        "started_at": time.time(),
        "done": False,
        "notify_email": None,
        # Host this build belongs to. Snapshot at build start so the build
        # keeps running on the right server even if the UI session later
        # disconnects or switches to another host.
        "host_ip": None,
        "host_username": None,
        "host_password": None,
        # Dedicated SSH connection owned by this build (independent of the
        # browser session's connection).
        "ssh_client": None,
    }


# ── Dry-run VM simulation ────────────────────────────────────
# In DRY_RUN mode the app runs against a simulated KVM host so the whole
# UI (connect → build → inventory → stop/start/delete) can be exercised
# safely. The simulated inventory is stateful AND keyed per host: builds
# add VMs to the host they were started on, stop/start flips their state,
# delete removes them — so a refresh (or switching servers and coming
# back) shows reality exactly like virsh does on a real host.
_FAKE_VMS = {}  # host_ip -> {name -> {"state": "running" | "shut off"}}
_FAKE_VMS_LOCK = threading.Lock()
_CPU_SAMPLES = {}  # vm_name -> (timestamp, cpu.time) for delta-based CPU %


def _seed_fake_vms(host_ip):
    with _FAKE_VMS_LOCK:
        host_vms = _FAKE_VMS.setdefault(host_ip, {})
        if not host_vms:
            host_vms["demo-gpu-01"] = {"state": "running"}
            host_vms["demo-web-02"] = {"state": "shut off"}


def _register_built_vm(host_ip, vm_name):
    """In dry-run mode, make a freshly built VM appear in the inventory
    of the host it was actually built on."""
    if DRY_RUN and vm_name and host_ip:
        with _FAKE_VMS_LOCK:
            _FAKE_VMS.setdefault(host_ip, {})[vm_name] = {"state": "running"}


def _fake_ssh_output(cmd, host_ip):
    """Script the responses of the simulated host for a given shell command.

    Every virsh listing is scoped to host_ip, mirroring how the real app
    runs `virsh` on whichever host the session is connected to.
    """
    c = cmd
    m = None
    if "virsh list --state-running --name" in c:
        with _FAKE_VMS_LOCK:
            host_vms = _FAKE_VMS.get(host_ip, {})
            return "\n".join(sorted(n for n, v in host_vms.items() if v["state"] == "running"))
    if "virsh list --all --name" in c or "virsh list --name" in c:
        with _FAKE_VMS_LOCK:
            host_vms = _FAKE_VMS.get(host_ip, {})
            return "\n".join(sorted(host_vms.keys()))
    m = re.search(r"virsh domstate\s+(\S+)", c)
    if m:
        with _FAKE_VMS_LOCK:
            host_vms = _FAKE_VMS.get(host_ip, {})
            v = host_vms.get(m.group(1))
            return v["state"] if v else "shut off"
    m = re.search(r"virsh dominfo\s+(\S+)", c)
    if m:
        with _FAKE_VMS_LOCK:
            host_vms = _FAKE_VMS.get(host_ip, {})
            if m.group(1) not in host_vms:
                return ""
        return "CPU(s): 8\nMax memory: 33554432 KiB\nUsed memory: 33554432 KiB"
    m = re.search(r"virsh domstats\s+(\S+)", c)
    if m:
        # cpu.time advances at ~50% of wall-clock -> steady 50% CPU in demo
        import time as _t
        return (
            f"Domain: '{m.group(1)}'\n"
            f"  cpu.time={int(_t.time() * 0.5e9)}\n"
            "  balloon.current=12884901888\n"
            "  balloon.maximum=34359738368\n"
            "  net.0.rx.bytes=10485760\n"
            "  net.0.tx.bytes=2097152"
        )
    m = re.search(r"virsh domifaddr\s+(\S+)", c)
    if m:
        # Only a running VM reports its address; stopped VMs fall back to leases
        with _FAKE_VMS_LOCK:
            host_vms = _FAKE_VMS.get(host_ip, {})
            v = host_vms.get(m.group(1))
            if v and v.get("state") == "shut off":
                return ""
        return "  vnet0    52:54:00:12:34:56    ipv4    192.168.122.50/24"
    if "virsh net-list --all" in c:
        return " Name      State    Autostart\n default   active   yes"
    if "virsh net-dhcp-leases" in c:
        return (
            " Expiry Time          MAC address        Protocol  IP address\n"
            " 2026-08-18 12:00:00  52:54:00:12:34:56  ipv4      192.168.122.50/24"
        )
    m = re.search(r"virsh domiflist\s+(\S+)", c)
    if m:
        return (
            "Interface  Type     Source  Model   MAC\n"
            "vnet0      network  virbr0  virtio  52:54:00:12:34:56\n"
            "vnet1      network  virbr0  virtio  52:54:00:12:34:57"
        )
    m = re.search(r"virsh dumpxml\s+(\S+)", c)
    if m:
        return (
            "<domain type='kvm'>\n"
            "<hostdev mode='subsystem' type='pci' managed='yes'>\n"
            "<source><address domain='0x0000' bus='0x0a' slot='0x00' function='0x0'/></source>\n"
            "</hostdev>\n"
            "<hostdev mode='subsystem' type='pci' managed='yes'>\n"
            "<source><address domain='0x0000' bus='0x0b' slot='0x00' function='0x0'/></source>\n"
            "</hostdev>\n"
            "</domain>"
        )
    m = re.search(r"virsh domblklist\s+(\S+)", c)
    if m:
        return "Target   Source\n-------------------------------\nvda      /home/vm_images/ubuntu.qcow2"
    m = re.search(r"virsh domblkinfo\s+(\S+)", c)
    if m:
        return "Capacity: 100.0 GiB\nAllocation: 12.0 GiB"
    m = re.search(r"virsh shutdown\s+(\S+)", c)
    if m:
        with _FAKE_VMS_LOCK:
            host_vms = _FAKE_VMS.setdefault(host_ip, {})
            if m.group(1) in host_vms:
                host_vms[m.group(1)]["state"] = "shut off"
        return f"Domain {m.group(1)} is being shutdown"
    m = re.search(r"virsh destroy\s+(\S+)", c)
    if m:
        with _FAKE_VMS_LOCK:
            host_vms = _FAKE_VMS.setdefault(host_ip, {})
            if m.group(1) in host_vms:
                host_vms[m.group(1)]["state"] = "shut off"
        return f"Domain {m.group(1)} destroyed"
    m = re.search(r"virsh start\s+(\S+)", c)
    if m:
        with _FAKE_VMS_LOCK:
            host_vms = _FAKE_VMS.setdefault(host_ip, {})
            if m.group(1) in host_vms:
                host_vms[m.group(1)]["state"] = "running"
        return f"Domain {m.group(1)} started"
    m = re.search(r"virsh undefine\s+(\S+)", c)
    if m:
        with _FAKE_VMS_LOCK:
            host_vms = _FAKE_VMS.get(host_ip, {})
            host_vms.pop(m.group(1), None)
        return f"Domain {m.group(1)} has been undefined"
    if c.startswith("sudo -S cp "):
        return ""
    m = re.search(r"virsh define (/tmp/(\S+)_clone\.xml)", c)
    if m:
        with _FAKE_VMS_LOCK:
            _FAKE_VMS.setdefault(host_ip, {})[m.group(2)] = {"state": "shut off"}
        return f"Domain {m.group(2)} defined from {m.group(1)}"
    if "rm -f" in c and "pci" in c:
        return ""
    if "free -b | grep Mem" in c:
        return "Mem:   137438953472 20000000000 110000000000 1000000000 5000000000 110000000000"
    if c.strip() == "nproc":
        return "32"
    if "df -B1" in c:
        return "Filesystem     1B-blocks     Used Available Use% Mounted on\n/dev/sda1 2147483648000 700000000000 1400000000000 34% /home"
    if "systemctl is-active qmonitor-proxy" in c:
        return "active"
    return ""


class _FakeChannel:
    """Minimal stand-in for a paramiko channel."""

    def __init__(self, host_ip):
        self._cmd = ""
        self._host_ip = host_ip

    def get_pty(self):
        return None

    def set_combine_stderr(self, v):
        pass

    def settimeout(self, t):
        pass

    def exec_command(self, cmd):
        self._cmd = cmd

    def sendall(self, data):
        pass

    def makefile(self, mode="rb"):
        out = _fake_ssh_output(self._cmd, self._host_ip)
        class _F:
            def __init__(self, s):
                self.s = s
            def read(self):
                return self.s.encode("utf-8", "replace")
        return _F(out)

    def recv_exit_status(self):
        # virsh returns exit 1 when a domain/network doesn't exist; the empty
        # output models that so clone/delete existence checks behave like real.
        if "virsh dominfo" in self._cmd or "virsh domstate" in self._cmd:
            out = _fake_ssh_output(self._cmd, self._host_ip)
            if not out.strip():
                return 1
        return 0


class _FakeSSH:
    """Simulated paramiko SSH client backed by _fake_ssh_output().

    Carries the host IP it represents so every simulated `virsh` command
    reads/writes only that host's inventory.
    """

    def __init__(self, host_ip):
        self._host_ip = host_ip

    def open_sftp(self):
        class _FakeSFTP:
            def open(self, path, mode="r"):
                class _FakeFile:
                    def __init__(self):
                        self._data = ""
                    def write(self, s):
                        self._data += s
                        return len(s)
                    def close(self):
                        pass
                    def __enter__(self):
                        return self
                    def __exit__(self, *a):
                        self.close()
                return _FakeFile()
            def close(self):
                pass
        return _FakeSFTP()

    def get_transport(self):
        host_ip = self._host_ip
        class _T:
            def open_session(self):
                return _FakeChannel(host_ip)
        return _T()

    def close(self):
        pass


# ── SSH helpers ───────────────────────────────────────────────

def _make_ssh(host_ip, username, password):
    """Create and return a connected paramiko SSH client."""
    import paramiko
    import traceback
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ssh.connect(
            host_ip,
            username=username,
            password=password,
            timeout=30,
            banner_timeout=30,
            auth_timeout=30,
            look_for_keys=False,
            allow_agent=False,
        )
    except Exception as e:
        print(f"[SSH ERROR] {type(e).__name__}: {e}")
        traceback.print_exc()
        raise
    return ssh


def _ssh_run(ssh, cmd, password=None, timeout=60):
    """Run a command over SSH and return (exit_code, output_string).
    If password is provided, allocates a PTY so sudo can read it reliably.
    """
    transport = ssh.get_transport()
    channel = transport.open_session()
    if password:
        channel.get_pty()  # PTY needed for sudo to prompt properly
    channel.set_combine_stderr(True)
    channel.settimeout(timeout)
    channel.exec_command(cmd)
    if password:
        try:
            time.sleep(0.5)
            channel.sendall(f"{password}\n".encode())
        except Exception:
            pass
    try:
        raw = channel.makefile("rb").read()
        output = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
    except Exception:
        output = ""
    exit_code = channel.recv_exit_status()
    # Strip sudo prompt lines from output
    lines = output.strip().splitlines()
    cleaned = [l for l in lines if not l.strip().startswith("[sudo]")]
    return exit_code, "\n".join(cleaned).strip()


def _fetch_host_resources(ssh, password):
    """SSH into host and collect RAM, CPU, disk info."""
    resources = {}

    # RAM
    _, mem_out = _ssh_run(ssh, "free -b | grep Mem")
    if mem_out:
        parts = mem_out.split()
        try:
            resources["ram_total_gb"] = round(int(parts[1]) / (1024**3), 1)
            resources["ram_free_gb"] = round(int(parts[6]) / (1024**3), 1)
        except Exception:
            resources["ram_total_gb"] = 0
            resources["ram_free_gb"] = 0

    # CPU count
    _, cpu_out = _ssh_run(ssh, "nproc")
    try:
        resources["cpu_count"] = int(cpu_out.strip())
    except Exception:
        resources["cpu_count"] = 0

    # CPU allocated
    _, vcpu_out = _ssh_run(
        ssh,
        "for vm in $(virsh list --state-running --name 2>/dev/null); do "
        "virsh dominfo $vm 2>/dev/null | grep 'CPU(s)'; done | "
        "awk '{sum+=$2} END {print sum+0}'",
    )
    try:
        cpu_allocated = int(vcpu_out.strip())
    except Exception:
        cpu_allocated = 0
    resources["cpu_allocated"] = cpu_allocated
    resources["cpu_free"] = max(0, resources["cpu_count"] - cpu_allocated)

    # Disk
    _, disk_out = _ssh_run(ssh, f"df -B1 {VM_WORK_DIR} 2>/dev/null || df -B1 /home")
    if disk_out:
        lines = disk_out.strip().splitlines()
        if len(lines) >= 2:
            parts = lines[-1].split()
            try:
                resources["disk_total_gb"] = round(int(parts[1]) / (1024**3), 1)
                resources["disk_free_gb"] = round(int(parts[3]) / (1024**3), 1)
            except Exception:
                resources["disk_total_gb"] = 0
                resources["disk_free_gb"] = 0

    return resources


def _format_summary(resources):
    """Format host resources into HTML summary."""
    cpu_free = resources.get('cpu_free', resources.get('cpu_count', '?'))
    cpu_total = resources.get('cpu_count', '?')
    return (
        f"<strong>RAM:</strong> <span class='val'>{resources.get('ram_free_gb','?')} GB free / {resources.get('ram_total_gb','?')} GB total</span><br>"
        f"<strong>CPU:</strong> <span class='val'>{cpu_free} free / {cpu_total} total cores</span><br>"
        f"<strong>Disk:</strong> <span class='val'>{resources.get('disk_free_gb','?')} GB free / {resources.get('disk_total_gb','?')} GB total</span>"
    )


# ── Agent Mode Helper ─────────────────────────────────────────

def _run_agent_mode(b, config, state, session, ssh_client, plan):
    """Run the agentic loop for build b, pushing events into b's buffer.

    Runs inside the build's daemon thread. The agentic loop itself runs in
    a nested thread; both inherit the build's log routing context so
    installer output stays isolated per build.
    """
    import queue as _queue
    import threading as _th
    import contextvars

    sid = b["sid"]
    log_queue = b["queue"]

    def _emit_event(obj):
        _emit(b, obj)

    # ── Ensure SSH is alive before starting ──────────────────
    if not DRY_RUN:
        try:
            alive = (ssh_client and
                     ssh_client.get_transport() and
                     ssh_client.get_transport().is_active())
            if alive:
                ssh_client.get_transport().send_ignore()
                print(f"   [AGENT] SSH connection verified alive", flush=True)
            else:
                # SSH is dead — try to reconnect using the build's own creds
                print(f"   [AGENT] SSH connection dead — reconnecting...", flush=True)
                host_ip = b.get("host_ip")
                host_user = b.get("host_username", "ubuntu")
                host_pass = b.get("host_password", "")
                if host_ip and host_user and host_pass:
                    try:
                        new_ssh = _make_ssh(host_ip, host_user, host_pass)
                        b["ssh_client"] = new_ssh
                        state.set_output("host_ssh", new_ssh)
                        print(f"   [AGENT] SSH reconnected to {host_ip}", flush=True)
                        _emit_event({'type': 'log', 'line': f'[AGENT] SSH reconnected to {host_ip}'})
                    except Exception as e:
                        print(f"   [AGENT] SSH reconnect failed: {e}", flush=True)
                        _emit_event({'type': 'failed', 'tool': 'agent_loop', 'error': f'SSH connection failed: {e}.'})
                        b["stage"] = "done"
                        return
                else:
                    _emit_event({'type': 'failed', 'tool': 'agent_loop', 'error': 'No SSH credentials captured for this build.'})
                    b["stage"] = "done"
                    return
        except Exception as e:
            print(f"   [AGENT] SSH check error: {e}", flush=True)

    agent_event_queue = _queue.Queue(maxsize=500)
    agent_done = {"done": False, "error": None}

    def stream_callback(event_type, data):
        """Called by run_agentic_loop() to send events to the browser."""
        try:
            agent_event_queue.put_nowait((event_type, data))
        except _queue.Full:
            pass  # Drop event if queue is full

    def run_loop():
        try:
            orchestrator.run_agentic_loop(
                config, state,
                stream_callback=stream_callback,
                should_cancel=lambda: bool(b["cancel_requested"]),
            )
        except Exception as e:
            agent_event_queue.put_nowait(("agent_error", {"error": str(e)}))
        finally:
            agent_done["done"] = True

    # Start the agentic loop in a background thread, inheriting this build's
    # log routing context (threading.Thread does not propagate contextvars).
    _ctx = contextvars.copy_context()
    t = _th.Thread(target=lambda: _ctx.run(run_loop), daemon=True)
    t.start()

    iteration_count = 0

    # Consume events while the loop runs
    while not agent_done["done"] or not agent_event_queue.empty() or not log_queue.empty():
        # User cancelled? Stop cleanly
        if b["cancel_requested"]:
            _emit_event({'type': 'cancelled', 'msg': 'Build cancelled by user.'})
            b["stage"] = "done"
            return

        # Drain log queue (installer output)
        while True:
            try:
                line = log_queue.get_nowait()
                _emit_event({'type': 'log', 'line': line})
            except _queue.Empty:
                break

        # Drain agent event queue
        while True:
            try:
                event_type, data = agent_event_queue.get_nowait()

                if event_type == "agent_start":
                    _emit_event({'type': 'agent_start', 'vm_name': data.get('vm_name'), 'plan': data.get('plan', [])})

                elif event_type == "agent_tool_start":
                    tool = data.get("tool", "")
                    iteration = data.get("iteration", 0)
                    iteration_count = iteration
                    pct = min(int((iteration / 20) * 100), 95)
                    _emit_event({'type': 'running', 'tool': tool, 'step': iteration, 'attempt': 1})
                    _emit_event({'type': 'progress', 'percent': pct, 'step': iteration, 'total': len(plan), 'tool': tool})
                    _emit_event({'type': 'log', 'line': f'[AGENT] Calling {tool}...'})

                elif event_type == "agent_tool_done":
                    tool = data.get("tool", "")
                    iteration = data.get("iteration", 0)
                    _emit_event({'type': 'done', 'tool': tool, 'step': iteration, 'narration': ''})
                    _emit_event({'type': 'log', 'line': f'[AGENT] ✅ {tool} succeeded'})

                elif event_type == "agent_tool_failed":
                    tool = data.get("tool", "")
                    error = data.get("error", "")
                    _emit_event({'type': 'log', 'line': f'[AGENT] ⚠ {tool} failed: {error}'})
                    _emit_event({'type': 'log', 'line': '[AGENT] Claude is deciding how to recover...'})

                elif event_type == "agent_complete":
                    vm_ip = data.get("vm_ip", "unknown")
                    vm_mac = data.get("vm_mac", "")
                    vm_name = data.get("vm_name", config.vm_name)
                    _audit(sid, "vm_build_complete", f"vm={vm_name} ip={vm_ip} mode=agent")
                    _register_built_vm(b.get("host_ip"), vm_name)
                    _emit_event({'type': 'progress', 'percent': 100, 'step': len(plan), 'total': len(plan), 'tool': 'done'})
                    _emit_event({'type': 'complete', 'vm_name': vm_name, 'vm_ip': vm_ip, 'vm_mac': vm_mac, 'username': config.vm_username, 'vm_password': config.vm_password})
                    b["stage"] = "done"
                    return

                elif event_type == "agent_failed":
                    reason = data.get("reason", "unknown")
                    _audit(sid, "vm_build_failed", f"vm={config.vm_name} mode=agent reason={reason[:200]}")
                    _emit_event({'type': 'failed', 'tool': 'agent_loop', 'error': reason})
                    b["stage"] = "done"
                    return

                elif event_type == "agent_error":
                    error = data.get("error", "unknown")
                    _emit_event({'type': 'error', 'msg': f'Agent error: {error}'})
                    b["stage"] = "done"
                    return

                elif event_type == "agent_cancelled":
                    reason = data.get("reason", "Cancelled by user")
                    _emit_event({'type': 'cancelled', 'msg': reason})
                    b["stage"] = "done"
                    return

            except _queue.Empty:
                break

        time.sleep(0.3)

    # If loop ended without explicit complete/failed event, check state
    vm_ip = state.get_output("vm_ip", "unknown")
    if vm_ip and vm_ip != "unknown":
        vm_mac = state.get_output("vm_mac", "")
        _audit(sid, "vm_build_complete", f"vm={config.vm_name} ip={vm_ip} mode=agent")
        _register_built_vm(b.get("host_ip"), config.vm_name)
        _emit_event({'type': 'progress', 'percent': 100, 'step': len(plan), 'total': len(plan), 'tool': 'done'})
        _emit_event({'type': 'complete', 'vm_name': config.vm_name, 'vm_ip': vm_ip, 'vm_mac': vm_mac, 'username': config.vm_username, 'vm_password': config.vm_password})
    else:
        _emit_event({'type': 'failed', 'tool': 'agent_loop', 'error': 'Agent loop ended without completion'})
    b["stage"] = "done"


# ── Routes ────────────────────────────────────────────────────

@app.before_request
def _require_login():
    """Gate the whole app (UI + APIs) behind the login flow.

    The landing page itself is always served (it renders the login screen
    when the session is not authenticated); every /api/* endpoint requires
    an authenticated session, except /api/login and /api/logout.
    """
    if request.path in ("/", "/api/login", "/api/logout"):
        return None
    if not request.path.startswith("/api/"):
        return None
    sid = _get_session_id()
    session = _get_session(sid)
    if not session.get("authed"):
        return jsonify({"error": "Authentication required"}), 401
    return None


@app.route("/")
def index():
    _cleanup_stale_sessions()
    sid = _get_session_id()
    session = _get_session(sid)  # ensure session exists
    resp = make_response(render_template("index.html", dry_run=DRY_RUN, authed=bool(session.get("authed")), api_key_set=bool(API_KEY), username=session.get("username", ""), host_ip=session.get("host_ip", ""), session_token=sid))
    return _make_response_with_cookie(sid, resp)


@app.route("/api/login", methods=["POST"])
def api_login():
    """Connect with the KVM server credentials (server IP + SSH user + password).

    The same credentials that connect to the KVM host gate the app: on success
    the session is unlocked AND connected to the host in a single step.
    In DRY_RUN the host is simulated, so any server IP / user / password is
    accepted. On a real host, a bad password fails the SSH connection and the
    connect is rejected.
    """
    sid = _get_session_id()
    session = _get_session(sid)
    data = request.json or {}
    host_ip = (data.get("host_ip") or data.get("server_ip") or "").strip()
    username = (data.get("username") or "").strip()
    password = data.get("password", "")
    error, payload = _connect_to_host(sid, session, host_ip, username, password)
    if error:
        _audit(sid, "login_failed", f"{username}@{host_ip}")
        return jsonify({"error": error}), 401
    session["authed"] = True
    session["username"] = username
    _audit(sid, "login", f"{username}@{host_ip}")
    resp = make_response(jsonify({"ok": True, "username": username, "host_ip": host_ip, "session_token": sid, **payload}))
    return _make_response_with_cookie(sid, resp)


@app.route("/api/logout", methods=["POST"])
def api_logout():
    """Disconnect: drop the SSH session and lock the console again."""
    sid = _get_session_id()
    session = _get_session(sid)
    old_ssh = session.get("ssh_client")
    if old_ssh:
        try:
            old_ssh.close()
        except Exception:
            pass
    session["authed"] = False
    session.pop("ssh_client", None)
    session.pop("host_ip", None)
    session.pop("host_username", None)
    session.pop("host_password", None)
    resp = make_response(jsonify({"ok": True}))
    return _make_response_with_cookie(sid, resp)


def _connect_to_host(sid, session, host_ip, username, password):
    """Establish the KVM host SSH session (real or simulated).

    Returns (error, payload). On success payload carries the host resources
    summary; error is a human-readable message on failure.
    """
    if not host_ip or not username:
        return "Server IP and SSH username are required.", None

    if DRY_RUN:
        fake_resources = {
            "ram_total_gb": 128.0, "ram_free_gb": 112.0,
            "cpu_count": 32, "cpu_free": 28, "cpu_allocated": 4,
            "disk_total_gb": 2000.0, "disk_free_gb": 1400.0,
        }
        _seed_fake_vms(host_ip)
        session.update({
            "host_ip": host_ip, "host_username": username,
            "host_password": password, "host_resources": fake_resources,
            "ssh_client": _FakeSSH(host_ip),
            "stage": "connected",
        })
        return None, {"summary": _format_summary(fake_resources), "resources": fake_resources, "host_ip": host_ip}

    # Close existing SSH if any (fresh connect)
    old_ssh = session.get("ssh_client")
    if old_ssh:
        try:
            old_ssh.close()
        except Exception:
            pass

    # SSH connect
    try:
        ssh = _make_ssh(host_ip, username, password)
    except Exception as e:
        return f"SSH connection failed: {e}", None

    print(f"   [session {sid[:8]}] SSH connected to {host_ip}", flush=True)

    # Ensure working directory
    try:
        _ssh_run(ssh, f"sudo -S mkdir -p {VM_WORK_DIR}", password, timeout=10)
    except Exception as e:
        print(f"   mkdir warning: {e}")

    # Fetch resources
    try:
        resources = _fetch_host_resources(ssh, password)
        print(f"   [session {sid[:8]}] Fetched resources: {resources}")
    except Exception as e:
        traceback.print_exc()
        ssh.close()
        return f"Failed to fetch host info: {e}", None

    # Background package install (per-session, non-blocking)
    def _bg_install():
        try:
            _ssh_run(
                ssh,
                f"sudo -S apt-get update -qq && sudo -S apt-get install -y -qq {KVM_APT_PACKAGES}",
                password,
                timeout=600,
            )
            print(f"   [session {sid[:8]}] Background package install finished.")
        except Exception as e:
            print(f"   [session {sid[:8]}] Background install warning: {e}")

    threading.Thread(target=_bg_install, daemon=True).start()

    # Update session state
    session.update({
        "host_ip": host_ip,
        "host_username": username,
        "host_password": password,
        "host_resources": resources,
        "ssh_client": ssh,
        "stage": "connected",
    })

    return None, {"summary": _format_summary(resources), "resources": resources}


@app.route("/api/connect", methods=["POST"])
def api_connect():
    """Switch / reconnect the KVM host (also connected at sign-in)."""
    sid = _get_session_id()
    session = _get_session(sid)
    try:
        data = request.json or {}
        host_ip = data.get("host_ip", "").strip()
        username = data.get("username", "").strip()
        password = data.get("password", "")
        error, payload = _connect_to_host(sid, session, host_ip, username, password)
        if error:
            return jsonify({"error": error}), 400
        resp = make_response(jsonify(payload))
        return _make_response_with_cookie(sid, resp)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"Unexpected server error: {e}"}), 500


@app.route("/api/execute")
def api_execute():
    """Step 2: Stream tool execution via Server-Sent Events."""
    sid = _get_session_id()
    session = _get_session(sid)
    data = request.args

    # Build config from form data
    config = VMConfig()
    config.vm_name = data.get("vm_name", "").strip()
    config.vm_username = data.get("vm_username", "ubuntu").strip() or "ubuntu"
    config.vm_password = data.get("vm_password", "").strip()
    config.vm_type = data.get("vm_type", "normal").strip()
    config.acs_state = data.get("acs_state", "").strip() or None
    config.debug = data.get("debug", "enable").strip()
    config.os_image = data.get("os_image", "").strip() or None
    config.disk_path = data.get("disk_path", "").strip() or None

    try:
        config.memory_gb = int(data.get("memory_gb", 0))
    except ValueError:
        config.memory_gb = None
    try:
        config.num_cpu = int(data.get("num_cpu", 0))
    except ValueError:
        config.num_cpu = None

    disk_size = data.get("disk_size", "").strip()
    if disk_size and disk_size[-1].upper() not in ("G", "M", "T"):
        disk_size = disk_size + "G"
    config.disk_size = disk_size if disk_size else "100G"

    try:
        aic_val = data.get("aic_cards", "").strip()
        config.aic_cards = int(aic_val) if aic_val else None
    except ValueError:
        config.aic_cards = None

    # Validate
    ok, errors = config_validator.validate(config)
    if not ok:
        def err_gen():
            yield f"data: {json.dumps({'type': 'error', 'msg': ', '.join(errors)})}\n\n"
        return Response(err_gen(), mimetype="text/event-stream")

    # Per-request agent mode override (from UI toggle)
    # This overrides the global AGENT_MODE setting from settings.py
    request_agent_mode = data.get("agent_mode", "").strip()
    if request_agent_mode == "1":
        effective_agent_mode = True
    elif request_agent_mode == "0":
        effective_agent_mode = False
    else:
        effective_agent_mode = AGENT_MODE  # fall back to global setting

    session["config"] = config
    session["stage"] = "running"
    _audit(sid, "vm_build_start", f"vm={config.vm_name} agent={effective_agent_mode}")

    plan = orchestrator.build_plan(config)
    b = _new_build(sid, config, effective_agent_mode, plan)
    b["config"] = config
    # Snapshot the host this build was started against. The build owns its
    # own SSH connection, so disconnecting or switching servers in the UI
    # never kills it — or worse, redirects it to a different host.
    b["host_ip"] = session.get("host_ip")
    b["host_username"] = session.get("host_username")
    b["host_password"] = session.get("host_password")
    # Optional per-build email notification address ("me@corp.com").
    notify_email = (data.get("notify_email") or "").strip()
    if notify_email and re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", notify_email) and len(notify_email) <= 200:
        b["notify_email"] = notify_email
    _register_build(b)

    def generate():
        yield f"data: {json.dumps({'type': 'build_id', 'build_id': b['id'], 'vm_name': config.vm_name, 'host_ip': b.get('host_ip')})}\n\n"

        import contextvars as _cv
        _ctx = _cv.copy_context()
        threading.Thread(target=lambda: _ctx.run(_run_build_thread, b["id"]), daemon=True).start()

        # New build: buffer starts empty, so replay=True is identical to
        # watching live but avoids a race where the thread's first events
        # could be emitted before the iterator's cursor is set.
        for obj in _iter_build_events(b, replay=True):
            yield f"data: {json.dumps(obj)}\n\n"

    return Response(generate(), mimetype="text/event-stream")

def _run_build_thread(build_id):
    """Run a VM build to completion in its own daemon thread.

    Each build gets its own log file + stream queue (via contextvars) and
    its own event buffer, so multiple builds run concurrently and each
    session can watch/cancel them independently.
    """
    with _BUILDS_LOCK:
        b = _BUILDS.get(build_id)
    if b is None:
        return
    try:
        try:
            from tools.installer_autopilot import _set_log_file, _set_log_stream_queue, _init_log
            _set_log_file(f"{b['vm_name']}.txt")
            _set_log_stream_queue(b["queue"])
            # Create the log file up front (with a header) so the in-UI log
            # viewer, tail endpoint and download all work even when a build
            # emits no streamed lines (e.g. dry-run).
            _init_log()
        except Exception:
            pass
        _run_build_plan(b)
    except Exception as e:
        _emit(b, {"type": "error", "msg": f"Build thread error: {e}"})
    finally:
        b["done"] = True
        if b["stage"] == "running":
            b["stage"] = "done"
        try:
            if b.get("ssh_client"):
                b["ssh_client"].close()
        except Exception:
            pass
        try:
            from tools.installer_autopilot import _set_log_stream_queue
            _set_log_stream_queue(None)
        except Exception:
            pass


def _run_build_plan(b):
    """Execute the classic plan for build b (agent mode dispatched from here)."""
    from tools import TOOL_REGISTRY
    from settings import MAX_RETRIES
    import queue as _queue
    import contextvars as _cv

    config = b["config"]
    sid = b["sid"]
    session = _get_session(sid)
    run_agentic = bool(b["agent_mode"]) and bool(ANTHROPIC_API_KEY)

    plan = b["plan"]
    state = AgentState()
    state.set_plan(plan)

    # Each build owns a dedicated SSH connection to the host it was started
    # on, so disconnecting or switching servers in the UI never kills the
    # build or redirects it to a different host.
    ssh_client = b.get("ssh_client")
    if ssh_client is None and not DRY_RUN:
        try:
            ssh_client = _make_ssh(b.get("host_ip"), b.get("host_username", "ubuntu"), b.get("host_password", ""))
            b["ssh_client"] = ssh_client
            _emit(b, {"type": "log", "line": f"[build] SSH session to {b.get('host_ip')} opened"})
        except Exception as e:
            _emit(b, {"type": "failed", "tool": "ssh", "error": f"Could not open build SSH session to {b.get('host_ip')}: {e}"})
            b["stage"] = "done"
            return
    if ssh_client and not DRY_RUN:
        state.set_output("host_ssh", ssh_client)
    state.set_output("host_password", b.get("host_password", ""))
    state.set_output("host_ip", b.get("host_ip", ""))
    state.set_output("host_username", b.get("host_username", "ubuntu"))

    if b["agent_mode"] and not ANTHROPIC_API_KEY:
        _emit(b, {"type": "log", "line": "⚠ No ANTHROPIC_API_KEY set — agent mode unavailable, falling back to the classic plan."})
        _emit(b, {"type": "log", "line": "⚠ Set ANTHROPIC_API_KEY to enable the agentic loop."})

    print(f"\n{'='*60}", flush=True)
    mode_label = "AGENT" if run_agentic else "CLASSIC"
    print(f"[EXECUTE][{sid[:8]}][{mode_label}] Starting for VM '{config.vm_name}'", flush=True)
    print(f"{'='*60}", flush=True)

    _emit(b, {"type": "start", "total": len(plan), "plan": plan, "agent_mode": run_agentic})

    # ─── AGENT MODE: Looping agentic loop ────────────────
    if run_agentic:
        _run_agent_mode(b, config, state, session, ssh_client, plan)
        return

    # ─── CLASSIC MODE: Hardcoded plan ────────────────────
    def _drain_log_queue():
        """Drain pending installer log lines into the build's buffer."""
        while True:
            try:
                _emit(b, {"type": "log", "line": b["queue"].get_nowait()})
            except _queue.Empty:
                break

    def _ensure_ssh_alive():
        """Check if the build's SSH is still alive, reconnect if needed."""
        nonlocal ssh_client
        try:
            if ssh_client and ssh_client.get_transport() and ssh_client.get_transport().is_active():
                ssh_client.get_transport().send_ignore()
                return
        except Exception:
            pass
        print(f"   [build {b['id']}] [SSH] Reconnecting to {b.get('host_ip')}...", flush=True)
        try:
            pw = b.get("host_password", "")
            usr = b.get("host_username", "ubuntu")
            host = b.get("host_ip")
            ssh_client = _make_ssh(host, usr, pw)
            b["ssh_client"] = ssh_client
            state.set_output("host_ssh", ssh_client)
            print(f"   [build {b['id']}] [SSH] Reconnected!", flush=True)
            _emit(b, {"type": "log", "line": f"[build] SSH reconnected to {host}"})
        except Exception as e:
            print(f"   [build {b['id']}] [SSH] Reconnect failed: {e}", flush=True)
            _emit(b, {"type": "log", "line": f"[build] ⚠ SSH reconnect failed: {e}"})

    try:
        for idx, tool_name in enumerate(plan, 1):
            if b["cancel_requested"]:
                _emit(b, {"type": "cancelled", "msg": "Build cancelled by user. In-flight operations may still be finishing on the host."})
                b["stage"] = "done"
                return

            if not DRY_RUN:
                _ensure_ssh_alive()

            fn = TOOL_REGISTRY.get(tool_name)
            if fn is None:
                _emit(b, {"type": "error", "tool": tool_name, "msg": "not found"})
                break

            state.mark_running(tool_name)
            pct = int((idx - 1) / len(plan) * 100)
            _emit(b, {"type": "progress", "percent": pct, "step": idx, "total": len(plan), "tool": tool_name})

            # Run tool in a background thread so we can drain logs while it runs
            import threading as _th
            tool_result_holder = {"result": None, "done": False}

            def _run_tool():
                try:
                    tool_result_holder["result"] = fn(config, state)
                except Exception as _e:
                    tool_result_holder["result"] = {"status": "failed", "error": str(_e), "data": None}
                finally:
                    tool_result_holder["done"] = True

            result = None
            for attempt in range(1, MAX_RETRIES + 1):
                print(f"\n[{sid[:8]}][{idx}/{len(plan)}] ▶ {tool_name} (attempt {attempt}/{MAX_RETRIES})", flush=True)
                _emit(b, {"type": "running", "tool": tool_name, "step": idx, "attempt": attempt})

                tool_result_holder["done"] = False
                tool_result_holder["result"] = None
                _ctx = _cv.copy_context()
                t = _th.Thread(target=lambda: _ctx.run(_run_tool), daemon=True)
                t.start()

                # While tool runs, drain log queue and emit log events
                while not tool_result_holder["done"]:
                    if b["cancel_requested"]:
                        break
                    _drain_log_queue()
                    time.sleep(0.3)
                # Final drain
                _drain_log_queue()

                if b["cancel_requested"]:
                    _emit(b, {"type": "cancelled", "msg": "Build cancelled by user. In-flight operations may still be finishing on the host."})
                    b["stage"] = "done"
                    return

                result = tool_result_holder["result"] or {"status": "failed", "error": "no result"}

                if result["status"] == "success":
                    state.mark_done(tool_name)
                    state.record_result(tool_name, result)
                    print(f"[{sid[:8]}][{idx}/{len(plan)}] ✅ {tool_name}", flush=True)

                    commentary = orchestrator._narrate_step(
                        tool_name, result, f"{idx}/{len(plan)} done", config
                    )
                    _emit(b, {"type": "done", "tool": tool_name, "step": idx, "narration": commentary or ""})
                    break
                else:
                    print(f"[{sid[:8]}][{idx}/{len(plan)}] ⚠ {tool_name} FAILED: {result.get('error','')}", flush=True)
                    _emit(b, {"type": "retry", "tool": tool_name, "attempt": attempt, "error": result.get("error", "")})
                    if attempt < MAX_RETRIES:
                        time.sleep(2)
                    else:
                        state.mark_failed(tool_name, result["error"])
                        print(f"[{sid[:8]}][{idx}/{len(plan)}] ❌ {tool_name} — giving up", flush=True)
                        _audit(sid, "vm_build_failed", f"vm={config.vm_name} tool={tool_name} err={result.get('error','')[:200]}")

                        # ─── Auto-rollback: clean up partial VM ───
                        if not DRY_RUN and idx > 1:
                            try:
                                _emit(b, {"type": "log", "line": f"⚠ Auto-rollback: cleaning up partial VM {config.vm_name}..."})
                                _ssh_run(ssh_client, f"sudo -S virsh destroy {config.vm_name} 2>/dev/null || true", password=b.get('host_password', ''))
                                time.sleep(1)
                                _ssh_run(ssh_client, f"sudo -S virsh undefine {config.vm_name} --remove-all-storage 2>/dev/null || true", password=b.get('host_password', ''))
                                _ssh_run(ssh_client, f"sudo -S rm -f {VM_WORK_DIR}/{config.vm_name}_pci*.xml 2>/dev/null", password=b.get('host_password', ''))
                                _emit(b, {"type": "log", "line": "✅ Rollback complete"})
                                _audit(sid, "auto_rollback", f"vm={config.vm_name}")
                            except Exception as _e:
                                _emit(b, {"type": "log", "line": f"⚠ Rollback error: {_e}"})

                        _emit(b, {"type": "failed", "tool": tool_name, "error": result.get("error", "")})
                        b["stage"] = "done"
                        return

        # All tools completed successfully
        vm_ip = state.get_output("vm_ip", "unknown")
        vm_mac = state.get_output("vm_mac", "")
        print(f"\n{'='*60}", flush=True)
        print(f"[{sid[:8]}][COMPLETE] VM '{config.vm_name}' ready — IP: {vm_ip} MAC: {vm_mac}", flush=True)
        print(f"{'='*60}\n", flush=True)
        _audit(sid, "vm_build_complete", f"vm={config.vm_name} ip={vm_ip}")
        _register_built_vm(b.get("host_ip"), config.vm_name)
        _emit(b, {"type": "progress", "percent": 100, "step": len(plan), "total": len(plan), "tool": "done"})
        _emit(b, {"type": "complete", "vm_name": config.vm_name, "vm_ip": vm_ip, "vm_mac": vm_mac, "username": config.vm_username, "vm_password": config.vm_password})
        b["stage"] = "done"
    finally:
        # Ensure the stream is marked finished even on early exit
        b["done"] = True
        if b["stage"] == "running":
            b["stage"] = "done"


def _collect_vm_inventory(ssh, password=""):
    """Read the host's VM inventory (name, state, specs) via virsh.

    Shared by /api/list_vms and the AI chat assistant so both always
    see the same live picture of the host.
    """
    vm_list = []
    try:
        _, output = _ssh_run(ssh, "sudo -S virsh list --all --name 2>/dev/null", password=password)
    except Exception:
        return vm_list
    vms = [name.strip() for name in output.splitlines() if name.strip()]

    for name in vms:
        try:
            _, state_out = _ssh_run(ssh, f"sudo -S virsh domstate {name} 2>/dev/null", password=password)
            state = state_out.strip() or "unknown"


            specs = {"vcpus": "?", "memory": "?", "disk": "?"}
            try:
                _, info_out = _ssh_run(ssh, f"sudo -S virsh dominfo {name} 2>/dev/null", password=password)
                for line in info_out.splitlines():
                    if "CPU(s):" in line:
                        specs["vcpus"] = line.split(":")[-1].strip()
                    elif "Max memory:" in line or "Used memory:" in line:
                        mem_kb = line.split(":")[-1].strip().replace("KiB", "").replace("kB", "").replace(",", "").strip()
                        try:
                            mem_gb = round(int(mem_kb) / 1024 / 1024, 1)
                            specs["memory"] = f"{mem_gb} GB"
                        except ValueError:
                            specs["memory"] = mem_kb
                _, if_list = _ssh_run(ssh, f"sudo -S virsh domiflist {name} 2>/dev/null", password=password)
                nics = []
                for iline in if_list.splitlines():
                    itok = iline.strip().split()
                    if itok and re.match(r"^(vnet|macvtap|tap|eth|ens|enp|br)", itok[0]):
                        imac = next((t for t in itok[1:] if re.match(r"^[0-9a-fA-F:]{17}$", t)), "?")
                        nics.append({"iface": itok[0], "mac": imac})
                specs["nics"] = nics

                # Live IP (only reported by domifaddr while the VM is running)
                _, net_out = _ssh_run(ssh, f"sudo -S virsh domifaddr {name} 2>/dev/null", password=password)
                if net_out.strip():
                    for line in net_out.splitlines():
                        m = re.search(r"([0-9a-fA-F:]{17})\s+(\w+)\s+(\d+\.\d+\.\d+\.\d+)/\d+", line)
                        if m:
                            specs["mac"] = m.group(1)
                            specs["protocol"] = m.group(2)
                            specs["ip"] = m.group(3)
                            specs["ip_source"] = "live"
                            break

                # Fallback for stopped VMs: the host still holds the last DHCP
                # lease for each NIC MAC, so match MACs against net-dhcp-leases.
                if not specs.get("ip"):
                    try:
                        _, nets_out = _ssh_run(ssh, "sudo -S virsh net-list --all 2>/dev/null", password=password)
                        nets = []
                        for nl in nets_out.splitlines():
                            nt = nl.split()
                            if nt and nt[0].lower() != "name" and not nt[0].startswith("-"):
                                nets.append(nt[0])
                        mac_to_ip = {}
                        for net in nets:
                            _, leases_out = _ssh_run(ssh, f"sudo -S virsh net-dhcp-leases {net} 2>/dev/null", password=password)
                            for lline in leases_out.splitlines():
                                lm = re.search(r"([0-9a-fA-F:]{17})\s+\S+\s+([\d.]+)", lline)
                                if lm:
                                    mac_to_ip[lm.group(1).lower()] = lm.group(2)
                        for nic in nics:
                            if nic["mac"] != "?" and mac_to_ip.get(nic["mac"].lower()):
                                specs["mac"] = nic["mac"]
                                specs["ip"] = mac_to_ip[nic["mac"].lower()]
                                specs["ip_source"] = "lease"
                                break
                    except Exception:
                        pass

                _, xml_out = _ssh_run(ssh, f"sudo -S virsh dumpxml {name} 2>/dev/null", password=password)
                pci_devs = []
                for hb in re.findall(r"<hostdev\b.*?</hostdev>", xml_out, re.S):
                    am = re.search(r"domain='(0x[0-9a-fA-F]+)'\s+bus='(0x[0-9a-fA-F]+)'\s+slot='(0x[0-9a-fA-F]+)'\s+function='(0x[0-9a-fA-F]+)'", hb)
                    if am:
                        sbdf = f"{int(am.group(1),16):04x}:{int(am.group(2),16):02x}:{int(am.group(3),16):02x}.{int(am.group(4),16)}"
                        pci_devs.append(sbdf)
                specs["pci_devices"] = pci_devs

                # Live utilization: CPU delta%, RAM used, network totals
                try:
                    _, stats_out = _ssh_run(ssh, f"sudo -S virsh domstats {name} 2>/dev/null", password=password)
                    st = {}
                    for sline in stats_out.splitlines():
                        if "=" in sline:
                            k, _, v = sline.partition("=")
                            st[k.strip()] = v.strip()
                    stats = {}
                    now = time.time()
                    cpu_time = st.get("cpu.time")
                    if cpu_time is not None:
                        try:
                            cur = int(cpu_time)
                            prev = _CPU_SAMPLES.get(name)
                            if prev:
                                d_cpu = cur - prev[1]
                                d_t = now - prev[0]
                                if d_t > 0 and d_cpu >= 0:
                                    stats["cpu_pct"] = round(min(100.0, d_cpu / (d_t * 1e9) * 100), 1)
                            _CPU_SAMPLES[name] = (now, cur)
                        except (ValueError, TypeError):
                            pass
                    balloon = st.get("balloon.current")
                    maximum = st.get("balloon.maximum")
                    if balloon is not None and maximum:
                        try:
                            stats["mem_used_gb"] = round(int(balloon) / 1024 / 1024 / 1024, 1)
                            stats["mem_pct"] = round(int(balloon) / int(maximum) * 100, 1)
                        except (ValueError, ZeroDivisionError):
                            pass
                    rx = sum(int(v) for k, v in st.items() if k.endswith(".rx.bytes") and v.isdigit())
                    tx = sum(int(v) for k, v in st.items() if k.endswith(".tx.bytes") and v.isdigit())
                    if rx or tx:
                        stats["net_rx_mb"] = round(rx / 1048576, 1)
                        stats["net_tx_mb"] = round(tx / 1048576, 1)
                    specs["stats"] = stats
                except Exception:
                    specs["stats"] = {}

                disk_target = "vda"
                _, blk_list = _ssh_run(ssh, f"sudo -S virsh domblklist {name} 2>/dev/null", password=password)
                for bline in blk_list.splitlines():
                    btok = bline.strip().split()
                    if btok and re.match(r"^(vd|sd|xvd|nvme)", btok[0]):
                        disk_target = btok[0]
                        break
                _, blk_out = _ssh_run(ssh, f"sudo -S virsh domblkinfo {name} {disk_target} --human 2>/dev/null", password=password)
                if blk_out.strip():
                    for line in blk_out.splitlines():
                        if "Capacity:" in line:
                            cap_str = line.split(":")[-1].strip()
                            try:
                                parts_cap = cap_str.split()
                                val = float(parts_cap[0])
                                unit = parts_cap[1] if len(parts_cap) > 1 else "GiB"
                                if "GiB" in unit or "GB" in unit:
                                    specs["disk"] = f"{int(val)} GB"
                                elif "TiB" in unit or "TB" in unit:
                                    specs["disk"] = f"{int(val * 1024)} GB"
                                elif "MiB" in unit or "MB" in unit:
                                    specs["disk"] = f"{round(val / 1024, 1)} GB"
                                else:
                                    specs["disk"] = cap_str
                            except (ValueError, IndexError):
                                specs["disk"] = cap_str
                            break
            except Exception:
                pass

            vm_list.append({"name": name, "state": state, "specs": specs})
        except Exception:
            vm_list.append({"name": name, "state": "unknown", "specs": {"vcpus": "?", "memory": "?", "disk": "?"}})

    return vm_list


_CLONE_META = {}  # vm_name -> {vm_username, vm_password, vm_type, os_image, ...}
_CLONE_META_LOCK = threading.Lock()


def _vm_build_meta(vm_name):
    """Return build-time metadata for a VM built in this process.

    P2P type, ACS state, OS image and requested AIC cards are not visible
    to virsh — they only exist in the build config. Looked up by VM name
    so the chat can answer "is it P2P / which image / how many AIC cards",
    and so clones can inherit the source's recorded credentials.
    """
    with _BUILDS_LOCK:
        for b in _BUILDS.values():
            cfg = b.get("config")
            if cfg and getattr(cfg, "vm_name", None) == vm_name:
                return {
                    "vm_type": getattr(cfg, "vm_type", "normal"),
                    "acs_state": getattr(cfg, "acs_state", None),
                    "os_image": getattr(cfg, "os_image", None),
                    "aic_cards": getattr(cfg, "aic_cards", None),
                    "disk_size": getattr(cfg, "disk_size", None),
                    "memory_gb": getattr(cfg, "memory_gb", None),
                    "num_cpu": getattr(cfg, "num_cpu", None),
                    "vm_username": getattr(cfg, "vm_username", None),
                    "vm_password": getattr(cfg, "vm_password", None),
                }
    with _CLONE_META_LOCK:
        if vm_name in _CLONE_META:
            return dict(_CLONE_META[vm_name])
    return None


@app.route("/api/list_vms", methods=["GET"])
def api_list_vms():
    """List all VMs on the connected host with specs."""
    sid = _get_session_id()
    session = _get_session(sid)
    if session.get("ssh_client") is None:
        return jsonify({"error": "Not connected to host"}), 400
    try:
        return jsonify({"vms": _collect_vm_inventory(session["ssh_client"], session.get("host_password", ""))})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"Failed to list VMs: {e}"}), 500


def _chat_fallback(message, vms, resources):
    """Rule-based answers when no ANTHROPIC_API_KEY is configured.

    Keeps the chat usable in dry-run/demo mode and as a safety net
    whenever Claude is unavailable.
    """
    m = (message or "").lower()
    if not vms:
        if resources:
            return (
                f"Connected to the host, but no VMs are defined yet. The host has "
                f"{resources.get('ram_total_gb')} GB RAM, {resources.get('cpu_count')} cores "
                f"and {resources.get('disk_total_gb')} GB disk. You can build one from the "
                "Configure VM step."
            )
        return "You're not connected to a KVM host yet — connect from step 1 first, then I can tell you about your VMs."

    def _vm_summary(v):
        s = v.get("specs", {})
        meta = _vm_build_meta(v["name"])
        ip = f", IP {s.get('ip')}" if s.get("ip") else ""
        nics = s.get("nics") or []
        pci = s.get("pci_devices") or []
        bits = [
            f"{v['name']} is **{v['state']}** — {s.get('vcpus', '?')} vCPUs, "
            f"{s.get('memory', '?')} RAM, {s.get('disk', '?')} disk{ip}",
            f"{len(nics)} NIC(s): {', '.join(n['iface'] for n in nics) or 'none'}",
            f"{len(pci)} PCI/AIC device(s): {', '.join(pci) or 'none'}",
        ]
        if meta:
            bits.append(f"type: **{meta['vm_type']}**" + (" (P2P)" if meta["vm_type"] == "p2p" else ""))
            if meta.get("os_image"):
                bits.append(f"OS image: {meta['os_image']}")
            if meta.get("aic_cards"):
                bits.append(f"requested AIC cards: {meta['aic_cards']}")
        st = s.get("stats") or {}
        if st.get("cpu_pct") is not None or st.get("mem_pct") is not None:
            bits.append(f"live: CPU {st.get('cpu_pct', '?')}%, RAM {st.get('mem_pct', '?')}% used")
        return " — ".join(bits)

    for v in vms:
        if v["name"].lower() in m:
            s = v.get("specs", {})
            meta = _vm_build_meta(v["name"])
            nics = s.get("nics") or []
            pci = s.get("pci_devices") or []
            if "nic" in m or "interface" in m or "port" in m:
                return f"{v['name']} has {len(nics)} NIC(s): {', '.join(n['iface'] + ' (' + n['mac'] + ')' for n in nics) or 'none'}."
            if "p2p" in m or "peer" in m:
                if meta:
                    return f"{v['name']} is {'**P2P**' if meta['vm_type'] == 'p2p' else '**not P2P**'}" + (f" (ACS state: {meta['acs_state']})" if meta.get("acs_state") else "") + "."
                return f"{v['name']} was not built through this app session, so I can't tell its P2P type — P2P is set at build time and isn't visible to virsh."
            if "sdk" in m:
                if meta and meta.get("os_image"):
                    return f"I don't track an SDK version inside the VM — I know it was built from OS image **{meta['os_image']}**. An in-VM SDK check needs a command to run inside the guest."
                return f"I don't track an SDK version inside the VM — that would need a check run inside the guest (e.g. via SSH). I can tell you the OS image if it was built through this app."
            if "aic" in m or "pci" in m or "gpu" in m or "card" in m or "device" in m:
                return f"{v['name']} has {len(pci)} PCI/AIC device(s): {', '.join(pci) or 'none'}." + (f" ({meta['aic_cards']} AIC cards were requested at build time)" if meta and meta.get("aic_cards") else "")
            if "os" in m or "image" in m or "sdk" in m:
                if meta and meta.get("os_image"):
                    return f"{v['name']} was built from OS image **{meta['os_image']}**."
                return f"{v['name']}'s OS image isn't recorded — it was likely created outside this app session."
            return _vm_summary(v)

    if "nic" in m or "interface" in m:
        lines = [f"{v['name']}: {len(v.get('specs', {}).get('nics') or [])} NIC(s)" for v in vms]
        return "NIC counts —\n" + "\n".join(lines)
    if "aic" in m or "pci" in m or "gpu" in m or "card" in m:
        lines = [f"{v['name']}: {len(v.get('specs', {}).get('pci_devices') or [])} device(s)" for v in vms]
        return "PCI/AIC devices —\n" + "\n".join(lines)
    if "busy" in m or "usage" in m or "util" in m or "load" in m or "live stats" in m:
        lines = []
        for v in vms:
            st = v.get("specs", {}).get("stats") or {}
            if st.get("cpu_pct") is not None or st.get("mem_pct") is not None:
                lines.append(f"{v['name']}: CPU {st.get('cpu_pct', '?')}%, RAM {st.get('mem_pct', '?')}% used ({st.get('mem_used_gb', '?')} GB)")
            else:
                lines.append(f"{v['name']}: no live stats (VM shut off?)")
        return "Live utilization —\n" + "\n".join(lines)
    if "p2p" in m or "peer" in m:
        p2p_vms = [v["name"] for v in vms if (_vm_build_meta(v["name"]) or {}).get("vm_type") == "p2p"]
        return f"P2P VMs (built through this app): {', '.join(p2p_vms) or 'none'}. P2P type is set at build time and isn't visible to virsh."
    if "sdk" in m:
        return "I don't track SDK versions inside VMs — that requires a check run inside the guest. I can report each VM's state, IP, specs, NICs and PCI/AIC devices, and the OS image for VMs built through this app."
    if "running" in m:
        run = [v["name"] for v in vms if v["state"] == "running"]
        return f"Running VMs: {', '.join(run) if run else 'none'}."
    if "shut" in m or "stop" in m or "off" in m:
        off = [v["name"] for v in vms if v["state"] != "running"]
        return f"Shut-off VMs: {', '.join(off) if off else 'none'}."
    if "ip" in m or "address" in m:
        lines = [f"{v['name']}: {v.get('specs', {}).get('ip', 'no IP yet')}" for v in vms]
        return "VM IPs —\n" + "\n".join(lines)
    if "host" in m or "resource" in m or "ram" in m or "cpu" in m or "disk" in m or "memory" in m:
        if resources:
            return (
                f"Host resources: {resources.get('ram_total_gb')} GB RAM, "
                f"{resources.get('cpu_count')} cores, {resources.get('disk_total_gb')} GB disk total."
            )
    names = ", ".join(v["name"] for v in vms)
    return (
        f"I can see {len(vms)} VM(s) on this host: {names}. Ask me about their state, IP, "
        "or specs — or about host resources."
    )


@app.route("/api/chat", methods=["POST"])
def api_chat():
    """AI assistant: answer any question about the host / its VMs using Claude.

    Context (live inventory + host resources) is gathered from the same
    virsh calls the UI uses, so answers reflect the real host state. When
    ANTHROPIC_API_KEY is not set, a small rule-based assistant answers
    instead so the chat still works in demo mode.
    """
    sid = _get_session_id()
    session = _get_session(sid)
    if not session.get("authed"):
        return jsonify({"error": "Not authenticated"}), 401
    data = request.json or {}
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"error": "Message is required"}), 400
    if len(message) > 2000:
        return jsonify({"error": "Message too long"}), 400

    ssh = session.get("ssh_client")
    password = session.get("host_password", "")
    host_ip = session.get("host_ip") or "unknown"

    context = f"Connected KVM host: {host_ip}\n"
    vms, resources = [], {}
    if ssh is not None:
        vms = _collect_vm_inventory(ssh, password)
        try:
            resources = _fetch_host_resources(ssh, password)
        except Exception:
            resources = {}
    else:
        context += "No host connected yet.\n"

    if vms:
        context += "VM inventory (name | state | vcpus | memory | disk | ip | nics | pci devices | build metadata):\n"
        for v in vms:
            s = v.get("specs", {})
            nics = s.get("nics") or []
            pci = s.get("pci_devices") or []
            meta = _vm_build_meta(v["name"])
            extra = ""
            if meta:
                extra = (
                    f" | type: {meta['vm_type']}"
                    f"{' p2p' if meta['vm_type'] == 'p2p' else ''}"
                    f"{' | acs: ' + meta['acs_state'] if meta.get('acs_state') else ''}"
                    f"{' | os_image: ' + meta['os_image'] if meta.get('os_image') else ''}"
                    f"{' | aic_cards: ' + str(meta['aic_cards']) if meta.get('aic_cards') else ''}"
                )
            stats = s.get("stats") or {}
            stats_txt = ""
            if stats.get("cpu_pct") is not None or stats.get("mem_pct") is not None:
                stats_txt = (
                    f" | cpu: {stats.get('cpu_pct', '?')}%"
                    f" mem: {stats.get('mem_pct', '?')}% used"
                )
            context += (
                f"- {v['name']} | {v['state']} | {s.get('vcpus', '?')} vcpu | "
                f"{s.get('memory', '?')} | {s.get('disk', '?')} | {s.get('ip', 'no IP')} | "
                f"nics: {len(nics)} ({', '.join(n['iface'] for n in nics) or 'none'}) | "
                f"pci devices: {', '.join(pci) or 'none'}{stats_txt}{extra}\n"
            )
    else:
        context += "No VMs found on the host.\n"

    if resources:
        context += (
            f"Host resources: {resources.get('ram_total_gb')} GB RAM, "
            f"{resources.get('cpu_count')} cores, {resources.get('disk_total_gb')} GB disk total.\n"
        )

    used_llm = bool(ANTHROPIC_API_KEY)
    answer = _chat_fallback(message, vms, resources)
    if used_llm:
        prompt = (
            "You are the assistant inside VM Agent, a KVM/Ubuntu VM provisioning tool.\n"
            "Answer the user's question using ONLY the live host context below. Be concise "
            "(2-5 sentences), factual and friendly. If the answer is not in the context, say "
            "what data is available.\n\n"
            f"CONTEXT:\n{context}\n\n"
            f"USER QUESTION: {message}"
        )
        try:
            answer = llm_client.ask(prompt, max_tokens=600, temperature=0.2)
        except Exception as e:
            answer = _chat_fallback(message, vms, resources) + f"\n\n(Claude unavailable: {e})"

    _audit(sid, "chat", f"len={len(message)} used_llm={used_llm}")
    return jsonify({"answer": answer, "used_llm": used_llm})


@app.route("/api/toggle_vm", methods=["POST"])
def api_toggle_vm():
    """Start or stop a VM on the connected host."""
    import traceback
    sid = _get_session_id()
    session = _get_session(sid)
    try:
        data = request.json or {}
        vm_name = data.get("vm_name", "").strip()
        action = data.get("action", "").strip()
        if not vm_name or action not in ("shutdown", "start"):
            return jsonify({"error": "vm_name and action (shutdown/start) required"}), 400

        ssh = session.get("ssh_client")
        if ssh is None:
            return jsonify({"error": "Not connected to host"}), 400

        password = session.get("host_password", "")

        if action == "shutdown":
            exit_code, output = _ssh_run(ssh, f"sudo -S virsh shutdown {vm_name} 2>&1", password=password)
            if exit_code != 0:
                exit_code, output = _ssh_run(ssh, f"sudo -S virsh destroy {vm_name} 2>&1", password=password)
            if exit_code != 0:
                return jsonify({"error": f"Failed to shut down: {output}"}), 400
            return jsonify({"status": "shut off", "vm_name": vm_name})
        else:
            exit_code, output = _ssh_run(ssh, f"sudo -S virsh start {vm_name} 2>&1", password=password)
            if exit_code != 0:
                return jsonify({"error": f"Failed to start: {output}"}), 400
            return jsonify({"status": "running", "vm_name": vm_name})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"Toggle VM failed: {e}"}), 500


@app.route("/api/delete_vm", methods=["POST"])
def api_delete_vm():
    """Delete VM + its PCI XML files."""
    import traceback
    sid = _get_session_id()
    session = _get_session(sid)
    try:
        data = request.json or {}
        vm_name = data.get("vm_name", "").strip()
        if not vm_name:
            return jsonify({"error": "vm_name is required"}), 400

        ssh = session.get("ssh_client")
        if ssh is None:
            return jsonify({"error": "Not connected to host"}), 400

        password = session.get("host_password", "")

        # Destroy (force stop)
        _ssh_run(ssh, f"sudo -S virsh destroy {vm_name} 2>/dev/null", password=password)
        time.sleep(1)

        # Undefine with storage removal (deletes disk = netplan inside gone)
        exit_code, output = _ssh_run(ssh, f"sudo -S virsh undefine {vm_name} --remove-all-storage 2>&1", password=password)
        if exit_code != 0:
            exit_code, output = _ssh_run(ssh, f"sudo -S virsh undefine {vm_name} 2>&1", password=password)

        if exit_code != 0:
            return jsonify({"error": f"Failed to delete VM: {output}"}), 400

        # Delete PCI XMLs
        _ssh_run(ssh, f"sudo -S rm -f {VM_WORK_DIR}/{vm_name}_pci*.xml 2>/dev/null", password=password)

        return jsonify({"status": "deleted", "vm_name": vm_name})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"Delete failed: {e}"}), 500


@app.route("/api/clone_vm", methods=["POST"])
def api_clone_vm():
    """Clone an existing VM into a new one (virsh-only, no extra packages).

    Copies the primary disk (reflink when possible), dumps the source XML,
    swaps name/uuid/disk, drops MACs + PCI hostdev passthrough (those
    devices are already assigned to the source), and defines the clone.
    """
    import traceback
    import os as _os
    sid = _get_session_id()
    session = _get_session(sid)
    try:
        data = request.json or {}
        src = data.get("vm_name", "").strip()
        new_name = data.get("new_name", "").strip()
        if not src or not new_name:
            return jsonify({"error": "vm_name and new_name are required"}), 400
        if not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$", new_name):
            return jsonify({"error": "new_name may only contain letters, digits, '.', '_' or '-'"}), 400
        if new_name == src:
            return jsonify({"error": "new_name must differ from the source VM"}), 400

        # Credentials: explicit values win; otherwise inherit from the source
        # (a clone is a byte-copy of the disk, so the OS login carries over).
        src_meta = _vm_build_meta(src) or {}
        new_username = (data.get("new_username") or "").strip() or src_meta.get("vm_username") or "ubuntu"
        new_password = (data.get("new_password") or "").strip() or src_meta.get("vm_password") or ""
        if not re.match(r"^[a-z_][a-z0-9._-]{0,31}$", new_username):
            return jsonify({"error": "new_username must be a valid login name (lowercase letters, digits, '.', '_', '-')"}), 400

        ssh = session.get("ssh_client")
        if ssh is None:
            return jsonify({"error": "Not connected to host"}), 400
        password = session.get("host_password", "")

        # Source must exist, target must not
        code, _ = _ssh_run(ssh, f"sudo -S virsh dominfo {src} 2>/dev/null", password=password)
        if code != 0:
            return jsonify({"error": f"Source VM '{src}' not found on the host"}), 400
        code, _ = _ssh_run(ssh, f"sudo -S virsh dominfo {new_name} 2>/dev/null", password=password)
        if code == 0:
            return jsonify({"error": f"A VM named '{new_name}' already exists"}), 400

        # Primary disk path
        code, blk = _ssh_run(ssh, f"sudo -S virsh domblklist {src} 2>/dev/null", password=password)
        disk_path = None
        for bl in blk.splitlines():
            bt = bl.strip().split()
            if bt and re.match(r"^(vd|sd|xvd|nvme)", bt[0]) and len(bt) > 1 and bt[1] != "-":
                disk_path = bt[1].strip()
                break
        if not disk_path:
            return jsonify({"error": f"Could not find a disk device for '{src}'"}), 400

        disk_dir = _os.path.dirname(disk_path)
        ext = _os.path.splitext(disk_path)[1] or ".qcow2"
        new_disk = f"{disk_dir}/{new_name}{ext}"

        # Copy the disk: reflink when the filesystem supports it (instant CoW),
        # otherwise a plain copy.
        code, out = _ssh_run(ssh, f"sudo -S cp --reflink=auto {disk_path} {new_disk} 2>&1", password=password)
        if code != 0:
            code, out = _ssh_run(ssh, f"sudo -S cp {disk_path} {new_disk} 2>&1", password=password)
        if code != 0:
            return jsonify({"error": f"Failed to copy disk: {out.strip()[:200]}"}), 400

        # Source XML -> clone XML
        code, xml = _ssh_run(ssh, f"sudo -S virsh dumpxml {src} 2>/dev/null", password=password)
        if code != 0:
            _ssh_run(ssh, f"sudo -S rm -f {new_disk}", password=password)
            return jsonify({"error": f"Failed to read XML of '{src}'"}), 400
        xml2 = re.sub(r"<name>.*?</name>", f"<name>{new_name}</name>", xml, count=1, flags=re.S)
        xml2 = re.sub(r"<uuid>.*?</uuid>\s*", "", xml2, count=1, flags=re.S)
        xml2 = xml2.replace(disk_path, new_disk)
        xml2 = re.sub(r"\s*<mac address='[^']*'/?(/?)>\s*", "\n", xml2)
        xml2 = re.sub(r"\s*<hostdev\b.*?</hostdev>\s*", "\n", xml2, flags=re.S)

        # Upload the transformed XML and define the clone
        remote_xml = f"/tmp/{new_name}_clone.xml"
        try:
            sftp = ssh.open_sftp()
            try:
                with sftp.open(remote_xml, "w") as f:
                    f.write(xml2)
            finally:
                sftp.close()
        except Exception as e:
            _ssh_run(ssh, f"sudo -S rm -f {new_disk}", password=password)
            return jsonify({"error": f"Failed to upload clone XML: {e}"}), 500
        code, out = _ssh_run(ssh, f"sudo -S virsh define {remote_xml} 2>&1", password=password)
        _ssh_run(ssh, f"sudo -S rm -f {remote_xml} 2>/dev/null", password=password)
        if code != 0:
            _ssh_run(ssh, f"sudo -S rm -f {new_disk}", password=password)
            return jsonify({"error": f"Failed to define clone: {out.strip()[:200]}"}), 400

        # Record the clone's login so chat/UI/SSH know it (even after the
        # source's build leaves the in-memory registry).
        with _CLONE_META_LOCK:
            _CLONE_META[new_name] = {
                "vm_name": new_name,
                "vm_username": new_username,
                "vm_password": new_password,
                "vm_type": src_meta.get("vm_type", "normal"),
                "os_image": src_meta.get("os_image"),
                "aic_cards": src_meta.get("aic_cards"),
                "source": src,
            }

        _audit(sid, "vm_clone", f"{src} -> {new_name}")
        return jsonify({
            "status": "ok",
            "vm_name": new_name,
            "source": src,
            "vm_username": new_username,
            "vm_password": new_password,
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"Clone VM failed: {e}"}), 500


@app.route("/api/session_status", methods=["GET"])
def api_session_status():
    """Return current session state — used to reconnect after page refresh."""
    sid = _get_session_id()
    session = _get_session(sid)
    config = session.get("config")
    return jsonify({
        "stage": session.get("stage", "waiting"),
        "vm_name": config.vm_name if config else None,
        "host_ip": session.get("host_ip"),
        "connected": session.get("ssh_client") is not None,
        "agent_mode": session.get("agent_mode", False),
    })


# ─── Natural-language VM request parsing ─────────────────────

def _heuristic_extract(message: str) -> dict:
    """Best-effort regex extraction used when the LLM is unavailable.

    Covers the common patterns (name/memory/cores/disk/AIC cards/type)
    so the natural-language box still works without an API key.
    """
    out: dict = {}

    m = (re.search(r"(?:named|called|name\s*=|name\s+is)\s+([a-zA-Z0-9][a-zA-Z0-9._-]*)", message, re.I)
         or re.search(r"\bvm\s+([a-zA-Z][a-zA-Z0-9._-]*)", message, re.I))
    if m:
        out["vm_name"] = m.group(1)

    m = (re.search(r"(\d+)\s*(?:gb|g)\s*(?:ram|memory|mem)", message, re.I)
         or re.search(r"(?:ram|memory|mem)\w*\s*[:=]?\s*(\d+)\s*(?:gb|g)", message, re.I))
    if m:
        out["memory_gb"] = int(m.group(1))

    # Word-based core counts first ("quad core" -> 4, "dual core" -> 2)
    m = re.search(r"(single|one)\s*cores?", message, re.I)
    if m:
        out["num_cpu"] = 1
    m = re.search(r"(dual|two|double)\s*cores?", message, re.I)
    if m:
        out["num_cpu"] = 2
    m = re.search(r"(quad|four|quadruple)\s*cores?", message, re.I)
    if m:
        out["num_cpu"] = 4
    m = re.search(r"(octa|eight|8)\s*cores?", message, re.I)
    if m:
        out["num_cpu"] = 8
    if "num_cpu" not in out:
        m = re.search(r"(\d+)\s*-?\s*(?:cores?|cpus?|vcpus?)", message, re.I)
        if m:
            out["num_cpu"] = int(m.group(1))

    # Disk only when the number is tied to disk/storage context, or is in TB
    m = (re.search(r"(\d+(?:\.\d+)?)\s*(tb|gb|g)\s*(?:disk|storage|drive|space)", message, re.I)
         or re.search(r"(?:disk|storage|drive)\w*\s*[:=]?\s*(\d+(?:\.\d+)?)\s*(tb|gb|g)", message, re.I)
         or re.search(r"(\d+(?:\.\d+)?)\s*tb\b", message, re.I))
    if m:
        val = float(m.group(1))
        unit = m.group(2).lower()
        out["disk_size"] = f"{int(val * 1024) if unit == 'tb' else int(val)}G"

    # Bare GB number with no disk/storage context defaults to memory
    # (covers the common phrasing "32 GB / 4-core VM") but never overrides
    # an explicit disk size or a memory value that was already found.
    if "memory_gb" not in out and "disk_size" not in out:
        m = re.search(r"(\d+)\s*(?:gb|g)\b", message, re.I)
        if m:
            out["memory_gb"] = int(m.group(1))

    m = re.search(r"(\d+)\s*(?:aic|card|cards|accelerators?)", message, re.I)
    if m:
        out["aic_cards"] = int(m.group(1))

    if re.search(r"\bp2p\b|\bpeer\b", message, re.I):
        out["vm_type"] = "p2p"

    return out


@app.route("/api/parse_request", methods=["POST"])
def api_parse_request():
    """Turn a natural-language request into a VM config dict.

    Uses Claude via input_extractor when ANTHROPIC_API_KEY is set,
    otherwise falls back to regex heuristics so the UI stays usable.
    """
    sid = _get_session_id()
    data = request.json or {}
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"error": "message is required"}), 400

    from settings import ANTHROPIC_API_KEY

    extracted: dict = {}
    used_llm = False
    note = None
    if ANTHROPIC_API_KEY:
        try:
            extracted = input_extractor.extract_vm_config(message)
            used_llm = True
        except Exception as e:
            extracted = {}
            note = f"Claude extraction failed ({e}) — used built-in parsing instead."
    else:
        note = "ANTHROPIC_API_KEY not set — used built-in parsing."

    # Fill anything still missing with heuristics
    for k, v in _heuristic_extract(message).items():
        extracted.setdefault(k, v)

    # Sensible defaults for unset fields
    defaults = {
        "vm_username": "ubuntu",
        "vm_type": "normal",
        "disk_size": "100G",
        "os_image": "/home/vm_images/ubuntu-24.04.3-live-server-amd64.iso",
        "debug": "enable",
    }
    for k, v in defaults.items():
        if not extracted.get(k):
            extracted[k] = v

    allowed = {
        "vm_name", "memory_gb", "num_cpu", "disk_size", "disk_path",
        "os_image", "aic_cards", "vm_type", "acs_state",
        "vm_username", "vm_password", "debug",
    }
    cleaned = {k: v for k, v in extracted.items() if k in allowed and v not in (None, "")}
    _audit(sid, "parse_request", f"llm={used_llm}")
    return jsonify({"config": cleaned, "used_llm": used_llm, "note": note})


@app.route("/api/cancel", methods=["POST"])
def api_cancel():
    """Request cancellation of a build (per build_id, or all this session's)."""
    sid = _get_session_id()
    data = request.get_json(silent=True) or {}
    build_id = (data.get("build_id") or "").strip()
    cancelled = []
    with _BUILDS_LOCK:
        if build_id:
            b = _BUILDS.get(build_id)
            if b and b["sid"] == sid and b["stage"] == "running":
                b["cancel_requested"] = True
                cancelled.append(build_id)
        else:
            # Back-compat: cancel every running build for this session
            for b in _BUILDS.values():
                if b["sid"] == sid and b["stage"] == "running":
                    b["cancel_requested"] = True
                    cancelled.append(b["id"])
    if cancelled:
        _audit(sid, "cancel", f"builds={','.join(cancelled)}")
    return jsonify({"status": "cancelling", "cancelled": cancelled})


@app.route("/api/builds", methods=["GET"])
def api_builds():
    """List this session's builds (running + recent) so the UI can restore
    live build cards and log viewers after a page refresh."""
    sid = _get_session_id()
    with _BUILDS_LOCK:
        builds = [_build_snapshot(b) for b in _BUILDS.values() if b["sid"] == sid]
    builds.sort(key=lambda x: x["started_at"], reverse=True)
    return jsonify({"builds": builds})


@app.route("/api/build_logs/<build_id>", methods=["GET"])
def api_build_logs(build_id):
    """Return the recent log lines captured for a specific build."""
    sid = _get_session_id()
    with _BUILDS_LOCK:
        b = _BUILDS.get(build_id)
        if not b or b["sid"] != sid:
            return jsonify({"error": "build not found"}), 404
        lines = list(b["log_lines"])[-500:]
        snapshot = _build_snapshot(b)
    return jsonify({"lines": lines, **snapshot})


@app.route("/api/execute_stream/<build_id>", methods=["GET"])
def api_execute_stream(build_id):
    """Re-attach to an already-running build and stream its events (SSE).

    Used after a page refresh or when switching the focused console back to
    an earlier build: replays the build's buffered events, then live ones.
    """
    sid = _get_session_id()
    with _BUILDS_LOCK:
        b = _BUILDS.get(build_id)
        if not b or b["sid"] != sid:
            return jsonify({"error": "build not found"}), 404

    def generate():
        yield f"data: {json.dumps({'type': 'build_id', 'build_id': b['id'], 'vm_name': b['vm_name'], 'replay': True})}\n\n"
        for obj in _iter_build_events(b, replay=True):
            yield f"data: {json.dumps(obj)}\n\n"

    return Response(generate(), mimetype="text/event-stream")


@app.route("/api/logs/tail/<vm_name>", methods=["GET"])
def api_logs_tail(vm_name):
    """Return the last N lines of a VM's log file (for reconnect after refresh)."""
    if not re.match(r"^[a-zA-Z0-9._-]+$", vm_name):
        return jsonify({"error": "Invalid VM name"}), 400
    log_path = os.path.join(LOG_DIR, f"{vm_name}.txt")
    if not os.path.exists(log_path):
        return jsonify({"lines": [], "total": 0})
    try:
        limit = int(request.args.get("limit", "500"))
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
        lines = [l.rstrip("\n") for l in all_lines[-limit:]]
        return jsonify({"lines": lines, "total": len(all_lines)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/reset", methods=["POST"])
def api_reset():
    """Reset current session."""
    sid = _get_session_id()
    with _SESSIONS_LOCK:
        if sid in _SESSIONS:
            ssh = _SESSIONS[sid].get("ssh_client")
            if ssh:
                try:
                    ssh.close()
                except Exception:
                    pass
            del _SESSIONS[sid]
    _audit(sid, "reset", "")
    return jsonify({"status": "reset"})


# ─── Feature 5: Downloadable log archive ─────────────────────

@app.route("/api/download_log/<vm_name>", methods=["GET"])
def api_download_log(vm_name):
    """Download the log file for a specific VM."""
    # Sanitize vm_name — only alphanumerics, dash, underscore, dot
    if not re.match(r"^[a-zA-Z0-9._-]+$", vm_name):
        return jsonify({"error": "Invalid VM name"}), 400
    log_path = os.path.join(LOG_DIR, f"{vm_name}.txt")
    if not os.path.exists(log_path):
        return jsonify({"error": f"Log file not found for VM '{vm_name}'"}), 404
    sid = _get_session_id()
    _audit(sid, "download_log", vm_name)
    return send_file(log_path, as_attachment=True, download_name=f"{vm_name}.log")


@app.route("/api/list_logs", methods=["GET"])
def api_list_logs():
    """List available log files."""
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        files = []
        for f in sorted(os.listdir(LOG_DIR)):
            if f.endswith(".txt") or f.endswith(".log"):
                path = os.path.join(LOG_DIR, f)
                try:
                    stat = os.stat(path)
                    files.append({
                        "name": f,
                        "size_bytes": stat.st_size,
                        "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                    })
                except Exception:
                    pass
        return jsonify({"logs": files})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─── Feature 10: Config validation preview ──────────────────

@app.route("/api/preview_config", methods=["POST"])
def api_preview_config():
    """Preview which PCI devices would be assigned for the given config.
    
    Runs pci list logic in read-only mode without actually creating anything.
    """
    import traceback
    sid = _get_session_id()
    session = _get_session(sid)
    try:
        data = request.json or {}
        ssh = session.get("ssh_client")
        if ssh is None:
            return jsonify({"error": "Not connected to host"}), 400

        # Build a temp config
        tmp_config = VMConfig()
        tmp_config.vm_name = data.get("vm_name", "preview").strip()
        tmp_config.aic_cards = None
        try:
            aic_val = data.get("aic_cards", "").strip()
            if aic_val:
                tmp_config.aic_cards = int(aic_val)
        except (ValueError, AttributeError):
            pass
        tmp_config.pci_group = data.get("pci_group", 1)

        # Build a fake state
        tmp_state = AgentState()
        tmp_state.set_output("host_ssh", ssh)
        tmp_state.set_output("host_password", session.get("host_password", ""))

        # Run the PCI list tool
        from tools import tool_list_pci_devices
        result = tool_list_pci_devices.run(tmp_config, tmp_state)

        if result.get("status") == "success":
            data_out = result.get("data", {})
            # The tool returns SBDF strings ("0000:0a:00.0"); the UI expects
            # {address, name, free} objects — normalize here.
            raw_devs = data_out.get("pci_devices", []) or []
            pci_devices = [
                {
                    "address": d.get("address") or d,
                    "name": d.get("name") or "Qualcomm AIC",
                    "free": True,
                }
                if isinstance(d, dict)
                else {"address": d, "name": "Qualcomm AIC", "free": True}
                for d in raw_devs
            ]
            return jsonify({
                "ok": True,
                "pci_devices": pci_devices,
                "iommu_group": data_out.get("iommu_group"),
                "free_groups_count": data_out.get("free_groups_count", 0),
                "used_iommu_groups": data_out.get("used_iommu_groups", []),
            })
        else:
            return jsonify({"ok": False, "error": result.get("error", "unknown error")}), 400

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


# ─── Feature 3: VM template/preset library ──────────────────

TEMPLATES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates_presets.json")
_TEMPLATES_LOCK = threading.Lock()

DEFAULT_TEMPLATES = [
    {
        "name": "AIC-4-card Standard",
        "description": "Standard Ubuntu VM with 4 Qualcomm AIC cards passed through",
        "config": {
            "memory_gb": 32,
            "num_cpu": 4,
            "disk_size": "100G",
            "vm_type": "normal",
            "debug": "enable",
            "vm_username": "ubuntu",
            "aic_cards": 4,
        }
    },
    {
        "name": "AIC-4-card Large (64GB)",
        "description": "Larger memory config for ML workloads",
        "config": {
            "memory_gb": 64,
            "num_cpu": 8,
            "disk_size": "200G",
            "vm_type": "normal",
            "debug": "enable",
            "vm_username": "ubuntu",
            "aic_cards": 4,
        }
    },
    {
        "name": "P2P VM",
        "description": "P2P-enabled VM with ACS override",
        "config": {
            "memory_gb": 32,
            "num_cpu": 4,
            "disk_size": "100G",
            "vm_type": "p2p",
            "acs_state": "disable",
            "debug": "enable",
            "vm_username": "ubuntu",
            "aic_cards": 4,
        }
    },
]


def _load_templates():
    """Load templates from disk, seed with defaults if missing."""
    with _TEMPLATES_LOCK:
        if not os.path.exists(TEMPLATES_FILE):
            try:
                with open(TEMPLATES_FILE, "w", encoding="utf-8") as f:
                    json.dump(DEFAULT_TEMPLATES, f, indent=2)
            except Exception:
                return DEFAULT_TEMPLATES
        try:
            with open(TEMPLATES_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return DEFAULT_TEMPLATES


def _save_templates(templates):
    """Save templates to disk atomically."""
    with _TEMPLATES_LOCK:
        try:
            tmp = TEMPLATES_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(templates, f, indent=2)
            os.replace(tmp, TEMPLATES_FILE)
            return True
        except Exception:
            return False


@app.route("/api/templates", methods=["GET"])
def api_templates():
    """List all VM templates."""
    return jsonify({"templates": _load_templates()})


@app.route("/api/templates", methods=["POST"])
def api_save_template():
    """Save a new VM template."""
    if not _check_api_key():
        return jsonify({"error": "unauthorized"}), 401
    sid = _get_session_id()
    try:
        data = request.json or {}
        name = data.get("name", "").strip()
        description = data.get("description", "").strip()
        cfg = data.get("config", {})
        if not name or not isinstance(cfg, dict):
            return jsonify({"error": "name and config required"}), 400
        templates = _load_templates()
        # Remove existing with same name
        templates = [t for t in templates if t.get("name") != name]
        templates.append({"name": name, "description": description, "config": cfg})
        _save_templates(templates)
        _audit(sid, "template_saved", name)
        return jsonify({"ok": True, "templates": templates})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/templates/<name>", methods=["DELETE"])
def api_delete_template(name):
    """Delete a VM template by name."""
    if not _check_api_key():
        return jsonify({"error": "unauthorized"}), 401
    sid = _get_session_id()
    templates = _load_templates()
    original_len = len(templates)
    templates = [t for t in templates if t.get("name") != name]
    if len(templates) == original_len:
        return jsonify({"error": "template not found"}), 404
    _save_templates(templates)
    _audit(sid, "template_deleted", name)
    return jsonify({"ok": True})


# ─── Feature 4: Health check dashboard ──────────────────────

@app.route("/api/health", methods=["GET"])
def api_health():
    """Return overall server + VMs health summary."""
    sid = _get_session_id()
    session = _get_session(sid)
    try:
        ssh = session.get("ssh_client")
        if ssh is None:
            return jsonify({"error": "Not connected to host"}), 400

        password = session.get("host_password", "")
        resources = _fetch_host_resources(ssh, password)

        # Count VMs by state
        _, output = _ssh_run(ssh, "sudo -S virsh list --all --name 2>/dev/null", password=password)
        vms = [n.strip() for n in output.splitlines() if n.strip()]
        vm_states = {"running": 0, "shut off": 0, "paused": 0, "unknown": 0}
        for vm in vms:
            _, state_out = _ssh_run(ssh, f"sudo -S virsh domstate {vm} 2>/dev/null", password=password)
            st = state_out.strip() or "unknown"
            vm_states[st] = vm_states.get(st, 0) + 1

        # Check qmonitor-proxy
        _, qm_out = _ssh_run(ssh, "systemctl is-active qmonitor-proxy 2>/dev/null || echo inactive", password=password)
        qmonitor_active = "active" in qm_out.lower() and "inactive" not in qm_out.lower()

        return jsonify({
            "host_resources": resources,
            "total_vms": len(vms),
            "vm_states": vm_states,
            "qmonitor_active": qmonitor_active,
            "active_sessions": len(_SESSIONS),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─── Feature 12: Rollback on failure ────────────────────────

@app.route("/api/rollback", methods=["POST"])
def api_rollback():
    """Clean up a partially-created VM (destroy + undefine + remove XMLs)."""
    if not _check_api_key():
        return jsonify({"error": "unauthorized"}), 401
    sid = _get_session_id()
    session = _get_session(sid)
    try:
        data = request.json or {}
        vm_name = data.get("vm_name", "").strip()
        if not vm_name:
            return jsonify({"error": "vm_name required"}), 400

        ssh = session.get("ssh_client")
        if ssh is None:
            return jsonify({"error": "Not connected to host"}), 400

        password = session.get("host_password", "")

        # Best-effort cleanup — ignore errors
        _ssh_run(ssh, f"sudo -S virsh destroy {vm_name} 2>/dev/null || true", password=password)
        time.sleep(1)
        _ssh_run(ssh, f"sudo -S virsh undefine {vm_name} --remove-all-storage 2>/dev/null || true", password=password)
        _ssh_run(ssh, f"sudo -S rm -f {VM_WORK_DIR}/{vm_name}_pci*.xml 2>/dev/null", password=password)
        _ssh_run(ssh, f"sudo -S rm -f {VM_WORK_DIR}/{vm_name}.qcow2 2>/dev/null", password=password)

        _audit(sid, "rollback", vm_name)
        return jsonify({"ok": True, "vm_name": vm_name})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─── Feature 13: Audit log endpoint ─────────────────────────

@app.route("/api/audit", methods=["GET"])
def api_audit():
    """Return the last N audit log entries."""
    if not _check_api_key():
        return jsonify({"error": "unauthorized"}), 401
    try:
        limit = int(request.args.get("limit", "100"))
        if not os.path.exists(AUDIT_LOG_PATH):
            return jsonify({"entries": []})
        with open(AUDIT_LOG_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
        entries = []
        for line in lines[-limit:]:
            parts = line.strip().split(" | ", 3)
            if len(parts) >= 3:
                entries.append({
                    "timestamp": parts[0],
                    "session": parts[1],
                    "action": parts[2],
                    "details": parts[3] if len(parts) > 3 else "",
                })
        return jsonify({"entries": entries})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─── Feature 9: Bulk VM operations ──────────────────────────

@app.route("/api/bulk_delete", methods=["POST"])
def api_bulk_delete():
    """Delete multiple VMs at once."""
    if not _check_api_key():
        return jsonify({"error": "unauthorized"}), 401
    sid = _get_session_id()
    session = _get_session(sid)
    try:
        data = request.json or {}
        vm_names = data.get("vm_names", [])
        if not isinstance(vm_names, list) or not vm_names:
            return jsonify({"error": "vm_names (list) required"}), 400

        ssh = session.get("ssh_client")
        if ssh is None:
            return jsonify({"error": "Not connected to host"}), 400

        password = session.get("host_password", "")
        results = []
        for vm_name in vm_names:
            if not re.match(r"^[a-zA-Z0-9._-]+$", vm_name):
                results.append({"vm": vm_name, "ok": False, "error": "invalid name"})
                continue
            _ssh_run(ssh, f"sudo -S virsh destroy {vm_name} 2>/dev/null || true", password=password)
            time.sleep(0.5)
            exit_code, output = _ssh_run(
                ssh, f"sudo -S virsh undefine {vm_name} --remove-all-storage 2>&1", password=password
            )
            _ssh_run(ssh, f"sudo -S rm -f {VM_WORK_DIR}/{vm_name}_pci*.xml 2>/dev/null", password=password)
            results.append({
                "vm": vm_name,
                "ok": exit_code == 0,
                "error": output if exit_code != 0 else None,
            })
        _audit(sid, "bulk_delete", f"{len(vm_names)} VMs")
        return jsonify({"results": results})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    print(f"🚀 VM Agent Web UI starting (DRY_RUN={DRY_RUN}) — multi-user mode")
    print("   Open http://localhost:5000 in your browser")
    app.run(debug=True, host="0.0.0.0", port=5000, threaded=True)