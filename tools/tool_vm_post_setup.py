# tool_vm_post_setup.py
# ─────────────────────────────────────────────────────────────
# Run post-installation setup commands on the VM by SSHing
# through the KVM host (since the VM's 192.168.122.x IP is only
# reachable from the host, not from the laptop running Flask).
#
# Steps:
#   1. Create /etc/netplan/01-netcfg.yaml with static IP config
#   2. chmod 600 + chown root:root
#   3. netplan generate + netplan apply
#   4. sudo apt full-upgrade -y
#
# This should run AFTER PCI devices are attached and VM is started.
# ─────────────────────────────────────────────────────────────

import time

from tools.base import success, failure, ssh_run_sudo
from settings import DRY_RUN


def _log(msg=""):
    """Print and push to live log stream if available."""
    try:
        from tools.installer_autopilot import log
        log(msg)
    except Exception:
        print(msg, flush=True)


NETPLAN_TEMPLATE = """network:
  version: 2
  renderer: networkd
  ethernets:
    enp1s0:
      addresses:
        - {vm_ip}/21
      routes:
        - to: default
          via: 10.131.24.1
      nameservers:
        addresses:
          - 8.8.8.8
          - 8.8.4.4
"""


def _ssh_run_on_vm(host_ssh, vm_ip, vm_user, vm_pass, cmd, host_password, timeout=120):
    """Run a command on the VM by hopping through the KVM host.
    
    Uses: ssh from KVM host → VM (since VM is only reachable from host).
    Uses sshpass for non-interactive password auth.
    """
    # Use sshpass to provide VM password, -o StrictHostKeyChecking=no to skip host key prompt
    hop_cmd = (
        f"sshpass -p '{vm_pass}' ssh -o StrictHostKeyChecking=no -o ConnectTimeout=30 "
        f"{vm_user}@{vm_ip} '{cmd}'"
    )
    # Run via sudo on host (sshpass might need to be installed)
    transport = host_ssh.get_transport()
    channel = transport.open_session()
    channel.get_pty()
    channel.set_combine_stderr(True)
    channel.settimeout(timeout)
    channel.exec_command(hop_cmd)

    output = ""
    while True:
        if channel.recv_ready():
            chunk = channel.recv(4096).decode("utf-8", errors="replace")
            output += chunk
            for line in chunk.splitlines():
                stripped = line.strip()
                if stripped:
                    print(f"   [vm] {stripped}", flush=True)
        elif channel.exit_status_ready():
            while channel.recv_ready():
                chunk = channel.recv(4096).decode("utf-8", errors="replace")
                output += chunk
            break
        else:
            time.sleep(1)

    exit_code = channel.recv_exit_status()
    return exit_code, output


def _ssh_run_sudo_on_vm(host_ssh, vm_ip, vm_user, vm_pass, cmd, host_password, timeout=120):
    """Run a sudo command on the VM by hopping through the KVM host."""
    # Use sshpass + echo password | sudo -S for the VM command
    sudo_cmd = f"echo '{vm_pass}' | sudo -S {cmd}"
    return _ssh_run_on_vm(host_ssh, vm_ip, vm_user, vm_pass, sudo_cmd, host_password, timeout)


