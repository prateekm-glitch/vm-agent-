# installer_autopilot.py
# ─────────────────────────────────────────────────────────────
# Pattern-matching Ubuntu installer autopilot:
#   - Waits for specific text on screen before acting
#   - Hardcoded actions for known screens (deterministic)
#   - Claude fallback when expected text doesn't appear
#   - Logs every step to logs/autopilot_run.txt
# ─────────────────────────────────────────────────────────────

import time
import json
import re
import os
import atexit
from datetime import datetime


# ─── Configuration ─────────────────────────────────────────────
SCREEN_WAIT_TIMEOUT = 60     # Max seconds to wait for expected text
SCREEN_READ_INTERVAL = 3     # Seconds between screen reads while waiting
POST_ACTION_WAIT = 3         # Seconds to wait after action before next step
INSTALL_TIMEOUT_MINUTES = 60 # Max time waiting for install to finish
REFRESH_WAIT = 1.5           # Time after Ctrl+L before reading


# ─── Logging Setup ─────────────────────────────────────────────
LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
_log_file = None
_log_file_name = "autopilot_run.txt"  # default, can be overridden per-VM
_log_stream_queue = None  # optional queue.Queue for real-time streaming


def _set_log_file(filename):
    """Set the log filename (called before _init_log to enable per-VM logs).
    
    Args:
        filename: log file name (relative to logs/ dir)
    """
    global _log_file_name, _log_file
    # Close previous log if open
    if _log_file:
        try:
            _log_file.write(f"\n=== Log switched (new VM) at {datetime.now().isoformat()} ===\n")
            _log_file.close()
        except Exception:
            pass
        _log_file = None
    _log_file_name = filename


def _set_log_stream_queue(q):
    """Set an optional queue to receive log lines in real-time (for SSE streaming).
    
    Args:
        q: queue.Queue instance, or None to disable
    """
    global _log_stream_queue
    _log_stream_queue = q


def _init_log():
    """Initialize the log file (creates logs/ dir if needed, overwrites file)."""
    global _log_file
    if _log_file is not None:
        return
    os.makedirs(LOG_DIR, exist_ok=True)
    log_path = os.path.join(LOG_DIR, _log_file_name)
    _log_file = open(log_path, "w", encoding="utf-8")
    _log_file.write(f"=== Autopilot Run Started: {datetime.now().isoformat()} ===\n")
    _log_file.write(f"=== Log file: {_log_file_name} ===\n\n")
    _log_file.flush()
    atexit.register(_close_log)


def _close_log():
    """Close the log file gracefully."""
    global _log_file
    if _log_file:
        try:
            _log_file.write(f"\n=== Autopilot Run Ended: {datetime.now().isoformat()} ===\n")
            _log_file.flush()
            _log_file.close()
        except Exception:
            pass
        _log_file = None


def log(msg=""):
    """Print to terminal AND write to log file (flush immediately).
    Also pushes to log stream queue if one is set (for real-time SSE streaming).
    """
    global _log_file, _log_stream_queue
    if _log_file is None:
        _init_log()
    print(msg, flush=True)
    try:
        _log_file.write(msg + "\n")
        _log_file.flush()
    except Exception:
        pass
    # Push to real-time stream queue if set (non-blocking)
    if _log_stream_queue is not None:
        try:
            _log_stream_queue.put_nowait(msg)
        except Exception:
            pass


# ─── Key mappings for the PTY ──────────────────────────────────
KEYS = {
    "enter": "\n",
    "tab": "\t",
    "space": " ",
    "up": "\x1b[A",
    "down": "\x1b[B",
    "left": "\x1b[D",
    "right": "\x1b[C",
}


# ─── Installer Steps Definition ───────────────────────────────
# Each step waits for "wait_for" text to appear on screen,
# then executes "action". This ensures we're on the right screen.

