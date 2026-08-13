# prompts.py
# ─────────────────────────────────────────────────────────────
# All LLM prompt templates live here.
# ─────────────────────────────────────────────────────────────

EXTRACT_PROMPT = """
You are a VM provisioning assistant for a Linux KVM system.

Extract the following fields from the user message.
Return ONLY a valid JSON object. No explanation. No extra text. ONLY JSON.

Fields to extract:
- vm_name       (string or null)
- memory_gb     (number in GB or null)
- num_cpu       (number or null)
- disk_size     (string like "100G" or null)
- disk_path     (string path to existing .qcow2 or null)
- os_image      (string: full path to ISO, or "ubuntu"/"rhel"/"centos", or null)
- aic_cards     (number or null)
- vm_type       ("normal" or "p2p" — default: "normal")
- acs_state     ("enable" / "disable" or null)
- vm_username   (string or null)
- vm_password   (string or null)

Critical rules:
- vm_name must be an EXPLICIT name given by the user (e.g. "named vm1", "called dev01").
  Do NOT treat action verbs or typos of action verbs as vm_name.
  Words like "create", "ctre", "creat", "make", "spin", "build", "install" are ACTION words, not names.
  If no explicit name is given, vm_name = null.

Smart understanding rules:
- "quad core"       -> num_cpu: 4
- "dual core"       -> num_cpu: 2
- "a couple"        -> 2
- "half a tb"       -> disk_size: "500G"
- "1 tb"            -> disk_size: "1000G"
- "32GB RAM"        -> memory_gb: 32
- "p2p" / "peer"    -> vm_type: "p2p"
- "2 cards"/"2 aic" -> aic_cards: 2
- Handle typos: "ctre"="create", "ubunt"="ubuntu", "memry"="memory", etc.
- If not mentioned  -> null

User message: "{user_input}"
"""


# Used when the agent asks the user to confirm the final specs.
CONFIRMATION_INTRO = "Please review the VM specifications below."