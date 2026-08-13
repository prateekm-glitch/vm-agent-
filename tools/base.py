# tools/base.py
# ─────────────────────────────────────────────────────────────
# Shared helpers for all tools:
#   • Standard return contract: {"status", "data", "error"}
#   • run_cmd(): executes a shell command, or simulates it in DRY_RUN.
#
# Every tool exposes a `run(config, state)` function and returns the
# standard dict so the orchestrator can handle them uniformly.
# ─────────────────────────────────────────────────────────────

import sys
import subprocess

# Ensure emoji/unicode prints correctly on Windows (cp1252 → utf-8).
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from settings import DRY_RUN


def success(data=None) -> dict:
    return {"status": "success", "data": data, "error": None}


def failure(error: str, data=None) -> dict:
    return {"status": "failed", "data": data, "error": error}


def ssh_run_sudo(ssh, cmd, password=""):
    """Run a command with sudo on a remote host via SSH (PTY + stdin password).
    
    This is the reliable approach: allocates a PTY so sudo can prompt for
    a password, then sends the password via stdin. Works regardless of
    special characters in the password.
    
    Args:
        ssh: paramiko SSHClient connection
        cmd: command to run (without 'sudo' prefix — it's added automatically)
        password: the sudo password to send
        
    Returns:
        (exit_code: int, output: str)
    """
    import time as _time
    
    transport = ssh.get_transport()
    channel = transport.open_session()
    channel.get_pty()  # PTY needed for sudo to prompt
    channel.set_combine_stderr(True)
    channel.exec_command(f"sudo {cmd}")
    
    # Wait for sudo password prompt, then send password
    if password:
        _time.sleep(1)
        channel.sendall(f"{password}\n".encode())
    
    # Read all output
    raw = channel.makefile("rb").read()
    output = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
    exit_code = channel.recv_exit_status()
    
    # Strip the sudo password prompt from output
    lines = output.strip().splitlines()
    cleaned_lines = [l for l in lines if not l.strip().startswith("[sudo]")]
    return exit_code, "\n".join(cleaned_lines).strip()


def run_cmd(cmd, description: str = "") -> dict:
    """Run a shell command and return the standard contract.

    In DRY_RUN mode the command is printed but NOT executed, and a
    simulated success is returned. `cmd` may be a list or a string.
    """
    printable = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
    label = description or printable

    if DRY_RUN:
        print(f"   [DRY_RUN] would run: {printable}")
        return success({"cmd": printable, "dry_run": True})

    try:
        print(f"   $ {printable}")
        result = subprocess.run(
            cmd,
            shell=isinstance(cmd, str),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return failure(
                f"{label} failed (exit {result.returncode}): {result.stderr.strip()}",
                data={"stdout": result.stdout, "stderr": result.stderr},
            )
        return success({"stdout": result.stdout.strip(), "cmd": printable})
    except Exception as e:  # noqa: BLE001
        return failure(f"{label} raised: {e}")