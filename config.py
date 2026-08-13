# config.py
# ─────────────────────────────────────────────────────────────
# VM configuration structure, defaults, and helpers.
# ─────────────────────────────────────────────────────────────

from dataclasses import dataclass, field, asdict
from typing import Optional


# Fields the user MUST provide (or that we must resolve) before execution.
REQUIRED_FIELDS = [
    "vm_name",
    "memory_gb",
    "num_cpu",
    "disk_size",
    "os_image",
    "vm_type",
    "vm_username",
    "vm_password",
]

# Optional fields (never block execution).
OPTIONAL_FIELDS = [
    "disk_path",
    "aic_cards",   # how many AIC cards to attach (0 = attach all found)
    "acs_state",   # only meaningful for p2p
    "debug",
]

# Human-friendly prompts used by input_collector when a field is missing.
FIELD_PROMPTS = {
    "vm_name":     "What should the VM be named?",
    "memory_gb":   "How much memory do you need? (e.g. 32 for 32GB)",
    "num_cpu":     "How many CPUs?",
    "disk_size":   "What disk size? (e.g. 100G)",
    "os_image":    "OS image path or distro name? (e.g. /home/vm_images/ubuntu.iso or 'ubuntu')",
    "aic_cards":   "How many AIC cards to attach?",
    "vm_type":     "VM type? (normal / p2p)",
    "vm_username": "VM login username?",
    "vm_password": "VM login password?",
    "acs_state":   "ACS state for p2p? (enable / disable)",
}


@dataclass
class VMConfig:
    """Holds all VM provisioning parameters with sensible defaults."""

    vm_name: Optional[str] = None
    memory_gb: Optional[int] = None
    num_cpu: Optional[int] = None
    disk_size: Optional[str] = "100G"          # default disk size
    disk_path: Optional[str] = None            # optional existing qcow2
    os_image: Optional[str] = None
    aic_cards: Optional[int] = None
    vm_type: str = "normal"                     # normal | p2p
    acs_state: Optional[str] = None             # p2p only
    vm_username: Optional[str] = "ubuntu"       # default username
    vm_password: Optional[str] = None
    debug: str = "enable"                       # default debug on

    # ── Helpers ──────────────────────────────────────────────

    @property
    def memory_mb(self) -> Optional[int]:
        """virt-install expects MB. Convert GB → MB."""
        if self.memory_gb is None:
            return None
        return int(self.memory_gb) * 1024

    @property
    def is_p2p(self) -> bool:
        return self.vm_type == "p2p"

    def missing_required_fields(self) -> list:
        """Return the list of required fields still unset (None)."""
        missing = []
        for f in REQUIRED_FIELDS:
            if getattr(self, f) in (None, ""):
                missing.append(f)
        # acs_state is required ONLY for p2p VMs
        if self.is_p2p and self.acs_state in (None, ""):
            missing.append("acs_state")
        return missing

    def update(self, data: dict) -> None:
        """Apply non-null values from a dict onto this config."""
        for key, value in (data or {}).items():
            if value is None:
                continue
            if hasattr(self, key):
                setattr(self, key, value)

    def to_dict(self) -> dict:
        return asdict(self)

    def summary(self) -> str:
        """Human-readable confirmation summary."""
        lines = [
            "Here are your VM specs:",
            f"  • Name      : {self.vm_name}",
            f"  • Memory    : {self.memory_gb}GB ({self.memory_mb}MB)",
            f"  • CPUs      : {self.num_cpu}",
            f"  • Disk      : {self.disk_size}",
            f"  • OS Image  : {self.os_image}",
            f"  • AIC Cards : {self.aic_cards}",
            f"  • VM Type   : {self.vm_type}",
            f"  • Username  : {self.vm_username}",
        ]
        if self.disk_path:
            lines.append(f"  • Disk Path : {self.disk_path}")
        if self.is_p2p:
            lines.append(f"  • ACS State : {self.acs_state}")
        return "\n".join(lines)


def default_config() -> VMConfig:
    return VMConfig()