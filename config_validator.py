# config_validator.py
# ─────────────────────────────────────────────────────────────
# Validates a fully-collected VMConfig before the orchestrator runs.
# Returns (is_valid, list_of_errors).
# ─────────────────────────────────────────────────────────────

from typing import List, Tuple

from config import VMConfig


def validate(config: VMConfig) -> Tuple[bool, List[str]]:
    errors: List[str] = []

    # 1. All required fields present
    missing = config.missing_required_fields()
    if missing:
        errors.append(f"Missing required fields: {', '.join(missing)}")

    # 2. Numeric sanity
    if config.memory_gb is not None and int(config.memory_gb) <= 0:
        errors.append("memory_gb must be greater than 0")
    if config.num_cpu is not None and int(config.num_cpu) <= 0:
        errors.append("num_cpu must be greater than 0")
    # aic_cards is optional; validate only if provided
    if config.aic_cards is not None and int(config.aic_cards) < 0:
        errors.append("aic_cards cannot be negative")

    # 3. vm_type must be valid
    if config.vm_type not in ("normal", "p2p"):
        errors.append("vm_type must be 'normal' or 'p2p'")

    # 4. acs_state rules
    if config.is_p2p:
        if config.acs_state not in ("enable", "disable"):
            errors.append("p2p VMs require acs_state to be 'enable' or 'disable'")
    else:
        # acs_state is meaningless for normal VMs; not an error but normalize.
        pass

    # 5. disk_size format (e.g. "100G")
    if config.disk_size and not str(config.disk_size)[-1:].upper() in ("G", "M", "T"):
        errors.append("disk_size should end with a unit (e.g. '100G')")

    # 6. os_image present
    if not config.os_image:
        errors.append("os_image is required (path to ISO or distro name)")

    return (len(errors) == 0, errors)