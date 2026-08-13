# tool_schemas.py
# ─────────────────────────────────────────────────────────────
# Tool schemas for the Phase 2 agentic loop.
#
# WHAT IS THIS?
# These are JSON descriptions of each tool that we give to Claude.
# Claude reads these schemas to understand:
#   - What each tool does
#   - What parameters it accepts
#   - When to use it
#
# WHY DO WE NEED THIS?
# Without schemas, Claude can only guess what tools exist.
# With schemas, Claude knows exactly what to call and with what parameters.
# This is the "menu" we hand to Claude before the agentic loop starts.
#
# FORMAT (Anthropic tool-use format):
#   {
#     "name": "tool_name",           ← must match TOOL_REGISTRY key exactly
#     "description": "...",          ← Claude reads this to decide when to use it
#     "input_schema": {              ← JSON Schema for the parameters
#       "type": "object",
#       "properties": {...},
#       "required": [...]
#     }
#   }
# ─────────────────────────────────────────────────────────────

TOOL_SCHEMAS = [
    {
        "name": "tool_download_iso",
        "description": (
            "Check if the Ubuntu ISO exists on the KVM host. "
            "If it exists, skip download. If not, download it from the internet. "
            "Always run this FIRST before installing a VM. "
            "Returns the resolved ISO path that should be used for installation."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "os_image": {
                    "type": "string",
                    "description": (
                        "Path to the Ubuntu ISO on the KVM host. "
                        "Default: /home/vm_images/ubuntu-24.04.3-live-server-amd64.iso"
                    ),
                }
            },
            "required": [],
        },
    },
    {
        "name": "tool_run_mq_vm_install",
        "description": (
            "Install a new Ubuntu VM on the KVM host using mq_vm.py. "
            "This uploads mq_vm.py to the host, runs virt-install, and "
            "automatically navigates the Ubuntu installer (17 steps). "
            "Takes 30-60 minutes. Run AFTER tool_download_iso. "
            "Returns the VM's IP address and MAC address when complete."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "vm_name": {
                    "type": "string",
                    "description": "Name for the new VM (e.g. gb39_ubuntu_vm01)",
                },
                "memory_mb": {
                    "type": "integer",
                    "description": "RAM in MB (e.g. 32768 for 32GB)",
                },
                "num_cpu": {
                    "type": "integer",
                    "description": "Number of vCPUs (e.g. 4)",
                },
                "disk_size": {
                    "type": "string",
                    "description": "Disk size with unit (e.g. '100G', '200G')",
                },
                "os_image": {
                    "type": "string",
                    "description": "Path to Ubuntu ISO on the host",
                },
            },
            "required": ["vm_name", "memory_mb", "num_cpu", "disk_size", "os_image"],
        },
    },
    {
        "name": "tool_run_mq_vm_p2p_install",
        "description": (
            "Install a new Ubuntu VM configured for P2P (Peer-to-Peer) mode. "
            "Same as tool_run_mq_vm_install but uses P2P-specific mq_vm.py parameters. "
            "Use this instead of tool_run_mq_vm_install when vm_type is 'p2p'. "
            "P2P allows multiple AIC cards to communicate directly with each other."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "vm_name": {
                    "type": "string",
                    "description": "Name for the new VM",
                },
                "memory_mb": {
                    "type": "integer",
                    "description": "RAM in MB",
                },
                "num_cpu": {
                    "type": "integer",
                    "description": "Number of vCPUs",
                },
                "disk_size": {
                    "type": "string",
                    "description": "Disk size with unit (e.g. '100G')",
                },
                "os_image": {
                    "type": "string",
                    "description": "Path to Ubuntu ISO on the host",
                },
                "acs_state": {
                    "type": "string",
                    "description": "ACS state: 'enable' or 'disable'. For P2P, use 'disable'.",
                    "enum": ["enable", "disable"],
                },
            },
            "required": ["vm_name", "memory_mb", "num_cpu", "disk_size", "os_image"],
        },
    },
    {
        "name": "tool_shutdown_vm",
        "description": (
            "Shut down a running VM gracefully using virsh shutdown. "
            "Run this AFTER installation is complete and BEFORE attaching PCI devices. "
            "The VM must be shut off before PCI devices can be attached persistently."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "vm_name": {
                    "type": "string",
                    "description": "Name of the VM to shut down",
                }
            },
            "required": ["vm_name"],
        },
    },
    {
        "name": "tool_list_pci_devices",
        "description": (
            "Find available Qualcomm AIC (AI Compute) cards on the KVM host. "
            "Reads IOMMU groups to find free devices not already used by other VMs. "
            "Handles servers with 1, 4, or 8 cards per IOMMU group automatically. "
            "Also runs qmonitor-proxy setup to ensure vfio bindings are ready. "
            "Run this AFTER shutting down the VM. "
            "Returns a list of PCI device addresses (SBDFs) to attach to the VM."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "aic_cards": {
                    "type": "integer",
                    "description": (
                        "Number of AIC cards to assign to the VM. "
                        "Default: 4. Use 8 for servers with 8 cards per IOMMU group."
                    ),
                },
                "pci_group": {
                    "type": "integer",
                    "description": (
                        "Which free IOMMU group to select (1=first available, 2=second, etc.). "
                        "Default: 1. Increase if first group fails."
                    ),
                },
            },
            "required": [],
        },
    },
    {
        "name": "tool_generate_pci_xml",
        "description": (
            "Generate XML configuration files for each Qualcomm AIC card. "
            "These XML files are required by virsh to attach PCI devices to the VM. "
            "Run this AFTER tool_list_pci_devices. "
            "Creates one XML file per device in the VM work directory."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "tool_attach_pci_devices",
        "description": (
            "Attach Qualcomm AIC cards to the VM using virsh attach-device. "
            "Uses the XML files created by tool_generate_pci_xml. "
            "Run this AFTER tool_generate_pci_xml and BEFORE tool_start_vm. "
            "The VM must be shut off when attaching devices persistently."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "tool_start_vm",
        "description": (
            "Start the VM using virsh start. "
            "Run this AFTER attaching PCI devices. "
            "The VM will boot with the AIC cards attached. "
            "Returns success/failure."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "vm_name": {
                    "type": "string",
                    "description": "Name of the VM to start",
                }
            },
            "required": ["vm_name"],
        },
    },
    {
        "name": "tool_vm_post_setup",
        "description": (
            "Run post-installation setup on the VM. "
            "SSHes into the VM through the KVM host and: "
            "1. Creates /etc/netplan/01-netcfg.yaml with static IP config "
            "2. Applies netplan (network configuration) "
            "3. Runs sudo apt full-upgrade -y (system updates) "
            "Run this LAST, after the VM is started. "
            "Takes 5-15 minutes due to apt upgrade."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
]


# ─── Tool name → schema lookup ────────────────────────────────
TOOL_SCHEMA_MAP = {t["name"]: t for t in TOOL_SCHEMAS}


def get_tool_schema(name: str) -> dict:
    """Get the schema for a specific tool by name."""
    return TOOL_SCHEMA_MAP.get(name, {})


def get_schemas_for_plan(plan: list) -> list:
    """Get only the schemas for tools in the current plan.
    
    This reduces token usage by only sending relevant tool schemas to Claude.
    
    Args:
        plan: List of tool names (e.g. ['tool_download_iso', 'tool_run_mq_vm_install'])
    
    Returns:
        List of tool schemas for the tools in the plan
    """
    return [TOOL_SCHEMA_MAP[name] for name in plan if name in TOOL_SCHEMA_MAP]