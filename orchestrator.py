# orchestrator.py
# ─────────────────────────────────────────────────────────────
# The "brain" of the VM Agent.
#
# TWO MODES:
#
# 1. CLASSIC MODE (original hardcoded plan):
#    - build_plan() returns a fixed list of tools
#    - web.py loops through them in order
#    - Simple, reliable, no AI decision-making
#    - Used when AGENT_MODE = False in settings.py
#
# 2. AGENT MODE (Phase 2 — looping agentic loop):
#    - run_agentic_loop() lets Claude decide each step
#    - Claude reads the result of each tool and decides what to do next
#    - Can recover from errors by trying different parameters
#    - Used when AGENT_MODE = True in settings.py
#
# ARCHITECTURE (Gen 2 — Looping):
#    User → Claude → Tool → Result → Claude → Tool → Result → ...
#    Claude holds all context in conversation history.
#    No plan written upfront — emerges dynamically.
#
# ─────────────────────────────────────────────────────────────

import json
import os
import sys
import time
from datetime import datetime

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from state import AgentState
from tools import TOOL_REGISTRY
from settings import MAX_RETRIES, RETRY_SLEEP_SECONDS

# ─── Classic Mode Plans ───────────────────────────────────────
# These are used when AGENT_MODE = False (default).
# The web.py loop runs these tools in order.

NORMAL_PLAN = [
    "tool_download_iso",
    "tool_run_mq_vm_install",
    "tool_shutdown_vm",
    "tool_list_pci_devices",
    "tool_generate_pci_xml",
    "tool_attach_pci_devices",
    "tool_start_vm",
    "tool_vm_post_setup",
]

P2P_PLAN = [
    "tool_download_iso",
    "tool_run_mq_vm_p2p_install",
    "tool_shutdown_vm",
    "tool_list_pci_devices",
    "tool_generate_pci_xml",
    "tool_attach_pci_devices",
    "tool_start_vm",
    "tool_vm_post_setup",
]


def build_plan(config) -> list:
    """Return the ordered list of tools for Classic Mode."""
    if config.is_p2p:
        return list(P2P_PLAN)
    return list(NORMAL_PLAN)


# ─── Narration (used by both modes) ──────────────────────────

def _narrate_step(tool_name, result, progress, config):
    """Ask Claude for a brief conversational status update after each step."""
    try:
        from llm_client import ask
        prompt = (
            f"You are narrating VM provisioning progress to the user. "
            f"VM name: {config.vm_name}. "
            f"Tool '{tool_name}' just completed with status: {result['status']}. "
            f"Progress: {progress}. "
            f"Give a brief one-line status update (conversational, friendly, <15 words). "
            f"No emojis. No code."
        )
        return ask(prompt, max_tokens=40, temperature=0.3)
    except Exception:
        return None


# ─── Agent Mode: Looping Agentic Loop ────────────────────────

# Maximum tool calls per session (safety limit to prevent infinite loops)
MAX_AGENT_ITERATIONS = 20

# Audit log for agent decisions
AGENT_LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
AGENT_LOG_PATH = os.path.join(AGENT_LOG_DIR, "agent_decisions.log")


