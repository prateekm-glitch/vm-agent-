# state.py
# ─────────────────────────────────────────────────────────────
# Tracks execution progress of the orchestrator: which tools are
# pending / running / done / failed, plus outputs produced by tools
# (e.g. the VM's IP address).
# ─────────────────────────────────────────────────────────────

from enum import Enum
from typing import Any, Dict, List, Optional


class ToolStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class AgentState:
    """Holds the runtime state of a provisioning run."""

    def __init__(self, plan: Optional[List[str]] = None):
        # ordered list of tool names to execute
        self.plan: List[str] = plan or []
        # status per tool
        self.status: Dict[str, ToolStatus] = {
            name: ToolStatus.PENDING for name in self.plan
        }
        # arbitrary outputs collected during the run (e.g. vm_ip, pci_xml paths)
        self.outputs: Dict[str, Any] = {}
        # error message if the run stops
        self.error: Optional[str] = None

    # ── Plan management ──────────────────────────────────────

    def set_plan(self, plan: List[str]) -> None:
        self.plan = list(plan)
        self.status = {name: ToolStatus.PENDING for name in self.plan}

    # ── Status transitions ───────────────────────────────────

    def mark_running(self, tool_name: str) -> None:
        self.status[tool_name] = ToolStatus.RUNNING

    def mark_done(self, tool_name: str) -> None:
        self.status[tool_name] = ToolStatus.DONE

    def mark_failed(self, tool_name: str, error: str = "") -> None:
        self.status[tool_name] = ToolStatus.FAILED
        if error:
            self.error = f"{tool_name}: {error}"

    def is_done(self, tool_name: str) -> bool:
        return self.status.get(tool_name) == ToolStatus.DONE

    # ── Outputs ──────────────────────────────────────────────

    def set_output(self, key: str, value: Any) -> None:
        self.outputs[key] = value

    def get_output(self, key: str, default: Any = None) -> Any:
        return self.outputs.get(key, default)

    def record_result(self, tool_name: str, result: dict) -> None:
        """Store a tool's returned data payload under its name and merge
        any dict data into the shared outputs so later tools can use it."""
        data = (result or {}).get("data")
        if data is not None:
            self.outputs[tool_name] = data
            if isinstance(data, dict):
                self.outputs.update(data)

    # ── Progress helpers ─────────────────────────────────────

    def completed_count(self) -> int:
        return sum(1 for s in self.status.values() if s == ToolStatus.DONE)

    def all_done(self) -> bool:
        return bool(self.plan) and all(
            self.status[name] == ToolStatus.DONE for name in self.plan
        )

    def progress_line(self) -> str:
        return f"{self.completed_count()}/{len(self.plan)} tools completed"

    def summary(self) -> str:
        lines = ["Execution state:"]
        for name in self.plan:
            symbol = {
                ToolStatus.PENDING: "⏳",
                ToolStatus.RUNNING: "🔄",
                ToolStatus.DONE: "✅",
                ToolStatus.FAILED: "❌",
            }[self.status[name]]
            lines.append(f"  {symbol} {name}")
        if self.error:
            lines.append(f"Error: {self.error}")
        return "\n".join(lines)