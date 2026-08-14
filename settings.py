# settings.py
# ─────────────────────────────────────────────────────────────
# Central runtime settings: LLM credentials, DRY_RUN mode,
# and paths for the remote KVM host workflow.
# ─────────────────────────────────────────────────────────────

import os

# ── LLM settings ─────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ.get(
    "ANTHROPIC_API_KEY",
    "",  # enter your api key 
)
ANTHROPIC_BASE_URL = os.environ.get(
    "ANTHROPIC_BASE_URL",
    "https://qgenie-api.qualcomm.com/",
)
MODEL = os.environ.get("VM_AGENT_MODEL", "anthropic::claude-4-6-sonnet")

# ── Execution mode ───────────────────────────────────────────
# Defaults to dry-run so it is safe to run on Windows during dev.
DRY_RUN = os.environ.get("VM_AGENT_DRY_RUN", "1") not in ("0", "false", "False")

# ── Agent Mode (Phase 2) ─────────────────────────────────────
# When True: Claude decides each tool step dynamically (looping agentic loop)
# When False: Classic hardcoded plan (default, reliable)
# Set VM_AGENT_MODE=1 to enable, or change to True below for testing.
AGENT_MODE = os.environ.get("VM_AGENT_MODE", "0") not in ("0", "false", "False")
#AGENT_MODE = True
#DRY_RUN = True
# ── Orchestrator behaviour ───────────────────────────────────
MAX_RETRIES = int(os.environ.get("VM_AGENT_MAX_RETRIES", "3"))
RETRY_SLEEP_SECONDS = int(os.environ.get("VM_AGENT_RETRY_SLEEP", "5"))

# ── Remote KVM host paths ────────────────────────────────────
# Local path to mq_vm.py on the Windows network share.
# Override with MQ_VM_SCRIPT_LOCAL env var if needed.
MQ_VM_SCRIPT_LOCAL = os.environ.get(
    "MQ_VM_SCRIPT_LOCAL",
    r"\\blrsweng1\bdcqranium\users\prateek\mq_vm.py",
)

# Working directory on the KVM host where ISO, disk images, and scripts live.
VM_WORK_DIR = os.environ.get("VM_WORK_DIR", "/home/vm_images")

# Ubuntu ISO URL and filename.
ISO_URL = os.environ.get(
    "ISO_URL",
    "https://releases.ubuntu.com/noble/ubuntu-24.04.3-live-server-amd64.iso",
)
ISO_FILENAME = os.environ.get("ISO_FILENAME", "ubuntu-24.04.3-live-server-amd64.iso")

# Full path to ISO on the KVM host.
ISO_PATH = f"{VM_WORK_DIR}/{ISO_FILENAME}"

# APT packages required on the KVM host.
KVM_APT_PACKAGES = (
    "qemu-kvm qemu-utils libvirt-daemon-system libvirt-clients "
    "virtinst bridge-utils python3-paramiko genisoimage"
)