def _log_agent_decision(iteration, tool_name, tool_input, result_status, reason="", duration_s=0):
    """Append Claude's decision to the audit log (raw JSON for machine reading)."""
    try:
        os.makedirs(AGENT_LOG_DIR, exist_ok=True)
        with open(AGENT_LOG_PATH, "a", encoding="utf-8") as f:
            entry = {
                "timestamp": datetime.now().isoformat(),
                "iteration": iteration,
                "tool": tool_name,
                "input": tool_input,
                "result": result_status,
                "reason": reason,
                "duration_s": round(duration_s, 1),
            }
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def _log_agent_session_start(vm_name: str) -> float:
    """Write session header to agent_decisions.log. Returns start timestamp."""
    start_time = time.time()
    try:
        os.makedirs(AGENT_LOG_DIR, exist_ok=True)
        with open(AGENT_LOG_PATH, "a", encoding="utf-8") as f:
            f.write("\n")
            f.write("=" * 80 + "\n")
            f.write(f"AGENT SESSION — {vm_name} — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write("=" * 80 + "\n")
            f.write(f"{'Iter':<5} {'Tool':<28} {'Input':<30} {'Result':<10} {'Duration'}\n")
            f.write(f"{'─'*4:<5} {'─'*27:<28} {'─'*29:<30} {'─'*8:<10} {'─'*8}\n")
    except Exception:
        pass
    return start_time


def _log_agent_session_end(vm_name: str, start_time: float, final_status: str,
                            vm_ip: str = "", iterations: int = 0):
    """Write session summary footer to agent_decisions.log."""
    total_s = round(time.time() - start_time)
    mins, secs = divmod(total_s, 60)
    duration_str = f"{mins}m {secs}s" if mins else f"{secs}s"
    try:
        with open(AGENT_LOG_PATH, "a", encoding="utf-8") as f:
            f.write("─" * 80 + "\n")
            ip_part = f" — IP: {vm_ip}" if vm_ip and vm_ip != "unknown" else ""
            f.write(f"RESULT: {final_status.upper()}{ip_part} — {iterations} steps — Total: {duration_str}\n")
            f.write("=" * 80 + "\n")
    except Exception:
        pass


def _log_agent_step(iteration: int, tool_name: str, tool_input: dict,
                    result_status: str, duration_s: float):
    """Write a formatted table row to agent_decisions.log."""
    try:
        # Compact input representation
        if tool_input:
            input_str = ", ".join(f"{k}={v}" for k, v in list(tool_input.items())[:3])
            if len(input_str) > 28:
                input_str = input_str[:25] + "..."
        else:
            input_str = "—"
        tool_short = tool_name.replace("tool_", "")
        dur_str = f"{round(duration_s)}s"
        with open(AGENT_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{iteration:<5} {tool_short:<28} {input_str:<30} {result_status.upper():<10} {dur_str}\n")
    except Exception:
        pass


def _build_system_prompt(config) -> str:
    """Build the system prompt that tells Claude its goal and constraints.
    
    This is the most important part of the agentic loop.
    Claude reads this ONCE at the start and uses it throughout.
    """
    vm_type = "P2P" if config.is_p2p else "standard"
    return f"""You are an AI agent that provisions Ubuntu VMs with Qualcomm AIC cards on KVM servers.

YOUR GOAL: Provision a {vm_type} VM with the following configuration:
- VM Name: {config.vm_name}
- Memory: {config.memory_mb} MB ({config.memory_mb // 1024} GB)
- CPUs: {config.num_cpu}
- Disk: {config.disk_size}
- AIC Cards: {config.aic_cards or 4}
- VM Type: {config.vm_type}
- OS Image: {config.os_image or '/home/vm_images/ubuntu-24.04.3-live-server-amd64.iso'}
- Username: {config.vm_username}

RULES:
1. Call tools in the correct order (download ISO → install → shutdown → list PCI → generate XML → attach → start → post-setup)
2. After each tool call, read the result carefully before deciding the next step
3. If a tool fails, try to recover:
   - For PCI errors: retry tool_list_pci_devices with pci_group=2 (or 3, 4...)
   - For ISO errors: the tool will auto-find the correct path
   - For SSH errors: retry the same tool once
4. NEVER change the VM name, memory, CPU count, or disk size
5. When all steps are complete and the VM is running, respond with text saying "PROVISIONING COMPLETE"
6. If you cannot recover from an error after 2 retries, respond with text saying "PROVISIONING FAILED: <reason>"

IMPORTANT: Always call tools — do not just describe what you would do. Take action."""


def _execute_tool_with_overrides(tool_name: str, tool_input: dict, config, state) -> dict:
    """Execute a tool, applying any parameter overrides from Claude.
    
    Claude may suggest different parameters than the original config.
    For example: pci_group=2 instead of 1 when the first group fails.
    
    We apply Claude's overrides to the config before running the tool.
    We NEVER override: vm_name, memory_mb, num_cpu, disk_size, vm_username, vm_password.
    """
    fn = TOOL_REGISTRY.get(tool_name)
    if fn is None:
        return {"status": "failed", "error": f"Tool '{tool_name}' not found in registry", "data": None}

    # Apply safe overrides from Claude's tool_input
    # These are parameters Claude is allowed to change
    SAFE_OVERRIDES = {"pci_group", "aic_cards", "os_image", "acs_state", "disk_path"}

    original_values = {}
    for key, value in tool_input.items():
        if key in SAFE_OVERRIDES and hasattr(config, key):
            original_values[key] = getattr(config, key)
            setattr(config, key, value)

    try:
        result = fn(config, state)
    except Exception as e:
        result = {"status": "failed", "error": str(e), "data": None}
    finally:
        # Restore original values (don't permanently change config)
        for key, value in original_values.items():
            setattr(config, key, value)

    return result


def run_agentic_loop(config, state: AgentState, stream_callback=None, should_cancel=None) -> AgentState:
    """Run the Phase 2 looping agentic loop.
    
    HOW IT WORKS:
    1. Build a system prompt explaining Claude's goal
    2. Start conversation with the VM config as context
    3. Loop:
       a. Ask Claude what to do next (with tool schemas)
       b. Claude returns either a tool_use block or text
       c. If tool_use: execute the tool, feed result back to Claude
       d. If text with "PROVISIONING COMPLETE": we're done!
       e. If text with "PROVISIONING FAILED": stop with error
       f. Repeat until done or max iterations reached
    
    Args:
        config:          VMConfig with all VM parameters
        state:           AgentState for sharing data between tools
        stream_callback: Optional function(event_type, data) for SSE streaming
        should_cancel:   Optional callable returning True when the user has
                         requested cancellation (checked between iterations)
    
    Returns:
        The final AgentState
    """
    from llm_client import ask_with_tools
    from tool_schemas import get_schemas_for_plan

    # Determine which tools are relevant for this VM type
    plan = build_plan(config)
    tool_schemas = get_schemas_for_plan(plan)
    system_prompt = _build_system_prompt(config)

    # Conversation history — Claude reads this to understand what's happened
    messages = [
        {
            "role": "user",
            "content": (
                f"Please provision the VM '{config.vm_name}' according to the configuration "
                f"in your system prompt. Start by checking/downloading the ISO."
            )
        }
    ]

    state.set_plan(plan)
    session_start = _log_agent_session_start(config.vm_name)
    print(f"\n{'='*60}", flush=True)
    print(f"[AGENT] Starting agentic loop for VM '{config.vm_name}'", flush=True)
    print(f"[AGENT] Max iterations: {MAX_AGENT_ITERATIONS}", flush=True)
    print(f"{'='*60}", flush=True)

    if stream_callback:
        stream_callback("agent_start", {"vm_name": config.vm_name, "plan": plan})

    completed_iterations = 0
    for iteration in range(1, MAX_AGENT_ITERATIONS + 1):
        print(f"\n[AGENT] Iteration {iteration}/{MAX_AGENT_ITERATIONS}", flush=True)

        # ── User cancelled? Stop cleanly between iterations ───
        if should_cancel and should_cancel():
            print("[AGENT] ⏹ Cancelled by user", flush=True)
            state.mark_failed("agent_loop", "Cancelled by user")
            if stream_callback:
                stream_callback("agent_cancelled", {"reason": "Cancelled by user"})
            return state

        # ── Ask Claude what to do next ────────────────────────
        try:
            response = ask_with_tools(
                messages=messages,
                tools=tool_schemas,
                system_prompt=system_prompt,
                max_tokens=1024,
            )
        except Exception as e:
            print(f"[AGENT] Claude API error: {e}", flush=True)
            if stream_callback:
                stream_callback("agent_error", {"error": str(e)})
            state.mark_failed("agent_loop", f"Claude API error: {e}")
            return state

        # ── Process Claude's response ─────────────────────────
        tool_calls = []
        text_responses = []

        for block in response.content:
            if block.type == "tool_use":
                tool_calls.append(block)
            elif block.type == "text":
                text_responses.append(block.text)

        # Check if Claude wrote a final text response (done or failed)
        for text in text_responses:
            print(f"[AGENT] Claude says: {text[:200]}", flush=True)
            if "PROVISIONING COMPLETE" in text.upper():
                print(f"[AGENT] ✅ Provisioning complete!", flush=True)
                _log_agent_session_end(
                    config.vm_name, session_start, "complete",
                    vm_ip=state.get_output("vm_ip", ""),
                    iterations=completed_iterations,
                )
                if stream_callback:
                    stream_callback("agent_complete", {
                        "vm_name": config.vm_name,
                        "vm_ip": state.get_output("vm_ip", "unknown"),
                        "vm_mac": state.get_output("vm_mac", ""),
                        "message": text,
                    })
                return state
            elif "PROVISIONING FAILED" in text.upper():
                reason = text.replace("PROVISIONING FAILED:", "").strip()
                print(f"[AGENT] ❌ Provisioning failed: {reason}", flush=True)
                _log_agent_session_end(
                    config.vm_name, session_start, "failed",
                    iterations=completed_iterations,
                )
                state.mark_failed("agent_loop", reason)
                if stream_callback:
                    stream_callback("agent_failed", {"reason": reason})
                return state

        # If no tool calls and no completion signal, Claude is confused
        if not tool_calls:
            if response.stop_reason == "end_turn":
                # Claude finished without calling tools — check if it's done
                combined_text = " ".join(text_responses)
                print(f"[AGENT] Claude stopped without tool calls. Text: {combined_text[:200]}", flush=True)
                # If we've run at least some tools, consider it done
                if state.get_output("vm_ip"):
                    print(f"[AGENT] VM IP found — treating as complete", flush=True)
                    if stream_callback:
                        stream_callback("agent_complete", {
                            "vm_name": config.vm_name,
                            "vm_ip": state.get_output("vm_ip", "unknown"),
                            "vm_mac": state.get_output("vm_mac", ""),
                        })
                    return state
            # Add Claude's response to history and continue
            messages.append({"role": "assistant", "content": response.content})
            messages.append({
                "role": "user",
                "content": "Please continue with the next step. Call the appropriate tool."
            })
            continue

        # ── Execute each tool Claude requested ────────────────
        # Add Claude's response (with tool calls) to history
        messages.append({"role": "assistant", "content": response.content})

        # Build tool results to send back to Claude
        tool_results = []

        for tool_call in tool_calls:
            tool_name = tool_call.name
            tool_input = tool_call.input or {}

            print(f"[AGENT] Claude calls: {tool_name}({json.dumps(tool_input)[:100]})", flush=True)

            if stream_callback:
                stream_callback("agent_tool_start", {
                    "tool": tool_name,
                    "input": tool_input,
                    "iteration": iteration,
                })

            # Execute the tool
            state.mark_running(tool_name)
            tool_start = time.time()
            result = _execute_tool_with_overrides(tool_name, tool_input, config, state)
            tool_duration = time.time() - tool_start
            completed_iterations = iteration

            # Log the decision (both raw JSON and formatted table row)
            _log_agent_decision(
                iteration=iteration,
                tool_name=tool_name,
                tool_input=tool_input,
                result_status=result.get("status", "unknown"),
                duration_s=tool_duration,
            )
            _log_agent_step(
                iteration=iteration,
                tool_name=tool_name,
                tool_input=tool_input,
                result_status=result.get("status", "unknown"),
                duration_s=tool_duration,
            )

            if result["status"] == "success":
                state.mark_done(tool_name)
                state.record_result(tool_name, result)
                print(f"[AGENT] ✅ {tool_name} succeeded", flush=True)

                # Build success result for Claude
                result_text = f"SUCCESS. "
                data = result.get("data") or {}
                if data:
                    # Include key data points so Claude knows what happened
                    key_data = {k: v for k, v in data.items()
                                if k in ("iso_path", "vm_ip", "vm_mac", "pci_devices",
                                         "iommu_group", "install_complete", "netplan_configured")}
                    if key_data:
                        result_text += f"Data: {json.dumps(key_data)}"

                if stream_callback:
                    stream_callback("agent_tool_done", {
                        "tool": tool_name,
                        "status": "success",
                        "iteration": iteration,
                    })
            else:
                error = result.get("error", "unknown error")
                print(f"[AGENT] ⚠ {tool_name} failed: {error}", flush=True)
                result_text = f"FAILED. Error: {error}. Please decide how to recover."

                if stream_callback:
                    stream_callback("agent_tool_failed", {
                        "tool": tool_name,
                        "error": error,
                        "iteration": iteration,
                    })

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tool_call.id,
                "content": result_text,
            })

        # Add all tool results to conversation history
        messages.append({"role": "user", "content": tool_results})

    # Max iterations reached
    print(f"[AGENT] ⚠ Max iterations ({MAX_AGENT_ITERATIONS}) reached", flush=True)
    _log_agent_session_end(
        config.vm_name, session_start, "max_iterations_reached",
        iterations=completed_iterations,
    )
    state.mark_failed("agent_loop", f"Max iterations ({MAX_AGENT_ITERATIONS}) reached without completion")
    if stream_callback:
        stream_callback("agent_failed", {"reason": f"Max iterations reached"})
    return state


