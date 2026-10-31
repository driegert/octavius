"""A scripted stand-in for `pi --mode rpc`, for tests/test_pi_brain.py.

Reads JSONL commands on stdin and answers like pi 1.4.0's RPC mode. The prompt's
last line picks the script: TOOL, CONFIRM, LOOP, SLOW, SLOWTOOL, THINK, DIE, ERROR, SPEAKSTALL, SPEAKERR, LATE, or anything
else for a plain two-sentence answer. Every command received is appended to
$FAKE_PI_LOG (one JSON line each) so tests can see aborts, steers and prompts.

Failover scripting. The process starts on $PI_OCTAVIUS_MODEL and follows `set_model`.
$FAKE_PI_FAIL_FILE (re-read on every prompt) is a JSON object mapping a model id to how a
prompt on that model fails: "error:<text>" (provider error, as pi reports after a 5xx or
refused connection), "hang" (accepts the prompt and never generates), "stall" (one thinking
delta, then nothing), "tool_then_error:<text>" (the TOOL script's first request calls a
tool, the next request errors); a "_unknown" list makes `set_model` fail for those models.
A prompt that opens "[System note: the previous attempt" is a failover resume: it answers
(text names the model) unless the current model is scripted to fail again.

"/warm" plays pi_harness's octavius-warm extension: the prompt is "handled" and a
setStatus report follows (result from $FAKE_PI_WARM_RESULT, default "ok"). With
FAKE_PI_WARM=unsupported it is an ordinary prompt, as in a pi without the extension.
"""

import json
import os
import queue
import sys
import threading
import time

commands: "queue.Queue[dict]" = queue.Queue()
log_path = os.environ.get("FAKE_PI_LOG")
model = os.environ.get("PI_OCTAVIUS_MODEL", "fake/model")
last_script = ""
RESUME_PREFIX = "[System note: the previous attempt"


def fail_config() -> dict:
    path = os.environ.get("FAKE_PI_FAIL_FILE")
    try:
        return json.load(open(path)) if path else {}
    except (OSError, json.JSONDecodeError):
        return {}


def note(record: dict) -> None:
    if log_path:
        with open(log_path, "a") as f:
            f.write(json.dumps(record) + "\n")


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


def provider_error(text: str) -> None:
    assistant("", stop="error", errorMessage=text)
    emit({"type": "turn_end"})
    emit({"type": "agent_end", "messages": [], "willRetry": False})
    emit({"type": "agent_settled", "aborted": False})


def tool_call() -> None:
    assistant("Let me check that.", stop="toolUse")
    emit({"type": "tool_execution_start", "toolCallId": "c1", "toolName": "mcp__email__hybrid_search", "args": {"query": "x", "folder": "Inbox"}})
    emit({"type": "tool_execution_end", "toolCallId": "c1", "toolName": "mcp__email__hybrid_search",
          "result": {"content": [{"type": "text", "text": "3 results"}]}, "isError": False, "durationMs": 12})
    emit({"type": "turn_end"})


def hang(think: bool) -> None:
    if think:
        emit({"type": "message_start", "message": {"role": "assistant", "content": [], "stopReason": "pending"}})
        emit({"type": "message_update", "assistantMessageEvent": {"type": "thinking_delta", "contentIndex": 0, "delta": "hmm "}})
    while True:
        if aborted(poll(0.05)):
            return


def tool_hang() -> None:
    """A tool that never returns. On abort it still finishes and leaves a dialog open."""
    emit({"type": "tool_execution_start", "toolCallId": "c9", "toolName": "mcp__email__hybrid_search", "args": {"query": "slow"}})
    while True:
        cmd = poll(0.05)
        if cmd is None:
            continue
        if cmd["type"] == "abort":
            emit({"type": "extension_ui_request", "id": "zz", "method": "confirm", "title": "Allow x?", "message": "{}"})
            emit({"type": "tool_execution_end", "toolCallId": "c9", "toolName": "mcp__email__hybrid_search",
                  "result": {"content": [{"type": "text", "text": "partial"}]}, "isError": True, "durationMs": 1})
            emit({"type": "turn_end"})
            emit({"type": "agent_settled", "aborted": True})
            respond(cmd)
            return
        respond(cmd, data={"disposition": "queued"})


