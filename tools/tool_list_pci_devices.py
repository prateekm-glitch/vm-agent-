# tool_list_pci_devices.py
# ─────────────────────────────────────────────────────────────
# Smart PCI device discovery:
#   1. Run qmonitor-proxy setup to ensure vfio bindings are ready
#   2. Query all defined VMs to find PCI devices already assigned
#   3. Read /sys/kernel/iommu_groups/ to map devices to IOMMU groups
#   4. Filter: only offer devices whose ENTIRE IOMMU group is free
#   5. Select first available IOMMU-viable group (or config-specified group)
#
# Config:
#   pci_group: Which contiguous IOMMU group to use (1=first, 2=second)
#              Default: 1 (uses the first available free group)
# ─────────────────────────────────────────────────────────────

import re

from tools.base import success, failure, ssh_run_sudo
from settings import DRY_RUN


def _ssh_run(ssh, cmd):
    """Basic SSH command without sudo."""
    transport = ssh.get_transport()
    channel = transport.open_session()
    channel.set_combine_stderr(True)
    channel.exec_command(cmd)
    raw = channel.makefile("rb").read()
    output = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
    exit_code = channel.recv_exit_status()
    return exit_code, output.strip()


def _get_all_qualcomm_devices(ssh):
    """Run lspci and return list of SBDF addresses for Qualcomm devices."""
    _, output = _ssh_run(ssh, "lspci | grep -i qualcomm")
    sbdfs = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^([0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-9a-fA-F])", line)
        if m:
            sbdfs.append("0000:" + m.group(1))
    return sbdfs


def _get_iommu_groups(ssh):
    """Build a map: {bus_hex_str: iommu_group_number}.
    
    Reads /sys/kernel/iommu_groups/ to determine which IOMMU group each PCI bus belongs to.
    """
    cmd = (
        "for iommu_group in $(find /sys/kernel/iommu_groups/ -maxdepth 1 -mindepth 1 -type d); do "
        "grp=$(basename $iommu_group); "
        "for device in $(ls $iommu_group/devices/ 2>/dev/null); do "
        "echo \"$grp $device\"; "
        "done; "
        "done"
    )
    _, output = _ssh_run(ssh, cmd)
    bus_to_group = {}
    for line in output.splitlines():
        parts = line.strip().split()
        if len(parts) == 2:
            grp, dev = parts
            # dev format: 0000:0a:00.0
            m = re.match(r"^0000:([0-9a-fA-F]{2}):", dev)
            if m:
                bus_to_group[m.group(1).lower()] = int(grp)
    return bus_to_group


def _get_used_pci_devices(ssh, host_password):
    """Query only RUNNING VMs and return the set of PCI buses already in use.
    
    Only running VMs actually hold PCI devices. Shut-off VMs release their
    PCI devices when stopped, so we must NOT count them as "in use".
    
    Returns set of "bus_hex" strings (e.g. {"0a", "0b", "0c"}).
    """
    # Only check RUNNING VMs — shut-off VMs don't hold PCI devices
    exit_code, list_out = ssh_run_sudo(ssh, "virsh list --state-running --name", host_password)
    if exit_code != 0:
        return set()

    vm_names = [n.strip() for n in list_out.splitlines() if n.strip()]
    used_buses = set()

    for vm in vm_names:
        exit_code, xml = ssh_run_sudo(ssh, f"virsh dumpxml {vm}", host_password)
        if exit_code != 0 or not xml:
            continue
        # Find all <hostdev> source addresses: bus='0x0a'
        matches = re.findall(r"<source>\s*<address\s+domain='0x0000'\s+bus='0x([0-9a-fA-F]+)'", xml)
        for bus in matches:
            used_buses.add(bus.lower().zfill(2))

    return used_buses


def _try_qmonitor_setup(ssh, host_password):
    """Try to initialize qmonitor-proxy service (Qualcomm AIC vfio setup).
    
    Best-effort — if it fails, we continue anyway. Some servers don't have this.
    """
    print("   [pci] Setting up qmonitor-proxy (Qualcomm AIC vfio bindings)...", flush=True)
    # Reload qaic driver
    ssh_run_sudo(ssh, "modprobe -r qaic 2>/dev/null || true", host_password)
    ssh_run_sudo(ssh, "modprobe qaic 2>/dev/null || true", host_password)
    # Stop and restart qmonitor
    exit_code, out = ssh_run_sudo(
        ssh, "/opt/qti-aic/scripts/qaic-monitor-service.sh stop 2>/dev/null || true", host_password
    )
    exit_code, out = ssh_run_sudo(
        ssh, "/opt/qti-aic/scripts/qaic-monitor-service.sh start 2>/dev/null || true", host_password
    )
    if "initialized all devices" in out.lower() or "success" in out.lower():
        print("   [pci] ✅ qmonitor-proxy: all devices initialized", flush=True)
    elif "could initialize only" in out.lower():
        print(f"   [pci] ⚠ qmonitor-proxy: partial init — {out[-200:]}", flush=True)
    else:
        print("   [pci] ⚠ qmonitor-proxy not available (skipping)", flush=True)


