# tool_start_vm.py — start the VM via SSH on the KVM host.
from tools.base import success, failure, ssh_run_sudo
from settings import DRY_RUN


def run(config, state):
    if DRY_RUN:
        print(f"   [DRY_RUN] would run: virsh start {config.vm_name}")
        return success({"started": True})

    ssh = state.get_output("host_ssh")
    if ssh is None:
        return failure("No SSH connection to host.")

    host_password = state.get_output("host_password", "")

    # Stop qmonitor before starting VM (as per mq_vm.py)
    ssh_run_sudo(ssh, "/opt/qti-aic/scripts/qaic-monitor-service.sh stop", host_password)

    exit_code, output = ssh_run_sudo(ssh, f"virsh start {config.vm_name}", host_password)
    if exit_code != 0:
        return failure(f"virsh start failed: {output}")

    print(f"   VM {config.vm_name} started.")
    return success({"started": True})