def _build_steps(config):
    """Build installer steps with config values."""
    username = config.vm_username or "ubuntu"
    password = config.vm_password or "ubuntu"
    hostname = config.vm_name or "ubuntu-server"

    steps = [
        {"wait_for": "rich mode",        "action": "enter",  "desc": "Continue in rich mode"},
        {"wait_for": "English",          "action": "enter",  "desc": "Select English"},
        {"wait_for": "without updating", "action": "enter",  "desc": "Continue without updating"},
        {"wait_for": "Layout",           "action": "enter",  "desc": "Done - keyboard layout"},
        {"wait_for": "type of install",  "action": "enter",  "desc": "Done - installation type"},
        {"wait_for": "Network",          "action": "enter",  "desc": "Done - network config"},
        {"wait_for": "proxy",            "action": "enter",  "desc": "Done - proxy config"},
        {"wait_for": "mirror",           "action": "wait30_enter", "desc": "Done - mirror (wait 30s for test)"},
        {"wait_for": "storage",          "action": "tab_to_done",  "desc": "Guided storage - Tab to Done, Enter"},
        {"wait_for": "FILE SYSTEM",      "action": "tab_to_done",  "desc": "Done - storage summary"},
        {"wait_for": "Continue",         "action": "tab_enter",  "desc": "Confirm destructive action - Tab to Continue, Enter"},
        {"wait_for": "Your name",        "action": "fill_user", "desc": "Fill username/password",
         "username": username, "password": password, "hostname": hostname},
        {"wait_for": "Ubuntu Pro",       "action": "enter",  "desc": "Skip Ubuntu Pro"},
        {"wait_for": "SSH",              "action": "space_tab_tab_enter", "desc": "Install OpenSSH"},
        {"wait_for": "Featured",         "action": "tab_to_done",  "desc": "Done - snaps"},
        {"wait_for": "Install complete", "action": "wait_install", "desc": "Wait for install"},
        {"wait_for": "Reboot Now",       "action": "reboot_sequence",  "desc": "Reboot (Enter + wait 2min + Enter)"},
        # Step 18 (login:) removed — after reboot, virt-install exits and the channel
        # loses the VM console. Login is handled in tool_run_mq_vm_install.py via
        # a new virsh console channel on the same SSH connection.
    ]
    return steps


# ─── Action Execution ─────────────────────────────────────────

def execute_action(channel, action_str):
    """Execute a basic action string (semicolons for chaining).
    
    Supported: enter, tab, space, up, down, type TEXT, wait N
    """
    actions = [a.strip() for a in action_str.split(";")]

    for action in actions:
        if action.startswith("wait"):
            try:
                secs = int(action.split()[1])
            except (IndexError, ValueError):
                secs = 3
            time.sleep(secs)

        elif action.startswith("type "):
            text = action[5:]
            channel.sendall(text.encode())
            time.sleep(0.5)

        elif action in KEYS:
            channel.sendall(KEYS[action].encode())
            time.sleep(0.5)

        else:
            # Unknown — send as raw text
            channel.sendall(action.encode())
            time.sleep(0.5)


