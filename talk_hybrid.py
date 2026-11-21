"""Voice controls for exact, observed Hermes submissions."""

from __future__ import annotations

import asyncio
import hashlib
import json

try:
    from . import talk_delegated
    from . import talk_realtime as rt
except ImportError:
    import talk_delegated
    import talk_realtime as rt


CONTROL_TOOL = {
    "type": "function",
    "name": "control_task",
    "description": (
        "Steer, cancel queued work, or interrupt an exact submitted Hermes turn. "
        "Use its request_id and available controls from submission_status. "
        "This does not control background children started by that turn."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "request_id": {"type": "string"},
            "action": {"type": "string", "enum": ["steer", "cancel", "interrupt"]},
            "text": {"type": "string"},
        },
        "required": ["request_id", "action"],
        "additionalProperties": False,
    },
}

TOOLS = [*talk_delegated.TOOLS, CONTROL_TOOL]

DELEGATE_TOOL = {
    "type": "function",
    "name": "delegate_work",
    "description": (
        "Start long work as a native background child of this Hermes conversation. "
        "Refuses instead of running inline."
    ),
    "parameters": {
        "type": "object",
        "properties": {"task": {"type": "string"}},
        "required": ["task"],
        "additionalProperties": False,
    },
}
CHILD_CONTROL_TOOL = {
    "type": "function",
    "name": "delegation_control",
    "description": (
        "List this conversation's native children, queue a correction for an exact child, "
        "or request its stop. A queued correction is not proof it has been applied."
        " Steering delivery is backend-dependent; native Codex app-server delivery "
        "is not supported by this integration."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "steer", "stop"]},
            "subagent_id": {"type": "string"},
            "message": {"type": "string"},
        },
        "required": ["action"],
        "additionalProperties": False,
    },
}


def tools(catalog):
    """Freeze the offered host surface; native delegation cannot silently run inline."""
    offered = []
    reserved = {tool["name"] for tool in [*TOOLS, DELEGATE_TOOL, CHILD_CONTROL_TOOL]}
    for schema in catalog:
        function = schema.get("function", schema)
        name = function.get("name")
        if not isinstance(name, str) or name in reserved:
            raise ValueError("Host tool name conflicts with the voice control surface")
        if name == "delegate_task":
            offered.extend([DELEGATE_TOOL, CHILD_CONTROL_TOOL])
        else:
            offered.append({"type": "function", **function})
    return [*TOOLS, *offered]


