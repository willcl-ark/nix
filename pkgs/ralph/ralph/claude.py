"""Native Claude wire format for the shared review and tool loop."""

import json


def request(data):
    messages = []
    for item in data.get("input", []):
        if "claude_content" in item:
            messages.append({"role": "assistant", "content": item["claude_content"]})
        elif item.get("type") in ("function_call", "web_search_call"):
            # Already retained in the assistant's original native content.
            continue
        elif item.get("type") == "function_call_output":
            block = {
                "type": "tool_result",
                "tool_use_id": item["call_id"],
                "content": item["output"],
            }
            if (
                messages
                and messages[-1]["role"] == "user"
                and isinstance(messages[-1]["content"], list)
            ):
                messages[-1]["content"].append(block)
            else:
                messages.append({"role": "user", "content": [block]})
        else:
            messages.append(item)
    payload = {
        "model": data["model"],
        "messages": messages,
        "max_tokens": data.get("max_output_tokens", 6000),
        "stream": True,
    }
    if data.get("instructions"):
        payload["system"] = data["instructions"]
    output_config = {}
    effort = data.get("reasoning", {}).get("effort")
    if effort and effort != "none":
        payload["thinking"] = {"type": "adaptive"}
        output_config["effort"] = effort
    format_ = data.get("text", {}).get("format")
    if format_ and format_.get("type") == "json_schema":
        output_config["format"] = {"type": "json_schema", "schema": format_["schema"]}
    if output_config:
        payload["output_config"] = output_config
    if data.get("tools"):
        tools = []
        for tool in data["tools"]:
            if tool["type"] == "function":
                tools.append(
                    {
                        "name": tool["name"],
                        "description": tool.get("description", ""),
                        "input_schema": tool["parameters"],
                    }
                )
            elif tool["type"] == "web_search":
                tools.append(
                    {
                        "type": "web_search_20250305",
                        "name": "web_search",
                        "max_uses": data.get("max_tool_calls", 2),
                    }
                )
            else:
                raise ValueError(f"Unsupported Claude tool type: {tool['type']}")
        payload["tools"] = tools
        choice = data.get("tool_choice", "auto")
        payload["tool_choice"] = {"type": "any" if choice == "required" else choice}
    return payload


def response(message):
    content = message.get("content", [])
    output = [
        {
            "type": "message",
            "role": "assistant",
            "claude_content": content,
            "content": [
                {"type": "output_text", "text": block["text"]}
                for block in content
                if block["type"] == "text"
            ],
        }
    ]
    for block in content:
        if block["type"] == "tool_use":
            output.append(
                {
                    "type": "function_call",
                    "call_id": block["id"],
                    "name": block["name"],
                    "arguments": json.dumps(block["input"]),
                }
            )
        elif block["type"] == "server_tool_use" and block["name"] == "web_search":
            result = next(
                (b for b in content if b.get("tool_use_id") == block["id"]), {}
            )
            failed = (
                isinstance(result.get("content"), dict)
                and result["content"].get("type") == "web_search_tool_result_error"
            )
            output.append(
                {
                    "type": "web_search_call",
                    "id": block["id"],
                    "status": "failed" if failed else "completed",
                    "action": {"type": "search", **block["input"]},
                }
            )
    usage = message.get("usage", {})
    cached = usage.get("cache_read_input_tokens", 0)
    written = usage.get("cache_creation_input_tokens", 0)
    input_tokens = usage.get("input_tokens")
    reason = message.get("stop_reason")
    result = {
        "id": message.get("id"),
        "model": message.get("model"),
        "status": "completed"
        if reason in ("end_turn", "tool_use", "stop_sequence")
        else "incomplete",
        "output": output,
        "usage": {
            "input_tokens": input_tokens + cached + written
            if input_tokens is not None
            else None,
            "output_tokens": usage.get("output_tokens"),
            "input_tokens_details": {
                "cached_tokens": cached,
                "cache_write_tokens": written,
            },
        },
    }
    if reason == "refusal":
        result.update(
            status="failed",
            error={"code": "refusal", "message": "Claude refused the request"},
        )
    elif result["status"] == "incomplete":
        result["incomplete_details"] = {
            "reason": "max_output_tokens" if reason == "max_tokens" else reason
        }
    return result


async def read_stream(events):
    message = None
    partial_inputs = {}
    open_blocks = set()
    stopped = False
    async for event in events:
        kind = event.get("type")
        if kind == "message_start":
            message = event["message"]
        elif kind == "content_block_start" and message is not None:
            if event["index"] != len(message["content"]):
                raise ValueError("Invalid Claude content block index")
            message["content"].append(event["content_block"])
            open_blocks.add(event["index"])
        elif kind == "content_block_delta" and message is not None:
            index = event["index"]
            block = message["content"][index]
            delta = event["delta"]
            if delta["type"] == "input_json_delta":
                partial_inputs[index] = (
                    partial_inputs.get(index, "") + delta["partial_json"]
                )
            elif delta["type"] in ("text_delta", "thinking_delta", "signature_delta"):
                key = delta["type"].removesuffix("_delta")
                block[key] = block.get(key, "") + delta[key]
            elif delta["type"] == "citations_delta":
                block.setdefault("citations", []).append(delta["citation"])
            else:
                raise ValueError("Unsupported Claude content delta")
        elif kind == "content_block_stop" and message is not None:
            index = event["index"]
            open_blocks.remove(index)
        elif kind == "message_delta" and message is not None:
            message.update(event["delta"])
            message.setdefault("usage", {}).update(event.get("usage", {}))
            stopped = bool(message.get("stop_reason"))
        elif kind == "message_stop" and message is not None:
            if not stopped or open_blocks:
                raise ValueError("Invalid terminal Claude response")
            incomplete_arguments = {}
            for index, partial in partial_inputs.items():
                if not partial:
                    continue
                block = message["content"][index]
                try:
                    block["input"] = json.loads(partial)
                except json.JSONDecodeError:
                    if message["stop_reason"] in (
                        "end_turn",
                        "tool_use",
                        "stop_sequence",
                    ):
                        raise
                    incomplete_arguments[block["id"]] = partial
            result = response(message)
            for item in result["output"]:
                if item.get("call_id") in incomplete_arguments:
                    item["arguments"] = incomplete_arguments[item["call_id"]]
            return result
    raise ConnectionError("Model stream ended before a terminal response")
