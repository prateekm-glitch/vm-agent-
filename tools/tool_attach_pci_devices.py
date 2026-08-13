# tool_attach_pci_devices.py
# ─────────────────────────────────────────────────────────────
# Attach each AIC card XML to the VM via virsh attach-device
# over SSH on the KVM host.
# ─────────────────────────────────────────────────────────────

from tools.base import success, failure, ssh_run_sudo
from settings import DRY_RUN


def run(config, state):
    xml_files = state.get_output("pci_xml_files") or []

    if not xml_files:
        return success({"attached": 0, "note": "no PCI XML files to attach"})

    if DRY_RUN:
        for xml in xml_files:
            print(f"   [DRY_RUN] would run: virsh attach-device {config.vm_name} {xml} --persistent")
        return success({"attached": len(xml_files)})

    ssh = state.get_output("host_ssh")
    if ssh is None:
        return failure("No SSH connection to host.")

    host_password = state.get_output("host_password", "")
    attached = 0
    for xml in xml_files:
        cmd = f"virsh attach-device {config.vm_name} {xml} --persistent"
        exit_code, output = ssh_run_sudo(ssh, cmd, host_password)
        if exit_code != 0:
            return failure(
                f"Failed to attach {xml}: {output}",
                data={"attached": attached},
            )
        print(f"   Attached PCI device: {xml}")
        attached += 1

    return success({"attached": attached})