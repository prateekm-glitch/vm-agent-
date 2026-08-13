# tool_generate_pci_xml.py
# ─────────────────────────────────────────────────────────────
# Generate libvirt hostdev XML files on the remote KVM host using
# the real SBDF addresses discovered by tool_list_pci_devices.
# XML files are written to VM_WORK_DIR on the host via SSH.
#
# Filename format: {vm_name}_pci{bus}.xml
# Example: gb39_ubuntu_091_pci03.xml
# ─────────────────────────────────────────────────────────────

from tools.base import success, failure, ssh_run_sudo
from settings import DRY_RUN, VM_WORK_DIR


def _build_xml(sbdf):
    """Build a libvirt hostdev XML string from an SBDF address."""
    parts = sbdf.split(":")
    domain = parts[0] if len(parts) == 3 else "0000"
    bus = parts[1] if len(parts) == 3 else parts[0]
    slot_func = parts[2] if len(parts) == 3 else parts[1]
    slot, func = slot_func.split(".")

    return (
        "<hostdev mode='subsystem' type='pci' managed='yes'>\n"
        "<source>\n"
        f"<address domain='0x{domain}' bus='0x{bus}'"
        f" slot='0x{slot}' function='0x{func}'/>\n"
        "</source>\n"
        "</hostdev>\n"
    )


def _get_bus(sbdf):
    """Extract the 2-digit bus number from an SBDF address."""
    parts = sbdf.split(":")
    return parts[1] if len(parts) == 3 else parts[0]


def run(config, state):
    sbdf_list = state.get_output("pci_sbdf_list") or []

    if not sbdf_list:
        count = int(config.aic_cards or 0)
        if count == 0:
            return success({"pci_xml_files": [], "note": "no AIC cards requested"})
        sbdf_list = [f"0000:{i+3:02x}:00.0" for i in range(count)]

    xml_files = []

    for i, sbdf in enumerate(sbdf_list):
        xml_content = _build_xml(sbdf)
        bus = _get_bus(sbdf)
        remote_path = f"{VM_WORK_DIR}/{config.vm_name}_pci{bus}.xml"

        if DRY_RUN:
            print(f"   [DRY_RUN] would write PCI XML {remote_path}")
        else:
            ssh = state.get_output("host_ssh")
            if ssh is None:
                return failure("No SSH connection to host.")

            host_password = state.get_output("host_password", "")
            # Use tee to write the file content (sudo via PTY + stdin password)
            cmd = f"tee {remote_path} > /dev/null << 'XMLEOF'\n{xml_content}XMLEOF"
            exit_code, output = ssh_run_sudo(ssh, cmd, host_password)
            if exit_code != 0:
                return failure(f"Failed to write XML {remote_path}: {output}")

            print(f"   Written PCI XML: {remote_path}")

        xml_files.append(remote_path)

    state.set_output("pci_xml_files", xml_files)
    return success({"pci_xml_files": xml_files})