def execute_step_action(channel, step):
    """Execute the action for a step (handles special action types)."""
    action = step["action"]

    if action == "enter":
        execute_action(channel, "enter")

    elif action == "tab_enter":
        execute_action(channel, "tab;enter")

    elif action == "tab_to_done":
        # Smart tab: press tab repeatedly until "Done" appears highlighted,
        # then press Enter. Works regardless of how many form elements exist.
        tab_to_done(channel)

    elif action == "tab4_enter":
        execute_action(channel, "tab;tab;tab;tab;enter")

    elif action == "reboot_sequence":
        # Press Enter on "Reboot Now", then handle any intermediate dialogs
        # (e.g., "Close" button, unmount CD prompt) by pressing Enter repeatedly
        log(f"      ▶ Pressing Enter on 'Reboot Now'...")
        execute_action(channel, "enter")
        time.sleep(5)
        # Press Enter again to dismiss any intermediate dialog (Close button, etc.)
        log(f"      ▶ Pressing Enter again (dismiss intermediate dialog)...")
        execute_action(channel, "enter")
        time.sleep(5)
        # One more Enter in case there's another prompt
        log(f"      ▶ Pressing Enter once more (ensure reboot starts)...")
        execute_action(channel, "enter")
        log(f"      ⏳ Waiting 90s for VM to unmount + reboot...")
        time.sleep(90)
        # After the 90s wait, press Enter to dismiss the CDROM unmount prompt
        # if it appeared during the wait (backup in case passive reader missed it)
        log(f"      ▶ Pressing Enter to dismiss CDROM prompt (backup, if present)...")
        execute_action(channel, "enter")
        time.sleep(5)
        execute_action(channel, "enter")
        log(f"      ✅ Reboot sequence complete!")

    elif action == "wait10_enter":
        log(f"      ⏳ Waiting 10s for mirror test...")
        time.sleep(10)
        execute_action(channel, "enter")

    elif action == "wait30_enter":
        log(f"      ⏳ Waiting 30s for mirror test to complete...")
        time.sleep(30)
        execute_action(channel, "enter")

    elif action == "tab_tab_tab_enter":
        execute_action(channel, "tab;tab;tab;enter")

    elif action == "space_tab_tab_enter":
        execute_action(channel, "space;tab;tab;enter")

    elif action == "fill_user":
        username = step.get("username", "ubuntu")
        password = step.get("password", "ubuntu")
        # Profile form: Your name → Server name → Username → Password → Confirm password → Done
        # Use username for ALL fields except password fields
        # After last field, tab lands on Done, then Enter presses it
        fill_action = (
            f"type {username};tab;"
            f"type {username};tab;"
            f"type {username};tab;"
            f"type {password};tab;"
            f"type {password};tab;"
            "enter"
        )
        log(f"      Filling profile: name={username}, server={username}, user={username}")
        execute_action(channel, fill_action)

    elif action == "login_user":
        username = step.get("username", "ubuntu")
        password = step.get("password", "ubuntu")
        # VM booted and showing login prompt — enter username, then password
        log(f"      ▶ Typing username: {username}")
        execute_action(channel, f"type {username};enter")
        # Wait for password prompt
        log(f"      ⏳ Waiting for Password prompt...")
        time.sleep(5)
        # Read to check for "Password:" prompt
        screen = read_screen(channel, timeout=10)
        log(f"      ▶ Typing password...")
        execute_action(channel, f"type {password};enter")
        # Wait for login to complete
        time.sleep(5)
        log(f"      ✅ Login credentials sent!")

    elif action == "wait_install":
        # This is handled specially in the main loop
        pass

    else:
        # Generic action string
        execute_action(channel, action)


# ─── Screen Reading ───────────────────────────────────────────

def flush_screen(channel, timeout=2):
    """Read and discard all pending data from the channel buffer."""
    discarded = ""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if channel.recv_ready():
            chunk = channel.recv(65536).decode("utf-8", errors="replace")
            discarded += chunk
            deadline = time.time() + 0.5
        else:
            time.sleep(0.1)
    return discarded


def read_screen(channel, timeout=8):
    """Read available output from the PTY channel with timeout."""
    output = ""
    deadline = time.time() + timeout
    got_data = False

    while time.time() < deadline:
        if channel.recv_ready():
            chunk = channel.recv(8192).decode("utf-8", errors="replace")
            output += chunk
            got_data = True
            deadline = time.time() + 2
        else:
            if got_data:
                time.sleep(0.3)
                if not channel.recv_ready():
                    break
            time.sleep(0.2)
    return output


def read_screen_fresh(channel, timeout=8):
    """Flush old data, send Ctrl+L to refresh TUI, then read fresh screen."""
    flush_screen(channel, timeout=1)
    channel.sendall(b"\x0c")
    time.sleep(REFRESH_WAIT)
    return read_screen(channel, timeout=timeout)


def strip_ansi(text):
    """Remove ANSI escape sequences for text matching."""
    ansi_escape = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[()][0-9A-B]')
    return ansi_escape.sub('', text)


# ─── Smart Tab-to-Done ─────────────────────────────────────────

