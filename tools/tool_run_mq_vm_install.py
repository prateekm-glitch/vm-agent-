# tool_run_mq_vm_install.py
# ─────────────────────────────────────────────────────────────
# Uploads mq_vm.py to the KVM host via SFTP, then runs:
#   python3 /home/vm_image/mq_vm.py install --name ... --memory ...
# over SSH. Streams stdout and parses the VM IP from output.
# ─────────────────────────────────────────────────────────────

import os
import re

from tools.base import success, failure
from settings import DRY_RUN, MQ_VM_SCRIPT_LOCAL, VM_WORK_DIR


def _upload_script(ssh):
    """SFTP mq_vm.py to the user's home dir on the host (avoids permission issues)."""
    local_path = MQ_VM_SCRIPT_LOCAL
    if not os.path.exists(local_path):
        raise FileNotFoundError(
            f"mq_vm.py not found at '{local_path}'. "
            "Place mq_vm.py in the agent directory alongside web.py."
        )
    # Upload to /tmp which is always writable
    remote_path = "/tmp/mq_vm.py"
    sftp = ssh.open_sftp()
    try:
        sftp.put(local_path, remote_path)
        sftp.chmod(remote_path, 0o755)
    finally:
        sftp.close()
    return remote_path


def _build_cmd(config, iso_path=None):
    """Build the mq_vm.py install command string."""
    parts = [
        f"cd {VM_WORK_DIR} &&",
        "sudo python3 /tmp/mq_vm.py install",
        f"--name {config.vm_name}",
        f"--memory {config.memory_mb}",
        f"--num_cpu {config.num_cpu}",
    ]
    if config.disk_path:
        parts.append(f"--disk_path {config.disk_path}")
    else:
        parts.append(f"--disk_size {config.disk_size}")

    # Use the ISO path resolved by tool_download_iso (may differ from config.os_image
    # if the user typed a wrong path but the ISO was found at the default location)
    resolved_iso = (
        iso_path                               # set by tool_download_iso via state
        or config.os_image                     # user-specified fallback
        or f"{VM_WORK_DIR}/ubuntu-24.04.3-live-server-amd64.iso"  # hardcoded default
    )
    parts.append(f"--os_image {resolved_iso}")

    debug = getattr(config, "debug", "enable") or "enable"
    parts.append(f"--debug {debug}")

    return " ".join(parts)


