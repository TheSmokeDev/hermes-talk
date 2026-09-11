"""Scripted app-server wire peer. Executes no models, tools, commands, or file changes."""

import json
import os
import sys
import time
from pathlib import Path

if "--version" in sys.argv:
    print("codex-cli " + os.environ.get("FAKE_CODEX_VERSION", "0.154.0"))
    raise SystemExit

path = Path(sys.argv[1])
scenario = sys.argv[2]
state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"requests": []}
state["processes"] = state.get("processes", 0) + 1


def save():
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state), encoding="utf-8")
    for attempt in range(30):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            if attempt == 29:
                raise
            time.sleep(0.005)


def send(message):
    print(json.dumps(message), flush=True)


def result(request, data):
    send({"id": request["id"], "result": data})


def notify(method, params):
    send({"method": method, "params": params})


def policy():
    options = state["options"]
    return {
        "thread": state["thread"],
        "model": options["model"],
        "cwd": options["cwd"],
        "approvalPolicy": options["approvalPolicy"],
        "approvalsReviewer": options["approvalsReviewer"],
        "sandbox": {
            "type": "dangerFullAccess"
            if scenario == "bad_policy"
            else {"read-only": "readOnly", "workspace-write": "workspaceWrite"}[options["sandbox"]],
            "networkAccess": False,
        },
    }


def finish(status="completed", emit=True):
    turn = state["thread"]["turns"][0]
    turn["items"] += [
        {"type": "agentMessage", "id": "part-one", "text": "First complete section\n" * 800},
        {"type": "agentMessage", "id": "part-two", "text": "Second section [artifact](result.md)"},
        {
            "type": "fileChange",
            "id": "artifact",
            "status": "completed",
            "changes": [{"path": "result.md", "kind": {"type": "add"}, "diff": "+full artifact"}],
        },
    ]
    turn["status"] = status
    save()
    if emit:
        notify("turn/completed", {"threadId": "thread-owned", "turn": turn})


save()
for line in sys.stdin:
    message = json.loads(line)
    state["requests"].append(message)
    save()
    method = message.get("method")
    params = message.get("params", {})
    if method == "initialize":
        result(message, {"userAgent": "scripted-codex-0.154.0"})
    elif method == "initialized":
        pass
    elif method == "thread/start":
        state["options"] = params
        state["thread"] = {"id": "thread-owned", "cwd": params["cwd"], "turns": []}
        save()
        if scenario == "drop_thread":
            os._exit(3)
        result(message, policy())
        notify("thread/started", {"thread": state["thread"]})
        if scenario == "blocked_writer":
            time.sleep(30)
    elif method == "thread/read":
        assert params["threadId"] == state["thread"]["id"]
        result(message, {"thread": state["thread"]})
    elif method == "thread/resume":
        assert params["threadId"] == state["thread"]["id"]
        result(message, policy())
    elif method == "turn/start":
        assert not state["thread"]["turns"]
        turn = {
            "id": "turn-owned",
            "status": "inProgress",
            "items": [
                {
                    "type": "userMessage",
                    "id": "user-original",
                    "clientId": params["clientUserMessageId"],
                    "content": params["input"],
                }
            ],
        }
        state["thread"]["turns"] = [turn]
        save()
        if scenario == "drop_turn":
            finish(emit=False)
            os._exit(3)
        result(message, {"turn": turn})
        notify("turn/started", {"threadId": "thread-owned", "turn": turn})
        if scenario == "complete":
            finish()
        if scenario == "foreign":
            notify("turn/completed", {"threadId": "foreign-thread", "turn": turn})
        if scenario in {"approval", "approval_replay"}:
            send(
                {
                    "id": 901,
                    "method": "item/commandExecution/requestApproval",
                    "params": {
                        "threadId": "thread-owned",
                        "turnId": "turn-owned",
                        "itemId": "command-one",
                        "command": "fixture command",
                        "startedAtMs": 1000,
                    },
                }
            )
    elif method == "turn/steer":
        assert params["threadId"] == "thread-owned" and params["expectedTurnId"] == "turn-owned"
        state["thread"]["turns"][0]["items"].append(
            {
                "type": "userMessage",
                "id": "steer-input",
                "clientId": params["clientUserMessageId"],
                "content": params["input"],
            }
        )
        save()
        if scenario == "drop_steer":
            os._exit(3)
        result(message, {"turnId": "turn-owned"})
    elif method == "turn/interrupt":
        assert params["threadId"] == "thread-owned" and params["turnId"] == "turn-owned"
        result(message, {})
        finish("interrupted")
    elif message.get("id") == 901 and "result" in message:
        state["approval_replies"] = state.get("approval_replies", 0) + 1
        save()
        if scenario == "approval_replay":
            send(
                {
                    "id": 901,
                    "method": "item/commandExecution/requestApproval",
                    "params": {
                        "threadId": "thread-owned",
                        "turnId": "turn-owned",
                        "itemId": "command-one",
                        "command": "fixture command",
                        "startedAtMs": 1000,
                    },
                }
            )
        else:
            finish()
    else:
        raise AssertionError("Unexpected protocol operation")
