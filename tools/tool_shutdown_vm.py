# tool_shutdown_vm.py — shut down the VM via SSH on the KVM host.
from tools.base import success, failure, ssh_run_sudo
from settings import DRY_RUN


def run(config, state):
    if DRY_RUN:
        print(f"   [DRY_RUN] would run: virsh shutdown {config.vm_name}")
        return success({"shutdown": True})

    ssh = state.get_output("host_ssh")
    if ssh is None:
        return failure("No SSH connection to host.")

    host_password = state.get_output("host_password", "")

    # Try graceful shutdown first
    exit_code, output = ssh_run_sudo(ssh, f"virsh shutdown {config.vm_name}", host_password)
    if exit_code == 0:
        print(f"   VM {config.vm_name} shutdown initiated.")
        return success({"shutdown": True})

    # If graceful fails (e.g. already off), try destroy
    exit_code2, output2 = ssh_run_sudo(ssh, f"virsh destroy {config.vm_name}", host_password)
    if exit_code2 == 0:
        print(f"   VM {config.vm_name} force-stopped (virsh destroy).")
        return success({"shutdown": True, "forced": True})

    # If both fail, check if it's already off
    exit_code3, state_out = ssh_run_sudo(ssh, f"virsh domstate {config.vm_name}", host_password)
    if "shut off" in state_out:
        print(f"   VM {config.vm_name} is already shut off.")
        return success({"shutdown": True, "already_off": True})

    # If domain doesn't exist at all, treat as already gone
    if "failed to get domain" in output or "Domain not found" in output or "failed to get domain" in output2 or "Domain not found" in output2:
        print(f"   VM {config.vm_name} domain not found — treating as already shut off.")
        return success({"shutdown": True, "domain_not_found": True})

    return failure(f"Could not shut down VM: {output} / {output2}")