def run(config, state):
    if DRY_RUN:
        print("   [DRY_RUN] would run post-setup commands on VM via KVM host")
        return success({"netplan_configured": True, "upgraded": True})

    # Get VM connection details
    vm_ip = state.get_output("vm_ip")
    if not vm_ip:
        return failure("No VM IP available. Run install first.")

    ssh = state.get_output("host_ssh")
    if ssh is None:
        return failure("No SSH connection to KVM host.")

    host_password = state.get_output("host_password", "")
    username = config.vm_username or "ubuntu"
    password = config.vm_password or "ubuntu"

    _log(f"   [vm_post_setup] Will SSH to VM {vm_ip} through KVM host...")

    # Ensure sshpass is installed on the host (needed for non-interactive SSH to VM)
    ssh_run_sudo(ssh, "apt-get install -y -qq sshpass 2>/dev/null", host_password)

    # ─── Wait for VM SSH to be ready (retry up to 3 minutes) ────
    _log(f"   [vm_post_setup] Waiting for VM SSH to be ready at {vm_ip}...")
    ssh_ready = False
    max_wait_attempts = 18   # 18 × 10s = 3 minutes
    for attempt in range(1, max_wait_attempts + 1):
        exit_code, _ = _ssh_run_on_vm(
            ssh, vm_ip, username, password, "echo ok", host_password, timeout=10
        )
        if exit_code == 0:
            _log(f"   [vm_post_setup] ✅ VM SSH is ready (attempt {attempt})")
            ssh_ready = True
            break
        _log(f"   [vm_post_setup] VM SSH not ready yet (attempt {attempt}/{max_wait_attempts}, exit={exit_code}) — waiting 10s...")
        time.sleep(10)

    if not ssh_ready:
        return failure(
            f"VM SSH at {vm_ip} did not become ready after {max_wait_attempts * 10}s. "
            f"VM is running but SSH is not accepting connections. "
            f"Try running post-setup manually: ssh {username}@{vm_ip}"
        )

    # ─── Step 1: Create netplan config with static IP ───────────
    _log(f"   [vm_post_setup] Creating /etc/netplan/01-netcfg.yaml (IP: {vm_ip}/21)...")

    netplan_content = NETPLAN_TEMPLATE.format(vm_ip=vm_ip)

    # Write to temp file on VM, then sudo mv
    write_cmd = f"cat > /tmp/01-netcfg.yaml << 'EOF'\n{netplan_content}EOF"
    exit_code, _ = _ssh_run_on_vm(ssh, vm_ip, username, password, write_cmd, host_password, timeout=30)
    if exit_code != 0:
        _log(f"   [vm_post_setup] ⚠ Failed to write temp netplan file")
    else:
        # Move with sudo
        exit_code, _ = _ssh_run_sudo_on_vm(
            ssh, vm_ip, username, password,
            "mv /tmp/01-netcfg.yaml /etc/netplan/01-netcfg.yaml",
            host_password, timeout=15
        )
        if exit_code == 0:
            _log(f"   [vm_post_setup] ✅ Netplan config created!")

    # ─── Step 2: Set permissions and apply netplan ─────────────
    _log(f"   [vm_post_setup] Setting permissions (chmod 600, chown root:root)...")
    _ssh_run_sudo_on_vm(ssh, vm_ip, username, password, "chmod 600 /etc/netplan/01-netcfg.yaml", host_password, timeout=10)
    _ssh_run_sudo_on_vm(ssh, vm_ip, username, password, "chown root:root /etc/netplan/01-netcfg.yaml", host_password, timeout=10)

    _log(f"   [vm_post_setup] Running netplan generate...")
    _ssh_run_sudo_on_vm(ssh, vm_ip, username, password, "netplan generate", host_password, timeout=30)

    _log(f"   [vm_post_setup] Running netplan apply...")
    exit_code, _ = _ssh_run_sudo_on_vm(ssh, vm_ip, username, password, "netplan apply", host_password, timeout=30)
    if exit_code == 0:
        _log(f"   [vm_post_setup] ✅ Netplan applied!")
    else:
        _log(f"   [vm_post_setup] ⚠ netplan apply returned non-zero")

    # ─── Step 3: Run apt full-upgrade ──────────────────────────
    _log(f"   [vm_post_setup] Running: sudo apt full-upgrade -y")
    _log(f"   [vm_post_setup] This may take several minutes...")

    exit_code, output = _ssh_run_sudo_on_vm(
        ssh, vm_ip, username, password,
        "apt full-upgrade -y",
        host_password, timeout=600
    )

    if exit_code != 0:
        return failure(
            f"apt full-upgrade failed (exit {exit_code})",
            data={"output": output[-2000:]},
        )

    _log(f"   [vm_post_setup] ✅ apt full-upgrade completed!")
    return success({
        "netplan_configured": True,
        "vm_ip": vm_ip,
        "upgraded": True,
    })