def tab_to_done(channel, max_tabs=15):
    """Press tab repeatedly until 'Done' is the focused/highlighted element, then Enter.
    
    After each tab press, refreshes the screen and checks if 'Done' appears
    to be highlighted (indicated by reverse video or specific ANSI patterns
    around the word 'Done'). Once found, presses Enter.
    
    Falls back to pressing Enter after max_tabs if Done is never detected as focused.
    """
    log(f"      🔄 Tab-to-Done: pressing tab until Done is focused (max {max_tabs})...")
    
    for i in range(1, max_tabs + 1):
        # Press tab
        channel.sendall(b"\t")
        time.sleep(0.8)
        
        # Read the screen response (partial — just the cursor movement/redraw)
        # After tab, the TUI typically sends a redraw showing the new focus
        response = ""
        deadline = time.time() + 1.5
        while time.time() < deadline:
            if channel.recv_ready():
                chunk = channel.recv(4096).decode("utf-8", errors="replace")
                response += chunk
                deadline = time.time() + 0.5
            else:
                time.sleep(0.1)
        
        # Check if "Done" appears to be highlighted in the response
        # In TUI, a focused button typically has reverse video (ESC[7m) or
        # specific color codes right before "Done"
        # Common patterns: highlighted "Done" has color codes wrapping it
        if "Done" in response:
            # Check for highlight indicators near "Done"
            # Look for reverse video, bold, or specific color patterns before Done
            done_idx = response.find("Done")
            # Check ~30 chars before "Done" for highlight indicators
            prefix = response[max(0, done_idx - 30):done_idx]
            
            # Common highlight patterns in urwid/subiquity TUI:
            # - Reverse video: \x1b[7m
            # - Green/highlight background: \x1b[42m or \x1b[48;2;...
            # - Bold: \x1b[1m
            is_highlighted = (
                "\x1b[7m" in prefix or      # Reverse video
                "48;2;" in prefix or         # RGB background color
                "\x1b[42m" in prefix or      # Green background
                "\x1b[1m" in prefix or       # Bold (sometimes used for focus)
                "\x1b[30;42m" in prefix or   # Black on green
                "\x1b[0;30;42m" in prefix    # Reset + black on green
            )
            
            if is_highlighted:
                log(f"      🔄 Tab #{i}: Done is FOCUSED! Pressing Enter.")
                channel.sendall(b"\n")
                time.sleep(0.5)
                return
            else:
                log(f"      🔄 Tab #{i}: 'Done' visible but not yet focused...")
        else:
            # No "Done" in response, keep tabbing
            if i <= 3 or i % 3 == 0:
                log(f"      🔄 Tab #{i}: still navigating...")
    
    # Fallback: max tabs reached, just press Enter (Done might be focused but undetected)
    log(f"      🔄 Max tabs ({max_tabs}) reached, pressing Enter anyway...")
    channel.sendall(b"\n")
    time.sleep(0.5)


# ─── Wait for Screen Pattern ──────────────────────────────────

def wait_for_text(channel, wait_for, timeout=SCREEN_WAIT_TIMEOUT):
    """Wait until the specified text appears on screen.
    
    Repeatedly reads the screen (with Ctrl+L refresh) until the
    wait_for text is found or timeout is reached.
    
    Args:
        channel: SSH PTY channel
        wait_for: text to look for (case-insensitive)
        timeout: max seconds to wait
        
    Returns:
        (found: bool, screen_text: str)
    """
    deadline = time.time() + timeout
    attempt = 0

    while time.time() < deadline:
        attempt += 1
        screen = read_screen_fresh(channel, timeout=5)
        cleaned = strip_ansi(screen)

        if wait_for.lower() in cleaned.lower():
            return True, screen

        # Brief wait before trying again
        remaining = deadline - time.time()
        if remaining > SCREEN_READ_INTERVAL:
            time.sleep(SCREEN_READ_INTERVAL)
        elif remaining > 0:
            time.sleep(remaining)

    return False, screen if 'screen' in dir() else ""


# Prompts that appear during the post-reboot shutdown/boot sequence that need
# an Enter press to dismiss before the boot can continue.
PASSIVE_INTERMEDIATE_PROMPTS = [
    "please remove the installation medium",
    "press enter:",
]