def run(config, state):
    # Resolve ISO path: use what tool_download_iso found, or fall back to config
    iso_path = state.get_output("iso_path") or config.os_image

    if DRY_RUN:
        cmd = _build_cmd(config, iso_path)
        print(f"   [DRY_RUN] would upload mq_vm.py and run: {cmd}")
        return success({"vm_ip": "192.168.122.50", "install_complete": True})

    ssh = state.get_output("host_ssh")
    if ssh is None:
        return failure("No SSH connection to host. Connect first via /api/connect.")

    # Upload mq_vm.py
    try:
        remote_script = _upload_script(ssh)
        print(f"   Uploaded mq_vm.py → {remote_script}", flush=True)
    except Exception as e:
        return failure(f"Failed to upload mq_vm.py: {e}")

    # Build and run the install command
    cmd = _build_cmd(config, iso_path)
    print(f"   $ {cmd}", flush=True)

    # Initialize the autopilot log file NOW so ALL mq_vm output is captured from the start
    from tools.installer_autopilot import log as _mq_log, _init_log
    _init_log()
    _mq_log(f"=== VM Creation: {config.vm_name} ===")
    _mq_log(f"   $ {cmd}")

    try:
        # Use a long timeout — virt-install can take 30+ minutes
        transport = ssh.get_transport()
        channel = transport.open_session()
        channel.get_pty()  # Request PTY so sudo works
        channel.set_combine_stderr(True)
        channel.exec_command(cmd)

        # Send password for sudo prompt
        password = state.get_output("host_password", "")
        if password:
            import time as _time
            _time.sleep(1)  # Wait for sudo prompt
            channel.sendall(f"{password}\n".encode())

        import time as _t
        _mq_log("   [mq_vm] --- waiting for virt-install to start ---")

        # Wait for mq_vm.py to launch virt-install and the installer console to appear
        # Read initial output (disk creation, virt-install command echo, etc.)
        initial_output = ""
        deadline = _t.time() + 60  # Wait up to 60s for installer to start
        installer_started = False

        while _t.time() < deadline:
            if channel.recv_ready():
                chunk = channel.recv(4096).decode("utf-8", errors="replace")
                initial_output += chunk
                for line in chunk.splitlines():
                    if line.strip() and not line.strip().startswith("[sudo]"):
                        _mq_log(f"   [mq_vm] {line}")

                # PRIORITY: check for "VM Already exist" FIRST before checking installer
                if "VM Already exist" in initial_output or "VM already exist" in initial_output:
                    print("   [mq_vm] VM already exists, skipping to post-install flow", flush=True)
                    break

                # Check if we're in the actual installer TUI
                if "rich mode" in initial_output.lower() or "Subiquity" in initial_output or "continue in basic mode" in initial_output.lower():
                    # Only trigger if NOT "VM Already exist" in the same output
                    if "VM Already exist" not in initial_output and "VM already exist" not in initial_output:
                        installer_started = True
                        print("   [mq_vm] 🎯 Installer console detected!", flush=True)
                        break
            else:
                _t.sleep(0.5)

        if not installer_started:
            # Maybe mq_vm.py finished without needing installer (VM already existed)
            if "Installation Successful" in initial_output:
                vm_ip = None
                m = re.search(r"(\d+\.\d+\.\d+\.\d+)/\d+", initial_output)
                if m:
                    vm_ip = m.group(1)
                    state.set_output("vm_ip", vm_ip)
                return success({"install_complete": True, "vm_ip": vm_ip})

            if "VM Already exist" in initial_output or "VM already exist" in initial_output:
                # Don't return immediately — let mq_vm.py finish its own handling
                # (it may undefine/recreate the VM). We'll read remaining output below.
                print("   [mq_vm] VM already exists, waiting for mq_vm.py to finish...", flush=True)

            else:
                # Installer didn't start within 60s — keep streaming normally
                print("   [mq_vm] ⚠ Installer TUI not detected, streaming raw output...", flush=True)

        # === AUTOPILOT: Navigate the Ubuntu installer with Claude ===
        if installer_started:
            from tools.installer_autopilot import run_autopilot
            autopilot_success, autopilot_output = run_autopilot(
                channel, config, max_steps=40, initial_screen=initial_output
            )
            initial_output += autopilot_output

            if not autopilot_success:
                return failure(
                    "Installer autopilot did not complete successfully",
                    data={"output": initial_output[-2000:]},
                )

            # Autopilot completed successfully (installer rebooted the VM).
            # virt-install has exited so the original channel no longer shows the VM's
            # console. Reconnect via virsh console on a NEW channel (same SSH connection)
            # to handle the CDROM unmount prompt and wait for the login prompt.
            from tools.installer_autopilot import log as autopilot_log, wait_for_text_passive
            autopilot_log("   [mq_vm] ✅ Autopilot completed!")
            autopilot_log("   [mq_vm] 🔌 Reconnecting to VM console via virsh console (same SSH)...")

            try:
                transport = ssh.get_transport()
                console_channel = transport.open_session()
                console_channel.get_pty(width=220, height=50)
                console_channel.set_combine_stderr(True)
                console_channel.exec_command(f"sudo virsh console {config.vm_name} --force")

                # Handle sudo password prompt if needed
                host_password = state.get_output("host_password", "")
                if host_password:
                    _t.sleep(1)
                    console_channel.sendall(f"{host_password}\n".encode())

                _t.sleep(3)  # Wait for virsh console to connect to the VM

                # Press Enter to get console attention (in case it's waiting for input)
                console_channel.sendall(b"\n")
                _t.sleep(1)

                # wait_for_text_passive logs ALL output in real-time AND auto-handles
                # the CDROM unmount prompt by pressing Enter when detected.
                # Credentials are sent ONLY if "login:" is actually found.
                autopilot_log("   [mq_vm] ⏳ Waiting for login prompt via virsh console (up to 300s)...")
                found_login, console_screen = wait_for_text_passive(
                    console_channel, "login:", timeout=300
                )

                if found_login:
                    autopilot_log(f"   [mq_vm] ✅ Login prompt detected! Sending credentials...")
                    console_channel.sendall(f"{config.vm_username}\n".encode())
                    _t.sleep(3)
                    console_channel.sendall(f"{config.vm_password}\n".encode())
                    _t.sleep(3)
                    autopilot_log("   [mq_vm] ✅ Login credentials sent!")
                else:
                    autopilot_log("   [mq_vm] ⚠ Login prompt not found within 300s — skipping credentials")

                console_channel.close()

            except Exception as _e:
                autopilot_log(f"   [mq_vm] ⚠ Console reconnect failed: {_e}")

            autopilot_log("   [mq_vm] --- reading post-install output (mq_vm.py finalization) ---")

        # Read remaining mq_vm.py output (post-install or no-installer flow)
        # After autopilot: consider success when we see login prompt or IP in cloud-init
        _mq_log("   [mq_vm] --- reading remaining output ---")
        remaining_output = ""
        vm_booted = False
        deadline = _t.time() + 600  # Wait up to 10 min for mq_vm.py to finish post-install
        while _t.time() < deadline:
            try:
                if channel.recv_ready():
                    chunk = channel.recv(4096).decode("utf-8", errors="replace")
                    remaining_output += chunk
                    for line in chunk.splitlines():
                        if line.strip():
                            _mq_log(f"   [mq_vm] {line}")

                    # Check for completion markers in accumulated output
                    combined = initial_output + remaining_output
                    if "Installation Successful" in combined:
                        print("   [mq_vm] ✅ 'Installation Successful' detected!", flush=True)
                        vm_booted = True
                        break
                    if "ubuntu login:" in combined.lower() or "login:" in combined[-500:].lower():
                        print("   [mq_vm] ✅ Login prompt detected — VM booted successfully!", flush=True)
                        vm_booted = True
                        break

                elif channel.exit_status_ready():
                    _t.sleep(0.5)
                    while channel.recv_ready():
                        chunk = channel.recv(65536).decode("utf-8", errors="replace")
                        remaining_output += chunk
                        for line in chunk.splitlines():
                            if line.strip():
                                _mq_log(f"   [mq_vm] {line}")
                    break
                else:
                    _t.sleep(1)
            except (OSError, EOFError) as e:
                print(f"   [mq_vm] Connection closed: {e}", flush=True)
                break

        try:
            exit_code = channel.recv_exit_status()
        except Exception:
            exit_code = 0  # Assume success if channel died

        full_output = initial_output + remaining_output
        print(f"   [mq_vm] --- exit code: {exit_code} ---", flush=True)

        # Parse VM IP from cloud-init output or virsh output
        vm_ip = None
        vm_mac = None
        for line in full_output.splitlines():
            # Match virsh format: vnet2  52:54:00:be:d1:80  ipv4  192.168.122.96/24
            m = re.search(r"([0-9a-fA-F:]{17})\s+\w+\s+(\d+\.\d+\.\d+\.\d+)/\d+", line)
            if m:
                vm_mac = m.group(1)
                vm_ip = m.group(2)
            # Match cloud-init format: | enp1s0 | True | 192.168.122.165 | 255.255.255.0 |
            elif "enp" in line and not vm_ip:
                m2 = re.search(r"\|\s*(\d+\.\d+\.\d+\.\d+)\s*\|", line)
                if m2 and not m2.group(1).startswith("127."):
                    vm_ip = m2.group(1)
            elif not vm_ip:
                # Generic fallback: IP/mask pattern
                m3 = re.search(r"(\d+\.\d+\.\d+\.\d+)/\d+", line)
                if m3 and not m3.group(1).startswith("127."):
                    vm_ip = m3.group(1)

        # Consider success if:
        # 1. "Installation Successful" was found (mq_vm.py completed), OR
        # 2. VM booted (login prompt detected), OR
        # 3. Installer autopilot completed and we got past reboot
        install_success = (
            "Installation Successful" in full_output or
            vm_booted or
            (installer_started and exit_code == 0)
        )

        if not install_success and exit_code != 0:
            return failure(
                f"mq_vm.py install failed (exit {exit_code})",
                data={"output": full_output[-2000:]},
            )

        if not install_success:
            return failure(
                "mq_vm.py install did not detect successful completion",
                data={"output": full_output[-2000:]},
            )

        # Store IP and MAC in state for later tools
        if vm_ip:
            state.set_output("vm_ip", vm_ip)
            print(f"   [mq_vm] ✅ VM IP: {vm_ip}", flush=True)
        if vm_mac:
            state.set_output("vm_mac", vm_mac)
            print(f"   [mq_vm] ✅ VM MAC: {vm_mac}", flush=True)

        return success({
            "install_complete": True,
            "vm_ip": vm_ip,
            "vm_mac": vm_mac,
        })

    except Exception as e:
        return failure(f"mq_vm.py install raised: {e}")
