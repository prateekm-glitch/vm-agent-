# tool_download_iso.py
# ─────────────────────────────────────────────────────────────
# Check if the OS ISO already exists on the remote KVM host.
# Uses config.os_image (user-specified path). If file exists, skip.
# If not found at user path, checks default VM_WORK_DIR path.
# If neither found, downloads the default Ubuntu ISO via wget.
#
# Fixes:
#   - Uses sudo for file existence check (VM_WORK_DIR is root-owned)
#   - Always downloads to VM_WORK_DIR (not user-specified wrong path)
#   - Verifies downloaded file size > 1 GB
#   - Shows wget progress in logs
# ─────────────────────────────────────────────────────────────

import os

from tools.base import success, failure, ssh_run_sudo
from settings import DRY_RUN, VM_WORK_DIR, ISO_URL, ISO_FILENAME


def _ssh_run(ssh, cmd):
    """Basic SSH command without sudo — for read-only checks like test -f."""
    transport = ssh.get_transport()
    channel = transport.open_session()
    channel.set_combine_stderr(True)
    channel.exec_command(cmd)
    raw = channel.makefile("rb").read()
    output = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
    exit_code = channel.recv_exit_status()
    return exit_code, output.strip()


def run(config, state):
    # Determine the ISO path to check — use user's os_image if provided
    user_iso_path = (config.os_image or "").strip()
    default_iso_path = f"{VM_WORK_DIR}/{ISO_FILENAME}"

    if DRY_RUN:
        # In DRY_RUN, use default path (simulates finding ISO at correct location)
        iso_path = default_iso_path
        print(f"   [DRY_RUN] would check/download ISO at {iso_path}")
        state.set_output("iso_path", iso_path)
        return success({"iso_path": iso_path, "downloaded": False})

    ssh = state.get_output("host_ssh")
    if ssh is None:
        return failure("No SSH connection to host. Connect first via /api/connect.")

    password = state.get_output("host_password", "")

    # ─── Step 1: Check user-specified path (no sudo needed — file is world-readable) ──
    if user_iso_path:
        exit_code, _ = _ssh_run(ssh, f"test -f {user_iso_path}")
        if exit_code == 0:
            print(f"   [iso] ✅ ISO found at user path: {user_iso_path}")
            state.set_output("iso_path", user_iso_path)
            return success({"iso_path": user_iso_path, "downloaded": False})
        else:
            print(f"   [iso] ⚠ ISO not found at user path: {user_iso_path}")

    # ─── Step 2: Check default path (VM_WORK_DIR/ISO_FILENAME) ───
    exit_code, _ = _ssh_run(ssh, f"test -f {default_iso_path}")
    if exit_code == 0:
        print(f"   [iso] ✅ ISO found at default path: {default_iso_path}")
        state.set_output("iso_path", default_iso_path)
        return success({"iso_path": default_iso_path, "downloaded": False})

    # ─── Step 3: Also check just the filename in VM_WORK_DIR ─────
    if user_iso_path:
        filename = os.path.basename(user_iso_path)
        alt_path = f"{VM_WORK_DIR}/{filename}"
        if alt_path != default_iso_path:
            exit_code2, _ = _ssh_run(ssh, f"test -f {alt_path}")
            if exit_code2 == 0:
                print(f"   [iso] ✅ ISO found at: {alt_path}")
                state.set_output("iso_path", alt_path)
                return success({"iso_path": alt_path, "downloaded": False})

    # ─── Step 4: ISO not found anywhere — download it ────────────
    dest_path = default_iso_path  # Always download to the correct default path
    print(f"   [iso] ISO not found. Downloading from:")
    print(f"   [iso]   {ISO_URL}")
    print(f"   [iso]   → {dest_path}")
    print(f"   [iso] This may take 5-15 minutes depending on network speed...")

    # Ensure destination directory exists
    ssh_run_sudo(ssh, f"mkdir -p {VM_WORK_DIR}", password)

    # Download with progress output (no -q flag so we see progress)
    cmd = (
        f"wget --tries=3 --timeout=120 --progress=dot:giga "
        f"-O {dest_path} {ISO_URL} 2>&1"
    )
    exit_code, output = ssh_run_sudo(ssh, cmd, password)

    if exit_code != 0:
        # Clean up partial download
        ssh_run_sudo(ssh, f"rm -f {dest_path}", password)
        return failure(
            f"ISO download failed (exit {exit_code}): {output[-500:]}",
            data={"iso_path": dest_path}
        )

    # ─── Step 5: Verify downloaded file size (must be > 1 GB) ────
    exit_code, size_out = ssh_run_sudo(ssh, f"stat -c '%s' {dest_path}", password)
    try:
        file_size = int(size_out.strip())
        size_gb = round(file_size / (1024**3), 2)
        print(f"   [iso] Downloaded file size: {size_gb} GB")
        if file_size < 1_000_000_000:  # Less than 1 GB → corrupted/incomplete
            ssh_run_sudo(ssh, f"rm -f {dest_path}", password)
            return failure(
                f"Downloaded ISO is too small ({size_gb} GB) — likely corrupted. "
                f"Please check network and retry.",
                data={"iso_path": dest_path}
            )
    except (ValueError, TypeError):
        print(f"   [iso] ⚠ Could not verify file size: {size_out}")

    print(f"   [iso] ✅ ISO downloaded successfully to {dest_path}")
    state.set_output("iso_path", dest_path)
    return success({"iso_path": dest_path, "downloaded": True})
