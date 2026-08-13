# tool_run_mq_vm_p2p_install.py
# ─────────────────────────────────────────────────────────────
# Uploads mq_vm.py to the KVM host via SFTP, then runs:
#   python3 /home/vm_image/mq_vm.py p2p_install --name ... --acs_state ...
# over SSH. Includes discover_switches + ACS state change.
# ─────────────────────────────────────────────────────────────

import re

from tools.base import success, failure
from tools.tool_run_mq_vm_install import _upload_script
from settings import DRY_RUN, VM_WORK_DIR


def _build_cmd(config):
    """Build the mq_vm.py p2p_install command string."""
    parts = [
        f"cd {VM_WORK_DIR} &&",
        "python3 mq_vm.py p2p_install",
        f"--name {config.vm_name}",
        f"--memory {config.memory_mb}",
        f"--num_cpu {config.num_cpu}",
    ]
    if config.disk_path:
        parts.append(f"--disk_path {config.disk_path}")
    else:
        parts.append(f"--disk_size {config.disk_size}")

    os_image = config.os_image or f"{VM_WORK_DIR}/ubuntu-24.04.3-live-server-amd64.iso"
    parts.append(f"--os_image {os_image}")

    acs = config.acs_state or "enable"
    parts.append(f"--acs_state {acs}")

    debug = getattr(config, "debug", "enable") or "enable"
    parts.append(f"--debug {debug}")

    return " ".join(parts)


def run(config, state):
    if DRY_RUN:
        cmd = _build_cmd(config)
        print(f"   [DRY_RUN] would upload mq_vm.py and run: {cmd}")
        return success({"vm_ip": "192.168.122.50", "install_complete": True, "acs_state": config.acs_state})

    ssh = state.get_output("host_ssh")
    if ssh is None:
        return failure("No SSH connection to host. Connect first via /api/connect.")

    # Upload mq_vm.py
    try:
        remote_script = _upload_script(ssh)
        print(f"   Uploaded mq_vm.py → {remote_script}")
    except Exception as e:
        return failure(f"Failed to upload mq_vm.py: {e}")

    # Build and run the p2p_install command
    cmd = _build_cmd(config)
    print(f"   $ {cmd}")

    try:
        transport = ssh.get_transport()
        channel = transport.open_session()
        channel.set_combine_stderr(True)
        channel.exec_command(cmd)

        output_lines = []
        vm_ip = None

        while True:
            if channel.recv_ready():
                chunk = channel.recv(4096).decode("utf-8", errors="replace")
                for line in chunk.splitlines():
                    print(f"   [mq_vm] {line}")
                    output_lines.append(line)
                    m = re.search(r"(\d+\.\d+\.\d+\.\d+)/\d+", line)
                    if m:
                        vm_ip = m.group(1)
            elif channel.exit_status_ready():
                remaining = channel.recv(65536).decode("utf-8", errors="replace")
                for line in remaining.splitlines():
                    print(f"   [mq_vm] {line}")
                    output_lines.append(line)
                    m = re.search(r"(\d+\.\d+\.\d+\.\d+)/\d+", line)
                    if m:
                        vm_ip = m.group(1)
                break

        exit_code = channel.recv_exit_status()
        full_output = "\n".join(output_lines)

        if exit_code != 0:
            return failure(
                f"mq_vm.py p2p_install failed (exit {exit_code})",
                data={"output": full_output},
            )

        if "Installation Successful" not in full_output:
            return failure(
                "mq_vm.py p2p_install did not report success",
                data={"output": full_output},
            )

        if vm_ip:
            state.set_output("vm_ip", vm_ip)

        return success({
            "install_complete": True,
            "vm_ip": vm_ip,
            "acs_state": config.acs_state,
            "output": full_output,
        })

    except Exception as e:
        return failure(f"mq_vm.py p2p_install raised: {e}")