class HybridRelay(talk_delegated.SubmissionRelay):
    """Keep controls outside the conversation queue and bound to observed execution."""

    def __init__(self, binding, authorizer, catalog=()):
        super().__init__(binding, authorizer)
        self.observed = {}
        self.control_attempts = {}
        self.host_names = {(schema.get("function", schema)).get("name") for schema in catalog}
        self.direct_requests = {}

    async def status(self, request_id):
        if request_id in self.direct_requests:
            receipt = await asyncio.to_thread(
                self.binding.ctx.execute_tools,
                **self.direct_requests[request_id],
            )
            if receipt.get("outputs"):
                receipt = {
                    **receipt,
                    "result": {
                        "final_response": "Host tool outputs: "
                        + json.dumps(receipt["outputs"])[:16000],
                    },
                }
        else:
            receipt = await super().status(request_id)
        if receipt.get("ok"):
            self.observed[request_id] = receipt
        return receipt

    async def handle_tool_call_async(self, event):
        if event.get("name") in self.host_names | {"delegate_work", "delegation_control"}:
            return await self.execute_direct(event)
        if event.get("name") != "control_task":
            return await super().handle_tool_call_async(event)
        call_id = str(event.get("call_id") or "")
        denial = self.authorizer("control_task", event)
        result = {"ok": False, "error": "invalid_params"}
        try:
            if denial or self.closed:
                result = {"ok": False, "error": denial or "Voice binding closed; rejoin Talk."}
            elif call_id in self.control_attempts:
                result = self.control_attempts[call_id]
            else:
                args = json.loads(event.get("arguments") or "{}")
                if not isinstance(args, dict) or set(args) - {"request_id", "action", "text"}:
                    raise ValueError("Unexpected control arguments")
                request_id, action = args.get("request_id"), args.get("action")
                if not isinstance(request_id, str) or request_id not in self.requests:
                    raise ValueError("Unknown request outside this voice call")
                if action not in {"steer", "cancel", "interrupt"}:
                    raise ValueError("Unsupported control action")
                if len(self.control_attempts) >= 256:
                    raise ValueError("Voice control limit reached; rejoin Talk")
                if action == "steer":
                    text = args.get("text")
                    if not isinstance(text, str) or not text.strip() or len(text) > 16000:
                        raise ValueError(
                            "Steering requires nonempty text of at most 16000 characters"
                        )
                elif "text" in args:
                    raise ValueError("Only steering accepts text")
                observed = self.observed.get(request_id)
                if observed is None:
                    observed = await self.status(request_id)
                if action not in observed.get("controls", []):
                    raise ValueError("This control is not available; consult submission_status")
                epoch = observed.get("authority_epoch")
                if type(epoch) is not int:
                    raise ValueError("The host did not supply an execution authority")
                # A lost acknowledgement is not permission to repeat a steering instruction.
                self.control_attempts[call_id] = {
                    "ok": False,
                    "status": "unknown",
                    "error": "control_outcome_unknown",
                }
                result = await asyncio.to_thread(
                    self.binding.ctx.control_submission,
                    action,
                    **self.identity(request_id),
                    authority_epoch=epoch,
                    execution_generation=observed.get("execution_generation"),
                    text=args.get("text"),
                )
                self.control_attempts[call_id] = result
                self.observed.pop(request_id, None)
        except (ValueError, TypeError, KeyError) as exc:
            result = {"ok": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - an ambiguous host write must not be retried
            result = {"ok": False, "status": "unknown", "error": type(exc).__name__}
            if call_id in self.control_attempts:
                self.control_attempts[call_id] = result
        return [rt.SubmitToolResult(call_id=call_id, output=json.dumps(result))]

    async def execute_direct(self, event):
        name, call_id = str(event.get("name") or ""), str(event.get("call_id") or "")
        denial = self.authorizer(name, event)
        try:
            if denial or self.closed:
                raise ValueError(denial or "Voice binding closed; rejoin Talk")
            arguments = json.loads(event.get("arguments") or "{}")
            if not isinstance(arguments, dict) or len(json.dumps(arguments)) > 32000:
                raise ValueError("Invalid or oversized tool arguments")
            native_name, background = name, False
            if name in {"delegate_work", "delegation_control"}:
                if "delegate_task" not in self.host_names:
                    raise ValueError("Native background delegation is unavailable")
                native_name = "delegate_task"
                if name == "delegate_work":
                    if (
                        set(arguments) != {"task"}
                        or not isinstance(arguments["task"], str)
                        or not arguments["task"].strip()
                    ):
                        raise ValueError("delegate_work requires a complete task brief")
                    arguments = {"tasks": [{"goal": arguments["task"], "group": "voice-task"}]}
                    background = True
                else:
                    if set(arguments) - {"action", "subagent_id", "message"} or arguments.get(
                        "action"
                    ) not in {"list", "steer", "stop"}:
                        raise ValueError("Unsupported native child control")
            elif name == "delegate_task":
                raise ValueError("Use delegate_work or delegation_control")
            request_id = hashlib.sha256(f"{self.binding.nonce}:tool:{call_id}".encode()).hexdigest()
            request = {
                **self.identity(request_id),
                "calls": [{"call_id": call_id, "name": native_name, "arguments": arguments}],
                "text": "Voice-requested tool operation: "
                + json.dumps({"name": name, "arguments": arguments}),
                "require_background": background,
            }
            previous = self.direct_requests.get(request_id)
            if previous is not None and previous != request:
                raise ValueError("A repeated call ID cannot change its tool operation")
            if previous is None and len(self.requests) >= talk_delegated.MAX_REQUESTS:
                raise ValueError("Voice request limit reached; rejoin Talk")
            self.direct_requests[request_id] = request
            self.requests[request_id] = request["text"]
            self.pending.add(request_id)
            try:
                result = await self.status(request_id)
            finally:
                self.pending.discard(request_id)
            if result.get("status") in {"terminal", "refused"}:
                self.announced.add(request_id)
        except (ValueError, TypeError, KeyError) as exc:
            result = {"ok": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - a lost tool acknowledgement may have committed
            result = {"ok": False, "status": "unknown", "error": type(exc).__name__}
            if "request_id" in locals():
                result["request_id"] = request_id
        return [rt.SubmitToolResult(call_id=call_id, output=json.dumps(result))]
