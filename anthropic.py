from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from . import config


BETA_TOOL_FIELDS = {"strict", "eager_input_streaming", "defer_loading", "cache_control"}
UNSUPPORTED_BUILTIN_PREFIXES = ("bash_", "text_editor_", "computer_")


def resolve_model(model: str) -> str:
    if not model:
        return config.DEFAULT_MODEL
    aliases = {
        "claude-3-5-haiku-latest": "claude-haiku-4-5",
        "claude-3-5-sonnet-latest": "claude-sonnet-4",
        "claude-sonnet-4-5": "claude-sonnet-4.5",
        "claude-sonnet-4-0": "claude-sonnet-4",
        "claude-opus-4-5": "claude-opus-4.5",
        "claude-haiku-4-5": "claude-haiku-4.5",
    }
    return aliases.get(model, model)


def system_to_text(system: Any) -> str:
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        return "\n".join(block.get("text", "") for block in system if block.get("type") == "text")
    return ""


def block_text(block: Any) -> str:
    if isinstance(block, str):
        return block
    if not isinstance(block, dict):
        return json.dumps(block, ensure_ascii=False)
    btype = block.get("type")
    if btype == "text":
        return block.get("text", "")
    if btype == "tool_result":
        content = block.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(block_text(item) for item in content)
    return json.dumps(block, ensure_ascii=False)


def content_to_parts(content: Any, tool_name_by_id: dict[str, str]) -> list[dict]:
    if isinstance(content, str):
        return [{"text": content or " "}]
    if not isinstance(content, list):
        return [{"text": block_text(content) or " "}]

    parts: list[dict] = []
    for block in content:
        if not isinstance(block, dict):
            parts.append({"text": str(block)})
            continue
        btype = block.get("type")
        if btype == "text":
            parts.append({"text": block.get("text", "") or " "})
        elif btype == "image":
            src = block.get("source", {})
            if src.get("type") == "base64" and src.get("data"):
                parts.append({
                    "inlineData": {
                        "mimeType": src.get("media_type", "image/png"),
                        "data": src.get("data", ""),
                    }
                })
        elif btype == "tool_use":
            tool_id = block.get("id", "")
            name = block.get("name", "")
            if tool_id and name:
                tool_name_by_id[tool_id] = name
            parts.append({"functionCall": {"name": name, "args": block.get("input", {})}})
        elif btype == "tool_result":
            tool_id = block.get("tool_use_id", "")
            name = tool_name_by_id.get(tool_id, "tool_result")
            parts.append({
                "functionResponse": {
                    "name": name,
                    "response": {"content": block_text(block)},
                }
            })
        else:
            parts.append({"text": json.dumps(block, ensure_ascii=False)})
    return parts or [{"text": " "}]


def messages_to_contents(messages: list[dict]) -> list[dict]:
    contents: list[dict] = []
    tool_name_by_id: dict[str, str] = {}
    for msg in messages:
        role = "model" if msg.get("role") == "assistant" else "user"
        parts = content_to_parts(msg.get("content", ""), tool_name_by_id)
        if contents and contents[-1]["role"] == role:
            contents[-1]["parts"].extend(parts)
        else:
            contents.append({"role": role, "parts": parts})
    return contents


def sanitize_tools(tools: list[dict] | None) -> list[dict]:
    cleaned: list[dict] = []
    for tool in tools or []:
        tool_type = str(tool.get("type", ""))
        if tool_type.startswith(UNSUPPORTED_BUILTIN_PREFIXES):
            continue
        if tool_type.startswith("web_search"):
            cleaned.append({
                "name": tool.get("name", "web_search"),
                "description": "Search the web for current information.",
                "input_schema": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            })
            continue
        cleaned.append({k: v for k, v in tool.items() if k not in BETA_TOOL_FIELDS and k != "type"})
    return [tool for tool in cleaned if tool.get("name")]


def tools_to_gemini(tools: list[dict] | None) -> list[dict]:
    declarations = []
    for tool in sanitize_tools(tools):
        declarations.append({
            "name": tool.get("name", ""),
            "description": tool.get("description", ""),
            "parameters": tool.get("input_schema", {"type": "object", "properties": {}}),
        })
    return [{"functionDeclarations": declarations}] if declarations else []


