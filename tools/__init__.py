# tools/__init__.py
# ─────────────────────────────────────────────────────────────
# Registry that maps tool names to their run() functions.
# The orchestrator uses TOOL_REGISTRY to look up and execute tools.
# ─────────────────────────────────────────────────────────────

from tools import (
    tool_download_iso,
    tool_run_mq_vm_install,
    tool_run_mq_vm_p2p_install,
    tool_shutdown_vm,
    tool_start_vm,
    tool_list_pci_devices,
    tool_generate_pci_xml,
    tool_attach_pci_devices,
    tool_vm_post_setup,
)

TOOL_REGISTRY = {
    "tool_download_iso":            tool_download_iso.run,
    "tool_run_mq_vm_install":       tool_run_mq_vm_install.run,
    "tool_run_mq_vm_p2p_install":   tool_run_mq_vm_p2p_install.run,
    "tool_shutdown_vm":             tool_shutdown_vm.run,
    "tool_start_vm":                tool_start_vm.run,
    "tool_list_pci_devices":        tool_list_pci_devices.run,
    "tool_generate_pci_xml":        tool_generate_pci_xml.run,
    "tool_attach_pci_devices":      tool_attach_pci_devices.run,
    "tool_vm_post_setup":           tool_vm_post_setup.run,
}