def run(config, state):
    if DRY_RUN:
        count = int(getattr(config, 'aic_cards', None) or 4)
        fake_sbdfs = [f"0000:{i+3:02x}:00.0" for i in range(count)]
        print(f"   [DRY_RUN] simulating lspci — {fake_sbdfs}")
        state.set_output("pci_sbdf_list", fake_sbdfs)
        return success({"pci_devices": fake_sbdfs})

    ssh = state.get_output("host_ssh")
    if ssh is None:
        return failure("No SSH connection to host.")

    host_password = state.get_output("host_password", "")

    # ─── Step 1: Ensure qmonitor-proxy is running (best-effort) ────
    _try_qmonitor_setup(ssh, host_password)

    # ─── Step 2: Get ALL Qualcomm devices ────────────────────────
    print("   [pci] Running: lspci | grep -i qualcomm", flush=True)
    all_sbdfs = _get_all_qualcomm_devices(ssh)
    if not all_sbdfs:
        return failure("No Qualcomm PCI devices found via lspci")

    print(f"   [pci] Total Qualcomm devices on host: {len(all_sbdfs)}", flush=True)

    # ─── Step 3: Get IOMMU group mapping ─────────────────────────
    print("   [pci] Reading IOMMU groups...", flush=True)
    bus_to_iommu = _get_iommu_groups(ssh)

    # ─── Step 4: Find USED PCI devices (across all VMs) ──────────
    print("   [pci] Checking which devices are already used by other VMs...", flush=True)
    used_buses = _get_used_pci_devices(ssh, host_password)
    if used_buses:
        print(f"   [pci] Used by existing VMs: {sorted(used_buses)}", flush=True)
    else:
        print("   [pci] No devices currently in use by any VM", flush=True)

    # ─── Step 5: Determine which IOMMU groups are FREE ───────────
    # Also mark ANY IOMMU group as USED if it contains any used bus (whole group blocked)
    used_iommu_groups = set()
    for used_bus in used_buses:
        if used_bus in bus_to_iommu:
            used_iommu_groups.add(bus_to_iommu[used_bus])

    if used_iommu_groups:
        print(f"   [pci] IOMMU groups blocked (in use): {sorted(used_iommu_groups)}", flush=True)

    # ─── Step 6: Group Qualcomm devices by IOMMU group ───────────
    iommu_to_devices = {}  # iommu_group → list of SBDFs
    for sbdf in all_sbdfs:
        parts = sbdf.split(":")
        bus_hex = parts[1].lower()
        iommu_grp = bus_to_iommu.get(bus_hex)
        if iommu_grp is None:
            continue
        if iommu_grp in used_iommu_groups:
            continue  # Skip blocked groups
        iommu_to_devices.setdefault(iommu_grp, []).append(sbdf)

    # ─── Step 6b: Determine cards per VM ─────────────────────────
    # Default: 4 cards per VM (standard AIC config)
    aic_count = getattr(config, 'aic_cards', None)
    try:
        cards_per_vm = int(aic_count) if aic_count else 4
    except (ValueError, TypeError):
        cards_per_vm = 4

    # ─── Step 6c: Build virtual groups ───────────────────────────
    # Case A: Groups already have 4+ cards (gb-292-blr-18 style: 8/group, gb-blr-70: 4/group)
    # Case B: Groups have 1 card each (gb-292-blr-17 style) — collect N consecutive groups
    multi_card_groups = [
        (grp, sorted(devs, key=lambda s: int(s.split(":")[1], 16)))
        for grp, devs in iommu_to_devices.items()
        if len(devs) >= cards_per_vm
    ]
    multi_card_groups.sort(key=lambda x: x[0])

    single_card_groups = sorted(
        [(grp, devs[0]) for grp, devs in iommu_to_devices.items() if len(devs) == 1],
        key=lambda x: int(x[1].split(":")[1], 16)  # sort by bus address
    )

    if multi_card_groups:
        # Standard case: each IOMMU group has enough cards
        free_groups = multi_card_groups
        print(f"   [pci] Mode: multi-card groups ({len(free_groups)} groups)", flush=True)
    elif len(single_card_groups) >= cards_per_vm:
        # Single-card-per-group case: collect N consecutive single-card groups
        free_groups = []
        for i in range(0, len(single_card_groups) - cards_per_vm + 1, cards_per_vm):
            chunk = single_card_groups[i:i + cards_per_vm]
            # Use the first group's IOMMU number as the "group ID"
            virtual_grp = chunk[0][0]
            virtual_devs = [c[1] for c in chunk]
            free_groups.append((virtual_grp, virtual_devs))
        print(
            f"   [pci] Mode: single-card groups → {len(free_groups)} virtual groups "
            f"of {cards_per_vm} cards each",
            flush=True,
        )
    else:
        return failure(
            f"No free IOMMU groups available with {cards_per_vm}+ Qualcomm devices. "
            f"Blocked groups: {sorted(used_iommu_groups)}"
        )

    if not free_groups:
        return failure(
            f"No free IOMMU groups available. Blocked: {sorted(used_iommu_groups)}"
        )

    print(f"   [pci] Available FREE groups: {len(free_groups)}", flush=True)
    for i, (grp, devs) in enumerate(free_groups[:5], 1):
        bus_list = [s.split(":")[1] for s in devs]
        print(f"   [pci]   Group #{i} (IOMMU {grp}): {bus_list}", flush=True)

    # ─── Step 7: Select which group to use ───────────────────────
    pci_group_idx = int(getattr(config, 'pci_group', None) or 1)
    if pci_group_idx < 1 or pci_group_idx > len(free_groups):
        print(f"   [pci] pci_group={pci_group_idx} invalid, using first free group", flush=True)
        pci_group_idx = 1

    selected_iommu, selected_sbdfs = free_groups[pci_group_idx - 1]

    print(
        f"   [pci] ✅ Selected group {selected_iommu}: {selected_sbdfs} "
        f"({len(selected_sbdfs)} devices)",
        flush=True,
    )

    state.set_output("pci_sbdf_list", selected_sbdfs)
    state.set_output("pci_iommu_group", selected_iommu)
    return success({
        "pci_devices": selected_sbdfs,
        "iommu_group": selected_iommu,
        "free_groups_count": len(free_groups),
        "used_iommu_groups": sorted(used_iommu_groups),
    })
