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
from settings import DRY_RUN, AGENT_MODE, KVM_APT_PACKAGES, VM_WORK_DIR

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
    """Check API key from X-API-Key header. Returns True if OK or auth disabled."""
    if not API_KEY:
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
    """Get session ID from cookie, or create new one."""
    sid = request.cookies.get(SESSION_COOKIE)
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
                "last_active": time.time(),
            }
        _SESSIONS[sid]["last_active"] = time.time()
        return _SESSIONS[sid]


def _cleanup_stale_sessions():
    """Remove sessions that have been idle beyond SESSION_TIMEOUT."""
    now = time.time()
    with _SESSIONS_LOCK:
        stale = [
            sid for sid, s in _SESSIONS.items()
            if now - s.get("last_active", now) > SESSION_TIMEOUT
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

def _run_agent_mode(config, state, sid, session, ssh_client, log_queue, plan):
    """Generator that runs the agentic loop and yields SSE events.
    
    HOW IT WORKS:
    1. Creates an event queue for the agentic loop to send events
    2. Runs run_agentic_loop() in a background thread
    3. While the loop runs, drains both the event queue and log queue
    4. Yields SSE events to the browser in real-time
    
    The stream_callback in run_agentic_loop() puts events into agent_event_queue.
    This generator reads from that queue and yields them as SSE.
    """
    import queue as _queue
    import threading as _th

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
                # SSH is dead — try to reconnect using stored credentials
                print(f"   [AGENT] SSH connection dead — reconnecting...", flush=True)
                host_ip = session.get("host_ip")
                host_user = session.get("host_username", "ubuntu")
                host_pass = session.get("host_password", "")
                if host_ip and host_user and host_pass:
                    try:
                        new_ssh = _make_ssh(host_ip, host_user, host_pass)
                        session["ssh_client"] = new_ssh
                        state.set_output("host_ssh", new_ssh)
                        print(f"   [AGENT] SSH reconnected to {host_ip}", flush=True)
                        yield f"data: {json.dumps({'type': 'log', 'line': f'[AGENT] SSH reconnected to {host_ip}'})}\n\n"
                    except Exception as e:
                        print(f"   [AGENT] SSH reconnect failed: {e}", flush=True)
                        yield f"data: {json.dumps({'type': 'failed', 'tool': 'agent_loop', 'error': f'SSH connection failed: {e}. Please go back to Step 1 and reconnect.'})}\n\n"
                        session["stage"] = "done"
                        return
                else:
                    yield f"data: {json.dumps({'type': 'failed', 'tool': 'agent_loop', 'error': 'No SSH connection. Please go back to Step 1 and reconnect to the host.'})}\n\n"
                    session["stage"] = "done"
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
            orchestrator.run_agentic_loop(config, state, stream_callback=stream_callback)
        except Exception as e:
            agent_event_queue.put_nowait(("agent_error", {"error": str(e)}))
        finally:
            agent_done["done"] = True

    # Start the agentic loop in a background thread
    t = _th.Thread(target=run_loop, daemon=True)
    t.start()

    iteration_count = 0

    # Stream events while the loop runs
    while not agent_done["done"] or not agent_event_queue.empty() or not log_queue.empty():
        # Drain log queue (installer output)
        while True:
            try:
                line = log_queue.get_nowait()
                yield f"data: {json.dumps({'type': 'log', 'line': line})}\n\n"
            except _queue.Empty:
                break

        # Drain agent event queue
        while True:
            try:
                event_type, data = agent_event_queue.get_nowait()

                if event_type == "agent_start":
                    yield f"data: {json.dumps({'type': 'agent_start', 'vm_name': data.get('vm_name'), 'plan': data.get('plan', [])})}\n\n"

                elif event_type == "agent_tool_start":
                    tool = data.get("tool", "")
                    iteration = data.get("iteration", 0)
                    iteration_count = iteration
                    pct = min(int((iteration / 20) * 100), 95)
                    yield f"data: {json.dumps({'type': 'running', 'tool': tool, 'step': iteration, 'attempt': 1})}\n\n"
                    yield f"data: {json.dumps({'type': 'progress', 'percent': pct, 'step': iteration, 'total': len(plan), 'tool': tool})}\n\n"
                    yield f"data: {json.dumps({'type': 'log', 'line': f'[AGENT] Calling {tool}...'})}\n\n"

                elif event_type == "agent_tool_done":
                    tool = data.get("tool", "")
                    iteration = data.get("iteration", 0)
                    yield f"data: {json.dumps({'type': 'done', 'tool': tool, 'step': iteration, 'narration': ''})}\n\n"
                    yield f"data: {json.dumps({'type': 'log', 'line': f'[AGENT] ✅ {tool} succeeded'})}\n\n"

                elif event_type == "agent_tool_failed":
                    tool = data.get("tool", "")
                    error = data.get("error", "")
                    yield f"data: {json.dumps({'type': 'log', 'line': f'[AGENT] ⚠ {tool} failed: {error}'})}\n\n"
                    yield f"data: {json.dumps({'type': 'log', 'line': '[AGENT] Claude is deciding how to recover...'})}\n\n"

                elif event_type == "agent_complete":
                    vm_ip = data.get("vm_ip", "unknown")
                    vm_mac = data.get("vm_mac", "")
                    vm_name = data.get("vm_name", config.vm_name)
                    _audit(sid, "vm_build_complete", f"vm={vm_name} ip={vm_ip} mode=agent")
                    yield f"data: {json.dumps({'type': 'progress', 'percent': 100, 'step': len(plan), 'total': len(plan), 'tool': 'done'})}\n\n"
                    yield f"data: {json.dumps({'type': 'complete', 'vm_name': vm_name, 'vm_ip': vm_ip, 'vm_mac': vm_mac, 'username': config.vm_username})}\n\n"
                    session["stage"] = "done"
                    return

                elif event_type == "agent_failed":
                    reason = data.get("reason", "unknown")
                    _audit(sid, "vm_build_failed", f"vm={config.vm_name} mode=agent reason={reason[:200]}")
                    yield f"data: {json.dumps({'type': 'failed', 'tool': 'agent_loop', 'error': reason})}\n\n"
                    session["stage"] = "done"
                    return

                elif event_type == "agent_error":
                    error = data.get("error", "unknown")
                    yield f"data: {json.dumps({'type': 'error', 'msg': f'Agent error: {error}'})}\n\n"
                    session["stage"] = "done"
                    return

            except _queue.Empty:
                break

        time.sleep(0.3)

    # If loop ended without explicit complete/failed event, check state
    vm_ip = state.get_output("vm_ip", "unknown")
    if vm_ip and vm_ip != "unknown":
        vm_mac = state.get_output("vm_mac", "")
        _audit(sid, "vm_build_complete", f"vm={config.vm_name} ip={vm_ip} mode=agent")
        yield f"data: {json.dumps({'type': 'progress', 'percent': 100, 'step': len(plan), 'total': len(plan), 'tool': 'done'})}\n\n"
        yield f"data: {json.dumps({'type': 'complete', 'vm_name': config.vm_name, 'vm_ip': vm_ip, 'vm_mac': vm_mac, 'username': config.vm_username})}\n\n"
    else:
        yield f"data: {json.dumps({'type': 'failed', 'tool': 'agent_loop', 'error': 'Agent loop ended without completion'})}\n\n"
    session["stage"] = "done"


# ── Routes ────────────────────────────────────────────────────

@app.route("/")
def index():
    _cleanup_stale_sessions()
    sid = _get_session_id()
    _get_session(sid)  # ensure session exists
    resp = make_response(render_template("index.html", dry_run=DRY_RUN))
    return _make_response_with_cookie(sid, resp)


@app.route("/api/connect", methods=["POST"])
def api_connect():
    """Step 1: SSH into KVM host, fetch system info."""
    import traceback
    sid = _get_session_id()
    session = _get_session(sid)

    try:
        data = request.json or {}
        host_ip = data.get("host_ip", "").strip()
        username = data.get("username", "").strip()
        password = data.get("password", "")

        if not host_ip or not username:
            return jsonify({"error": "host_ip and username are required"}), 400

        if DRY_RUN:
            fake_resources = {
                "ram_total_gb": 128.0, "ram_free_gb": 112.0,
                "cpu_count": 32, "cpu_free": 28, "cpu_allocated": 4,
                "disk_total_gb": 2000.0, "disk_free_gb": 1400.0,
            }
            session.update({
                "host_ip": host_ip, "host_username": username,
                "host_password": password, "host_resources": fake_resources,
                "stage": "connected",
            })
            summary = _format_summary(fake_resources)
            resp = make_response(jsonify({"summary": summary, "resources": fake_resources}))
            return _make_response_with_cookie(sid, resp)

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
            return jsonify({"error": f"SSH connection failed: {e}"}), 400

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
            return jsonify({"error": f"Failed to fetch host info: {e}"}), 500

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

        summary = _format_summary(resources)
        resp = make_response(jsonify({"summary": summary, "resources": resources}))
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

    def generate():
        from tools import TOOL_REGISTRY
        from settings import MAX_RETRIES
        import queue as _queue

        plan = orchestrator.build_plan(config)
        state = AgentState()
        state.set_plan(plan)

        # Inject per-session SSH + credentials into state
        ssh_client = session.get("ssh_client")
        if ssh_client and not DRY_RUN:
            state.set_output("host_ssh", ssh_client)
        state.set_output("host_password", session.get("host_password", ""))
        # Store host credentials so agent can reconnect if SSH drops
        state.set_output("host_ip", session.get("host_ip", ""))
        state.set_output("host_username", session.get("host_username", "ubuntu"))

        # Set per-VM log file + real-time log stream queue
        log_queue = _queue.Queue(maxsize=1000)
        try:
            from tools.installer_autopilot import _set_log_file, _set_log_stream_queue
            _set_log_file(f"{config.vm_name}.txt")
            _set_log_stream_queue(log_queue)
        except Exception:
            pass

        print(f"\n{'='*60}", flush=True)
        mode_label = "AGENT" if effective_agent_mode else "CLASSIC"
        print(f"[EXECUTE][{sid[:8]}][{mode_label}] Starting for VM '{config.vm_name}'", flush=True)
        print(f"{'='*60}", flush=True)

        yield f"data: {json.dumps({'type': 'start', 'total': len(plan), 'plan': plan, 'agent_mode': effective_agent_mode})}\n\n"

        # ─── AGENT MODE: Looping agentic loop ────────────────
        if effective_agent_mode:
            yield from _run_agent_mode(
                config, state, sid, session, ssh_client, log_queue, plan
            )
            return

        # ─── CLASSIC MODE: Hardcoded plan ────────────────────
        # (original code below, unchanged)

        def _drain_log_queue():
            """Drain all pending log lines from the queue and yield SSE events."""
            events = []
            while True:
                try:
                    line = log_queue.get_nowait()
                    events.append(f"data: {json.dumps({'type': 'log', 'line': line})}\n\n")
                except _queue.Empty:
                    break
            return events

        def _ensure_ssh_alive():
            """Check if SSH is still alive, reconnect if needed."""
            nonlocal ssh_client
            try:
                if ssh_client and ssh_client.get_transport() and ssh_client.get_transport().is_active():
                    ssh_client.get_transport().send_ignore()
                    return
            except Exception:
                pass
            print(f"   [session {sid[:8]}] [SSH] Reconnecting (session died)...", flush=True)
            try:
                pw = session.get("host_password", "")
                usr = session.get("host_username", "ubuntu")
                host = session.get("host_ip")
                ssh_client = _make_ssh(host, usr, pw)
                session["ssh_client"] = ssh_client
                state.set_output("host_ssh", ssh_client)
                print(f"   [session {sid[:8]}] [SSH] Reconnected!", flush=True)
            except Exception as e:
                print(f"   [session {sid[:8]}] [SSH] Reconnect failed: {e}", flush=True)

        try:
            for idx, tool_name in enumerate(plan, 1):
                if not DRY_RUN:
                    _ensure_ssh_alive()

                fn = TOOL_REGISTRY.get(tool_name)
                if fn is None:
                    yield f"data: {json.dumps({'type': 'error', 'tool': tool_name, 'msg': 'not found'})}\n\n"
                    break

                state.mark_running(tool_name)
                # Progress: send percentage complete based on step number
                pct = int((idx - 1) / len(plan) * 100)
                yield f"data: {json.dumps({'type': 'progress', 'percent': pct, 'step': idx, 'total': len(plan), 'tool': tool_name})}\n\n"

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
                    yield f"data: {json.dumps({'type': 'running', 'tool': tool_name, 'step': idx, 'attempt': attempt})}\n\n"

                    tool_result_holder["done"] = False
                    tool_result_holder["result"] = None
                    t = _th.Thread(target=_run_tool, daemon=True)
                    t.start()

                    # While tool runs, drain log queue and yield log events
                    while not tool_result_holder["done"]:
                        events = _drain_log_queue()
                        for ev in events:
                            yield ev
                        time.sleep(0.3)
                    # Final drain
                    for ev in _drain_log_queue():
                        yield ev

                    result = tool_result_holder["result"] or {"status": "failed", "error": "no result"}

                    if result["status"] == "success":
                        state.mark_done(tool_name)
                        state.record_result(tool_name, result)
                        print(f"[{sid[:8]}][{idx}/{len(plan)}] ✅ {tool_name}", flush=True)

                        commentary = orchestrator._narrate_step(
                            tool_name, result, f"{idx}/{len(plan)} done", config
                        )
                        yield f"data: {json.dumps({'type': 'done', 'tool': tool_name, 'step': idx, 'narration': commentary or ''})}\n\n"
                        break
                    else:
                        print(f"[{sid[:8]}][{idx}/{len(plan)}] ⚠ {tool_name} FAILED: {result.get('error','')}", flush=True)
                        yield f"data: {json.dumps({'type': 'retry', 'tool': tool_name, 'attempt': attempt, 'error': result.get('error', '')})}\n\n"
                        if attempt < MAX_RETRIES:
                            time.sleep(2)
                        else:
                            state.mark_failed(tool_name, result["error"])
                            print(f"[{sid[:8]}][{idx}/{len(plan)}] ❌ {tool_name} — giving up", flush=True)
                            _audit(sid, "vm_build_failed", f"vm={config.vm_name} tool={tool_name} err={result.get('error','')[:200]}")

                            # ─── Auto-rollback: clean up partial VM ───
                            if not DRY_RUN and idx > 1:
                                try:
                                    yield f"data: {json.dumps({'type': 'log', 'line': f'⚠ Auto-rollback: cleaning up partial VM {config.vm_name}...'})}\n\n"
                                    _ssh_run(ssh_client, f"sudo -S virsh destroy {config.vm_name} 2>/dev/null || true", password=session.get('host_password', ''))
                                    time.sleep(1)
                                    _ssh_run(ssh_client, f"sudo -S virsh undefine {config.vm_name} --remove-all-storage 2>/dev/null || true", password=session.get('host_password', ''))
                                    _ssh_run(ssh_client, f"sudo -S rm -f {VM_WORK_DIR}/{config.vm_name}_pci*.xml 2>/dev/null", password=session.get('host_password', ''))
                                    yield f"data: {json.dumps({'type': 'log', 'line': '✅ Rollback complete'})}\n\n"
                                    _audit(sid, "auto_rollback", f"vm={config.vm_name}")
                                except Exception as _e:
                                    yield f"data: {json.dumps({'type': 'log', 'line': f'⚠ Rollback error: {_e}'})}\n\n"

                            yield f"data: {json.dumps({'type': 'failed', 'tool': tool_name, 'error': result.get('error', '')})}\n\n"
                            session["stage"] = "done"
                            return

            # All tools completed successfully
            vm_ip = state.get_output("vm_ip", "unknown")
            vm_mac = state.get_output("vm_mac", "")
            print(f"\n{'='*60}", flush=True)
            print(f"[{sid[:8]}][COMPLETE] VM '{config.vm_name}' ready — IP: {vm_ip} MAC: {vm_mac}", flush=True)
            print(f"{'='*60}\n", flush=True)
            _audit(sid, "vm_build_complete", f"vm={config.vm_name} ip={vm_ip}")
            yield f"data: {json.dumps({'type': 'progress', 'percent': 100, 'step': len(plan), 'total': len(plan), 'tool': 'done'})}\n\n"
            yield f"data: {json.dumps({'type': 'complete', 'vm_name': config.vm_name, 'vm_ip': vm_ip, 'vm_mac': vm_mac, 'username': config.vm_username})}\n\n"
            session["stage"] = "done"
        finally:
            # Clear the log stream queue
            try:
                from tools.installer_autopilot import _set_log_stream_queue
                _set_log_stream_queue(None)
            except Exception:
                pass

    return Response(generate(), mimetype="text/event-stream")


@app.route("/api/list_vms", methods=["GET"])
def api_list_vms():
    """List all VMs on the connected host with specs."""
    import traceback
    sid = _get_session_id()
    session = _get_session(sid)
    try:
        ssh = session.get("ssh_client")
        if ssh is None:
            return jsonify({"error": "Not connected to host"}), 400

        password = session.get("host_password", "")
        _, output = _ssh_run(ssh, "sudo -S virsh list --all --name 2>/dev/null", password=password)
        vms = [name.strip() for name in output.splitlines() if name.strip()]

        vm_list = []
        for name in vms:
            _, state_out = _ssh_run(ssh, f"sudo -S virsh domstate {name} 2>/dev/null", password=password)
            state = state_out.strip() or "unknown"

            specs = {"vcpus": "?", "memory": "?", "disk": "?"}
            try:
                _, info_out = _ssh_run(ssh, f"sudo -S virsh dominfo {name} 2>/dev/null", password=password)
                for line in info_out.splitlines():
                    if "CPU(s):" in line:
                        specs["vcpus"] = line.split(":")[-1].strip()
                    elif "Max memory:" in line or "Used memory:" in line:
                        mem_kb = line.split(":")[-1].strip().replace("KiB", "").replace("kB", "").strip()
                        try:
                            mem_gb = round(int(mem_kb) / 1024 / 1024, 1)
                            specs["memory"] = f"{mem_gb} GB"
                        except ValueError:
                            specs["memory"] = mem_kb
                _, net_out = _ssh_run(ssh, f"sudo -S virsh domifaddr {name} 2>/dev/null", password=password)
                if net_out.strip():
                    for line in net_out.splitlines():
                        m = re.search(r"([0-9a-fA-F:]{17})\s+(\w+)\s+(\d+\.\d+\.\d+\.\d+)/\d+", line)
                        if m:
                            specs["mac"] = m.group(1)
                            specs["protocol"] = m.group(2)
                            specs["ip"] = m.group(3)
                            break

                _, blk_out = _ssh_run(ssh, f"sudo -S virsh domblkinfo {name} vda --human 2>/dev/null", password=password)
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

        return jsonify({"vms": vm_list})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"Failed to list VMs: {e}"}), 500


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
    })


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
            return jsonify({
                "ok": True,
                "pci_devices": data_out.get("pci_devices", []),
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