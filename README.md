# VM Agent — AI VM Provisioning Console

An AI-powered web application that provisions Linux KVM virtual machines on a **remote host** via SSH. Powered by Claude (Anthropic) for intelligent narration.

## Quick Start

```bash
pip install -r requirements.txt
# Place mq_vm.py in this directory (alongside web.py)
python web.py
# Open http://localhost:5000
```

By default runs in **DRY_RUN** mode (simulates all commands). To execute real commands:

```bash
export VM_AGENT_DRY_RUN=0
python web.py
```

## How It Works

```
Step 1 — Connect:  Enter KVM host IP/user/pass → SSH in → apt install deps →
                   fetch RAM/CPU/disk/AIC info → Claude formats summary

Step 2 — Configure: Fill VM form (resource limits auto-set from host data) →
                    click "Start Building"

Step 3 — Build:    7 tools execute in sequence over SSH → VM ready
```

## Project Structure

```
agent/
├── web.py                  # Flask web server (entry point)
├── mq_vm.py                # ← Place here: automation script for VM install
├── templates/index.html    # 3-step web UI (Connect → Configure → Build)
├── orchestrator.py         # Builds tool plan, runs tools with retry + narration
├── state.py                # Tracks tool execution state
├── config.py               # VMConfig dataclass + defaults
├── config_validator.py     # Validates config before execution
├── settings.py             # Environment variables + DRY_RUN flag
├── llm_client.py           # Shared Claude API client
├── input_extractor.py      # LLM config extraction (legacy, unused in new UI)
├── prompts.py              # LLM prompt templates
├── requirements.txt        # Python dependencies
└── tools/                  # 8 provisioning tools (all SSH-based)
    ├── __init__.py                     # Tool registry
    ├── base.py                         # Standard {status, data, error} contract
    ├── tool_download_iso.py            # Check/download Ubuntu ISO on remote host
    ├── tool_run_mq_vm_install.py       # Upload + run mq_vm.py install via SSH
    ├── tool_run_mq_vm_p2p_install.py   # Upload + run mq_vm.py p2p_install via SSH
    ├── tool_shutdown_vm.py             # virsh shutdown via SSH
    ├── tool_list_pci_devices.py        # lspci on remote host via SSH
    ├── tool_generate_pci_xml.py        # Write hostdev XML files on remote host
    ├── tool_attach_pci_devices.py      # virsh attach-device via SSH
    └── tool_start_vm.py                # virsh start via SSH
```

## Configuration

Set via environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `VM_AGENT_DRY_RUN` | `1` | Set to `0` for real execution |
| `ANTHROPIC_API_KEY` | dev token | Claude API key |
| `ANTHROPIC_BASE_URL` | qgenie gateway | API endpoint |
| `VM_AGENT_MODEL` | `anthropic::claude-4-6-sonnet` | Model ID |
| `VM_AGENT_MAX_RETRIES` | `3` | Retry attempts per tool |
| `VM_AGENT_RETRY_SLEEP` | `5` | Seconds between retries |
| `VM_WORK_DIR` | `/home/vm_image` | Working directory on KVM host |
| `ISO_URL` | Ubuntu 24.04 URL | ISO download URL |
| `MQ_VM_SCRIPT_LOCAL` | `\\blrsweng1\bdcqranium\users\prateek\mq_vm.py` | Network share path to mq_vm.py |

## Tool Execution Order

**Normal VM (7 tools):**
```
tool_download_iso → tool_run_mq_vm_install → tool_shutdown_vm →
tool_list_pci_devices → tool_generate_pci_xml → tool_attach_pci_devices → tool_start_vm
```

**p2p VM (7 tools, different install):**
```
tool_download_iso → tool_run_mq_vm_p2p_install → tool_shutdown_vm →
tool_list_pci_devices → tool_generate_pci_xml → tool_attach_pci_devices → tool_start_vm
```

## What `mq_vm.py` Does

The `mq_vm.py` script (uploaded to the KVM host at runtime) handles the heavy lifting:

- **`install`**: Creates disk image → runs `virt-install` → waits for OS install → shuts down → starts VM → fetches IP → waits for boot
- **`p2p_install`**: Same as `install` + runs `discover_switch.py` + `acs.sh enable|disable`

## Prerequisites

### On the agent machine (where `web.py` runs):
- Python 3.8+
- `pip install -r requirements.txt`
- `mq_vm.py` must be accessible at `\\blrsweng1\bdcqranium\users\prateek\mq_vm.py` (network share must be mounted)
- Override path with `set MQ_VM_SCRIPT_LOCAL=C:\path\to\mq_vm.py` if needed

### On the KVM host (auto-installed on connect):
- Ubuntu 22.04/24.04
- The agent auto-runs: `apt install qemu-kvm qemu-utils libvirt-daemon-system libvirt-clients virtinst bridge-utils python3-paramiko genisoimage`
- IOMMU enabled for PCI passthrough
- `discover_switch.py` and `acs.sh` scripts (p2p only, must be in `/home/vm_image/`)

## Production Deployment

```bash
export VM_AGENT_DRY_RUN=0
pip install gunicorn
gunicorn -w 1 --threads 4 -b 0.0.0.0:5000 web:app
```

## Dependencies

```
anthropic
httpx
paramiko
flask