def wait_for_text_passive(channel, wait_for, timeout=600):
    """Wait for text by passively reading the channel (no Ctrl+L refresh).
    
    Used after reboot when the console is showing raw boot output,
    not a TUI that responds to Ctrl+L. Just reads whatever comes in
    and checks for the target text. Logs output in real-time.
    
    Also handles intermediate prompts (e.g. CDROM unmount) by pressing Enter
    automatically when detected, so the boot sequence can continue.
    
    Args:
        channel: SSH PTY channel
        wait_for: text to look for (case-insensitive)
        timeout: max seconds to wait (default 600s)
        
    Returns:
        (found: bool, accumulated_screen_text: str)
    """
    deadline = time.time() + timeout
    accumulated = ""
    last_log_time = time.time()
    handled_prompts = set()

    log(f"      ⏳ Passively waiting up to {timeout}s for \"{wait_for}\"...")

    while time.time() < deadline:
        # Just read whatever is available — send Enter only for intermediate prompts
        if channel.recv_ready():
            chunk = channel.recv(8192).decode("utf-8", errors="replace")
            accumulated += chunk
            
            # Log ALL boot output lines in real-time (exactly what serial console shows)
            cleaned_chunk = strip_ansi(chunk)
            lines = cleaned_chunk.splitlines()
            for line in lines:
                stripped = line.strip()
                if stripped:
                    log(f"      {stripped[:100]}")
            
            # Check for intermediate prompts that need Enter to dismiss
            # (e.g. "Please remove the installation medium, then press ENTER:")
            cleaned_acc = strip_ansi(accumulated).lower()
            for prompt in PASSIVE_INTERMEDIATE_PROMPTS:
                if prompt in cleaned_acc and prompt not in handled_prompts:
                    log(f"      ▶ Detected intermediate prompt, pressing Enter to dismiss...")
                    channel.sendall(b"\n")
                    time.sleep(2)
                    handled_prompts.add(prompt)
            
            # Check if target text appeared
            cleaned = strip_ansi(accumulated)
            if wait_for.lower() in cleaned.lower():
                log(f"      ✅ \"{wait_for}\" detected!")
                return True, accumulated
        else:
            # Periodically log that we're still waiting
            if time.time() - last_log_time > 30:
                elapsed = int(time.time() - (deadline - timeout))
                log(f"      ⏳ Still waiting... ({elapsed}s elapsed)")
                last_log_time = time.time()
            time.sleep(2)

    return False, accumulated


# ─── Claude Fallback ──────────────────────────────────────────

FALLBACK_PROMPT = """You are navigating the Ubuntu 24.04 Server installer over serial console.
The autopilot expected to see "{expected_text}" on screen but it didn't appear.

Current screen text (cleaned):
```
{screen_text}
```

What key(s) should be pressed to proceed? Respond with ONLY a valid JSON object:
{{
  "action": "enter" or "tab;enter" or "down;enter" or "type text;tab;enter" etc,
  "reason": "brief explanation"
}}

Key options: enter, tab, space, up, down, type TEXT, wait SECONDS
Chain with semicolons: "tab;tab;enter"
"""


def ask_claude_fallback(screen_text, expected_text):
    """Ask Claude what to do when expected text isn't found."""
    from llm_client import ask, strip_code_fences

    cleaned = strip_ansi(screen_text)
    prompt = FALLBACK_PROMPT.format(
        expected_text=expected_text,
        screen_text=cleaned[-2500:],
    )

    try:
        raw = ask(prompt, max_tokens=200, temperature=0.0)
        cleaned_resp = strip_code_fences(raw)
        for start_idx in range(len(cleaned_resp)):
            if cleaned_resp[start_idx] == '{':
                try:
                    result = json.loads(cleaned_resp[start_idx:])
                    return result
                except json.JSONDecodeError:
                    continue
        return json.loads(cleaned_resp)
    except Exception as e:
        return {"action": "enter", "reason": f"Fallback error: {e}"}


# ─── Installation Progress Polling ────────────────────────────

def wait_for_install_complete(channel, timeout_minutes=INSTALL_TIMEOUT_MINUTES):
    """Poll until 'Reboot Now' or 'Install complete' appears."""
    deadline = time.time() + (timeout_minutes * 60)
    full_output = ""

    log(f"   [INSTALL] Waiting up to {timeout_minutes} min for installation...")

    while time.time() < deadline:
        # Use read_screen_fresh (Ctrl+L) to force TUI to send current state
        # This makes install progress visible in logs instead of long silences
        screen = read_screen_fresh(channel, timeout=8)
        full_output += screen

        if screen.strip():
            cleaned = strip_ansi(screen)
            lines = [l for l in cleaned.splitlines() if l.strip()]
            if lines:
                log(f"   [INSTALL] {lines[-1][:80]}")

        lower = full_output.lower()
        if "reboot now" in lower:
            log("   [INSTALL] ✅ 'Reboot Now' detected!")
            return True, full_output
        if "install complete" in lower or "installation complete" in lower:
            log("   [INSTALL] ✅ Installation complete!")
            return True, full_output

        time.sleep(10)

    log("   [INSTALL] ⚠ Timeout!")
    return False, full_output


# ─── Main Autopilot Entry Point ───────────────────────────────