def run(prompt: str) -> None:
    global last_script
    resumed = prompt.startswith(RESUME_PREFIX)
    if resumed:
        script = "RESUME_TOOLS" if "after your tool calls" in prompt else "RESUME"
    else:
        script = last_script = prompt.strip().splitlines()[-1].strip()
    emit({"type": "agent_start"})
    emit({"type": "message_end", "message": {"role": "user", "content": prompt}})
    note({"type": "_run", "model": model, "script": script})
    if script == "DIE":
        sys.exit(3)
    fail = fail_config().get(model, "")
    if script == "ERROR" or (resumed and last_script == "ERROR"):
        provider_error("502 Bad Gateway")
        return
    if fail.startswith("error:"):
        provider_error(fail[6:])
        return
    if fail == "empty_length":  # max_tokens spent on reasoning: stopReason length, no content
        assistant("", stop="length")
        emit({"type": "turn_end"})
        emit({"type": "agent_end", "messages": [], "willRetry": False})
        emit({"type": "agent_settled", "aborted": False})
        return
    if fail == "toolhang" and script == "TOOL":
        tool_hang()
        return
    if fail.startswith("toolstart_then_error:") and script == "TOOL":
        emit({"type": "tool_execution_start", "toolCallId": "c1", "toolName": "mcp__email__hybrid_search", "args": {}})
        provider_error(fail[21:])
        return
    if script == "SPEAKERR" and not resumed:
        assistant("Said it.")
        emit({"type": "turn_end"})
        provider_error("500: late failure")
        return
    if fail in ("hang", "stall"):
        hang(think=fail == "stall")
        return
    if fail.startswith("tool_then_error:") and script == "TOOL":
        tool_call()
        provider_error(fail[16:])
        return
    if script == "SPEAKSTALL" and not resumed:
        assistant("First part.")  # spoken, then the connection dies mid-turn
        hang(think=False)
        return
    if script in ("RESUME", "RESUME_TOOLS"):
        assistant(f"Resumed on {model}. All good." + (" Tools were kept." if script == "RESUME_TOOLS" else ""))
        emit({"type": "turn_end"})
    elif script == "TOOL":
        tool_call()
        assistant("Found three. The first is from Alice.")
        emit({"type": "turn_end"})
    elif script == "SLOWTOOL":
        # A tool that runs longer than any model silence is a tool, not a zombie hop.
        assistant("Checking.", stop="toolUse")
        emit({"type": "tool_execution_start", "toolCallId": "c1", "toolName": "subagent", "args": {}})
        time.sleep(1.0)
        emit({"type": "tool_execution_end", "toolCallId": "c1", "toolName": "subagent",
              "result": {"content": [{"type": "text", "text": "done"}]}, "isError": False, "durationMs": 1000})
        emit({"type": "turn_end"})
        assistant("The slow tool finished.")
        emit({"type": "turn_end"})
    elif script == "LATE":
        time.sleep(1.0)  # a slow first output (a warm to wait out, a cold prefill)
        assistant("Late but fine.")
        emit({"type": "turn_end"})
    elif script == "THINK":
        # Reasoning deltas count as progress: a long think is not silence.
        emit({"type": "message_start", "message": {"role": "assistant", "content": [], "stopReason": "pending"}})
        for _ in range(6):
            emit({"type": "message_update", "assistantMessageEvent": {"type": "thinking_delta", "contentIndex": 0, "delta": "hm "}})
            time.sleep(0.2)
        assistant("Done thinking.")
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
    global model
    threading.Thread(target=reader, daemon=True).start()
    while True:
        cmd = commands.get()
        if cmd["type"] == "_eof":
            return
        if cmd["type"] == "prompt" and cmd.get("message") == "/warm" and os.environ.get("FAKE_PI_WARM") != "unsupported":
            respond(cmd, data={"disposition": "handled"})
            result = os.environ.get("FAKE_PI_WARM_RESULT", "ok")
            report = {"event": "warm", "result": result, "source": "template", "prompt_n": 24, "cache_n": 10151,
                      "prefill_ms": 180, "wall_ms": 210}
            emit({"type": "extension_ui_request", "id": "w", "method": "setStatus",
                  "statusKey": "octavius-warm", "statusText": json.dumps(report)})
        elif cmd["type"] == "set_model":
            target = f"{cmd.get('provider')}/{cmd.get('modelId')}"
            if target in fail_config().get("_unknown", []):
                emit({"id": cmd.get("id"), "type": "response", "command": "set_model", "success": False,
                      "error": f"Model not found: {target}"})
            else:
                model = target
                respond(cmd, data={"provider": cmd.get("provider"), "id": cmd.get("modelId")})
        elif cmd["type"] == "prompt":
            # "_ack_delay": {model: seconds} holds back the prompt's acknowledgement.
            time.sleep(fail_config().get("_ack_delay", {}).get(model, 0))
            respond(cmd, data={"disposition": "started"})
            run(cmd.get("message", ""))
        else:
            respond(cmd)


if __name__ == "__main__":
    main()
