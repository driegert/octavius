"""A scripted stand-in for `pi --mode rpc`, for tests/test_pi_brain.py.

Reads JSONL commands on stdin and answers like pi 1.4.0's RPC mode. The prompt's
last line picks the script: TOOL, CONFIRM, LOOP, SLOW, DIE, ERROR, or anything
else for a plain two-sentence answer. Every command received is appended to
$FAKE_PI_LOG (one JSON line each) so tests can see aborts, steers and prompts.
"""

import json
import os
import queue
import sys
import threading
import time

commands: "queue.Queue[dict]" = queue.Queue()
log_path = os.environ.get("FAKE_PI_LOG")


def emit(record: dict) -> None:
    sys.stdout.write(json.dumps(record) + "\n")
    sys.stdout.flush()


def reader() -> None:
    for line in sys.stdin:
        try:
            cmd = json.loads(line)
        except json.JSONDecodeError:
            continue
        if log_path:
            with open(log_path, "a") as f:
                f.write(json.dumps(cmd) + "\n")
        commands.put(cmd)
    commands.put({"type": "_eof"})


def respond(cmd: dict, **fields) -> None:
    emit({"id": cmd.get("id"), "type": "response", "command": cmd["type"], "success": True, **fields})


def assistant(text: str, stop: str = "stop", **extra) -> None:
    emit({"type": "message_start", "message": {"role": "assistant", "content": [], "stopReason": "pending"}})
    for word in text.split(" "):
        emit({"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "contentIndex": 0, "delta": word + " "}})
    emit({"type": "message_end", "message": {"role": "assistant", "content": [{"type": "text", "text": text}], "stopReason": stop, **extra}})


def poll(timeout: float = 0.0):
    try:
        return commands.get(timeout=timeout) if timeout else commands.get_nowait()
    except queue.Empty:
        return None


def aborted(cmd: dict | None) -> bool:
    """Handle a command arriving mid-run; True if it was an abort."""
    if cmd is None:
        return False
    if cmd["type"] == "abort":
        emit({"type": "agent_settled", "aborted": True})
        respond(cmd)
        return True
    respond(cmd, data={"disposition": "queued"})
    return False


def run(prompt: str) -> None:
    script = prompt.strip().splitlines()[-1].strip()
    emit({"type": "agent_start"})
    emit({"type": "message_end", "message": {"role": "user", "content": prompt}})
    if script == "DIE":
        sys.exit(3)
    if script == "ERROR":
        assistant("", stop="error", errorMessage="502 Bad Gateway")
        emit({"type": "turn_end"})
    elif script == "TOOL":
        assistant("Let me check that.", stop="toolUse")
        emit({"type": "tool_execution_start", "toolCallId": "c1", "toolName": "mcp__email__hybrid_search", "args": {"query": "x", "folder": "Inbox"}})
        emit({"type": "tool_execution_end", "toolCallId": "c1", "toolName": "mcp__email__hybrid_search",
              "result": {"content": [{"type": "text", "text": "3 results"}]}, "isError": False, "durationMs": 12})
        emit({"type": "turn_end"})
        assistant("Found three. The first is from Alice.")
        emit({"type": "turn_end"})
    elif script == "CONFIRM":
        emit({"type": "extension_ui_request", "id": "u1", "method": "confirm", "title": "Allow create_task?",
              "message": json.dumps({"title": "Buy milk", "project_id": 1}), "timeout": 60000})
        while True:
            cmd = poll(10)
            if cmd is None or cmd["type"] == "_eof":
                return
            if cmd["type"] == "extension_ui_response":
                break
        assistant(f"Confirmed is {str(bool(cmd.get('confirmed'))).lower()}.")
        emit({"type": "turn_end"})
    elif script == "LOOP":
        while True:
            assistant("Searching again.", stop="toolUse")
            emit({"type": "turn_end"})
            time.sleep(0.01)
            if aborted(poll()):
                return
    elif script == "SLOW":
        emit({"type": "message_start", "message": {"role": "assistant", "content": [], "stopReason": "pending"}})
        while True:
            emit({"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "contentIndex": 0, "delta": "la "}})
            if aborted(poll(0.05)):
                return
    else:
        assistant("Hello there. How are you?")
        emit({"type": "turn_end"})
    emit({"type": "agent_end", "messages": [], "willRetry": False})
    emit({"type": "agent_settled", "aborted": False})


def main() -> None:
    threading.Thread(target=reader, daemon=True).start()
    while True:
        cmd = commands.get()
        if cmd["type"] == "_eof":
            return
        if cmd["type"] == "prompt":
            respond(cmd, data={"disposition": "started"})
            run(cmd.get("message", ""))
        else:
            respond(cmd)


if __name__ == "__main__":
    main()
