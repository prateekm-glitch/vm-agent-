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
# Dry-run defaults to OFF so real commands execute against the host.
# Set VM_AGENT_DRY_RUN=1 to re-enable simulation.
DRY_RUN = os.environ.get("VM_AGENT_DRY_RUN", "0") not in ("0", "false", "False")

# ── Agent Mode (Phase 2) ─────────────────────────────────────
# When True: Claude decides each tool step dynamically (looping agentic loop)
# When False: Classic hardcoded plan
# Agent mode defaults to ON; set VM_AGENT_MODE=0 for classic mode.
AGENT_MODE = os.environ.get("VM_AGENT_MODE", "1") not in ("0", "false", "False")
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

# Optional Slack/Teams/Discord-style incoming webhook for build notifications.
# Set SLACK_WEBHOOK_URL in the Keys tab to enable. Payload is {"text": msg}.
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")

# Optional email notifications (Resend) for build completion. Users can also
# set a per-build notify email in the Configure VM form; that is combined
# with the API key below to send the summary email.
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
RESEND_FROM = os.environ.get("RESEND_FROM", "VM Agent <onboarding@resend.dev>")
RESEND_API_URL = os.environ.get("RESEND_API_URL", "https://api.resend.com/emails")

# APT packages required on the KVM host.
KVM_APT_PACKAGES = (
    "qemu-kvm qemu-utils libvirt-daemon-system libvirt-clients "
    "virtinst bridge-utils python3-paramiko genisoimage"
)