# ─── Legacy CLI runner (not used by web.py) ──────────────────

def run(config, state: AgentState = None, sleep_fn=time.sleep, narrate=True) -> AgentState:
    """Execute the full Classic Mode plan. Returns the final AgentState."""
    plan = build_plan(config)
    state = state or AgentState()
    state.set_plan(plan)

    print(f"\n🔧 Executing {len(plan)} tools...\n")

    for idx, tool_name in enumerate(plan, 1):
        fn = TOOL_REGISTRY.get(tool_name)
        if fn is None:
            state.mark_failed(tool_name, "tool not found in registry")
            print(f"❌ {tool_name}: not found")
            return state

        state.mark_running(tool_name)
        result = None

        for attempt in range(1, MAX_RETRIES + 1):
            print(f"▶ [{idx}/{len(plan)}] {tool_name} (attempt {attempt}/{MAX_RETRIES})")
            result = fn(config, state)

            if result["status"] == "success":
                state.mark_done(tool_name)
                state.record_result(tool_name, result)
                print(f"✅ {tool_name}")

                if narrate:
                    commentary = _narrate_step(
                        tool_name, result,
                        f"{idx}/{len(plan)} done", config
                    )
                    if commentary:
                        print(f"   🤖 {commentary}")
                print()
                break

            print(f"⚠ {tool_name} failed: {result['error']}")
            if attempt < MAX_RETRIES:
                sleep_fn(RETRY_SLEEP_SECONDS)
            else:
                state.mark_failed(tool_name, result["error"])
                print(f"❌ {tool_name}: giving up after {MAX_RETRIES} attempts\n")
                return state

    print("🎉 All tools completed successfully.")
    return state


def final_report(config, state: AgentState) -> str:
    lines = [
        "",
        "═" * 50,
        "✅ VM PROVISIONING COMPLETE",
        "═" * 50,
        f"  VM Name  : {config.vm_name}",
        f"  IP Addr  : {state.get_output('vm_ip', 'unknown')}",
        f"  MAC Addr : {state.get_output('vm_mac', 'unknown')}",
        f"  Username : {config.vm_username}",
        f"  Type     : {config.vm_type}",
        f"  AIC Cards: {config.aic_cards}",
        "═" * 50,
    ]
    return "\n".join(lines)