def tool_choice_to_config(tool_choice: dict | None) -> dict | None:
    if not tool_choice:
        return None
    choice_type = tool_choice.get("type", "auto")
    if choice_type == "none":
        return {"functionCallingConfig": {"mode": "NONE"}}
    if choice_type == "any":
        return {"functionCallingConfig": {"mode": "ANY"}}
    if choice_type == "tool":
        return {
            "functionCallingConfig": {
                "mode": "ANY",
                "allowedFunctionNames": [tool_choice.get("name", "")],
            }
        }
    return {"functionCallingConfig": {"mode": "AUTO"}}


def anthropic_to_antigravity_request(body: dict) -> tuple[str, dict]:
    model = resolve_model(body.get("model", config.DEFAULT_MODEL))
    generation_config: dict[str, Any] = {
        "temperature": body.get("temperature", config.DEFAULT_TEMPERATURE),
        "maxOutputTokens": body.get("max_tokens", config.DEFAULT_MAX_OUTPUT_TOKENS),
    }
    if body.get("top_p") is not None:
        generation_config["topP"] = body["top_p"]
    if body.get("top_k") is not None:
        generation_config["topK"] = body["top_k"]
    if body.get("stop_sequences"):
        generation_config["stopSequences"] = body["stop_sequences"]

    request: dict[str, Any] = {
        "contents": messages_to_contents(body.get("messages", [])),
        "generationConfig": generation_config,
    }
    system_text = system_to_text(body.get("system"))
    if system_text:
        request["systemInstruction"] = {"parts": [{"text": system_text}]}

    gemini_tools = tools_to_gemini(body.get("tools"))
    if gemini_tools:
        request["tools"] = gemini_tools
    tool_config = tool_choice_to_config(body.get("tool_choice"))
    if tool_config:
        request["toolConfig"] = tool_config
    return model, request


@dataclass
class AggregatedResponse:
    text: str = ""
    tool_uses: list[dict] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    stop_reason: str = "end_turn"


def extract_parts(chunk: dict) -> list[dict]:
    parts: list[dict] = []
    for cand in chunk.get("response", {}).get("candidates", []):
        parts.extend(cand.get("content", {}).get("parts", []) or [])
        finish = cand.get("finishReason") or cand.get("finish_reason")
        if finish:
            parts.append({"_finishReason": finish})
    return parts


def aggregate_chunks(chunks: list[dict]) -> AggregatedResponse:
    agg = AggregatedResponse()
    for chunk in chunks:
        usage = chunk.get("response", {}).get("usageMetadata", {})
        agg.input_tokens = max(agg.input_tokens, int(usage.get("promptTokenCount", 0) or 0))
        agg.output_tokens = max(agg.output_tokens, int(usage.get("candidatesTokenCount", 0) or 0))
        for part in extract_parts(chunk):
            if "text" in part:
                agg.text += part.get("text", "")
            if "functionCall" in part:
                fc = part["functionCall"]
                agg.tool_uses.append({
                    "type": "tool_use",
                    "id": f"toolu_{uuid.uuid4().hex[:24]}",
                    "name": fc.get("name", ""),
                    "input": fc.get("args", {}) or {},
                })
            if "_finishReason" in part:
                reason = str(part["_finishReason"]).upper()
                agg.stop_reason = {
                    "MAX_TOKENS": "max_tokens",
                    "STOP": "end_turn",
                    "SAFETY": "stop_sequence",
                    "RECITATION": "stop_sequence",
                }.get(reason, "end_turn")
    if agg.tool_uses:
        agg.stop_reason = "tool_use"
    if not agg.output_tokens and agg.text:
        agg.output_tokens = max(1, len(agg.text) // 4)
    return agg


def aggregate_to_anthropic(agg: AggregatedResponse, model: str) -> dict:
    content: list[dict] = []
    if agg.text:
        content.append({"type": "text", "text": agg.text})
    content.extend(agg.tool_uses)
    return {
        "id": f"msg_{uuid.uuid4().hex}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content or [{"type": "text", "text": ""}],
        "stop_reason": agg.stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": agg.input_tokens,
            "output_tokens": agg.output_tokens,
        },
    }


def estimate_count_tokens(body: dict) -> int:
    chars = len(system_to_text(body.get("system")))
    for msg in body.get("messages", []):
        chars += len(block_text({"type": "text", "text": ""}))
        content = msg.get("content", "")
        if isinstance(content, str):
            chars += len(content)
        elif isinstance(content, list):
            chars += sum(len(block_text(block)) for block in content)
    chars += len(json.dumps(body.get("tools", []), ensure_ascii=False))
    return max(1, chars // 4)