def run_autopilot(channel, config, max_steps=40, initial_screen=""):
    """Run the pattern-matching installer autopilot.

    Args:
        channel: SSH PTY channel
        config: VMConfig with username/password
        max_steps: ignored (kept for backward compat)
        initial_screen: text already read from the channel

    Returns (success: bool, full_output: str)
    """
    full_output = initial_screen
    steps = _build_steps(config)

    log("\n   [AUTOPILOT] ═══════════════════════════════════════════════")
    log("   [AUTOPILOT] Starting pattern-matching installer autopilot")
    log("   [AUTOPILOT] Strategy: Wait for text → Execute action")
    log(f"   [AUTOPILOT] Total steps: {len(steps)}")
    log(f"   [AUTOPILOT] Username: {config.vm_username or 'ubuntu'}")
    log("   [AUTOPILOT] ═══════════════════════════════════════════════\n")

    # Flush initial buffer
    log("   [AUTOPILOT] Flushing initial buffer...")
    flush_screen(channel, timeout=3)
    time.sleep(2)

    # ─── Execute each step ─────────────────────────────────────
    for idx, step in enumerate(steps, 1):
        step_label = f"[{idx}/{len(steps)}]"
        wait_for = step["wait_for"]
        desc = step["desc"]

        log(f"   {step_label} {desc}")
        log(f"   {step_label} Waiting for: \"{wait_for}\"...")

        # Special handling for install wait
        if step["action"] == "wait_install":
            log(f"   {step_label} ⏳ Switching to install polling mode...")
            install_ok, install_output = wait_for_install_complete(channel)
            full_output += install_output
            if not install_ok:
                log(f"   {step_label} ⚠ Installation did not complete in time")
                return False, full_output
            log(f"   {step_label} ✅ Installation complete!")
            # Wait extra time for installer to finish cleanup (grub, unmount, etc.)
            log(f"   {step_label} ⏳ Waiting 60s for installer to finish cleanup...")
            time.sleep(60)
            continue

        # Wait for the expected text to appear
        # Use longer timeout and passive reading for login step (VM boot — no TUI)
        step_timeout = 600 if step.get("action") == "login_user" else SCREEN_WAIT_TIMEOUT
        if step.get("action") == "login_user":
            # After reboot, don't send Ctrl+L — just passively read boot output
            found, screen = wait_for_text_passive(channel, wait_for, timeout=step_timeout)
        else:
            found, screen = wait_for_text(channel, wait_for, timeout=step_timeout)
        full_output += screen

        if found:
            # Show what we found — clean readable output
            cleaned = strip_ansi(screen)
            visible = [l for l in cleaned.splitlines() if l.strip()]
            log(f"   {step_label} ✅ Found \"{wait_for}\"!")
            if visible:
                for l in visible[-3:]:
                    log(f"   {step_label}   │ {l[:75]}")

            # Execute the step action
            log(f"   {step_label} ▶ Executing: {step['action']}")
            execute_step_action(channel, step)
            time.sleep(POST_ACTION_WAIT)

        else:
            # Text not found — use Claude fallback
            log(f"   {step_label} ⚠ \"{wait_for}\" NOT found after {SCREEN_WAIT_TIMEOUT}s!")
            log(f"   {step_label} 🤖 Asking Claude for help...")

            fallback = ask_claude_fallback(screen, wait_for)
            fb_action = fallback.get("action", "enter")
            fb_reason = fallback.get("reason", "no reason")

            log(f"   {step_label} 🤖 Claude says: {fb_action} ({fb_reason})")
            execute_action(channel, fb_action)
            time.sleep(POST_ACTION_WAIT)

            # Try one more time to find the expected text
            found2, screen2 = wait_for_text(channel, wait_for, timeout=30)
            full_output += screen2

            if found2:
                log(f"   {step_label} ✅ Found after fallback!")
                log(f"   {step_label} ▶ Executing: {step['action']}")
                execute_step_action(channel, step)
                time.sleep(POST_ACTION_WAIT)
            else:
                log(f"   {step_label} ⚠ Still not found, executing action anyway...")
                execute_step_action(channel, step)
                time.sleep(POST_ACTION_WAIT)

        log()  # Blank line between steps

    # ─── All steps done ────────────────────────────────────────
    log("\n   [AUTOPILOT] ═══════════════════════════════════════════════")
    log("   [AUTOPILOT] ✅ All steps completed!")
    log("   [AUTOPILOT] ═══════════════════════════════════════════════")

    # Read any final output
    final = read_screen(channel, timeout=10)
    full_output += final

    return True, full_output