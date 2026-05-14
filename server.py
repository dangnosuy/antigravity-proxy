from __future__ import annotations

import asyncio
import json
import uuid
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from . import __version__, config
from .anthropic import (
    AggregatedResponse,
    aggregate_chunks,
    aggregate_to_anthropic,
    anthropic_to_antigravity_request,
    estimate_count_tokens,
    extract_parts,
)
from .antigravity import AntigravityClient, AntigravityError


app = FastAPI(title="Antigravity Anthropic Proxy", version=__version__)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
client = AntigravityClient()


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


_MAX_RETRIES = 2
_RETRY_429_WAIT = 2.0  # seconds to wait before retrying a 429


def _error_type_for_status(status_code: int) -> str:
    if status_code == 401:
        return "authentication_error"
    if status_code == 400:
        return "invalid_request_error"
    if status_code == 429:
        return "rate_limit_error"
    if status_code == 529:
        return "overloaded_error"
    if status_code == 404:
        return "not_found_error"
    return "api_error"


def anthropic_error(status_code: int, message: str) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={
            "type": "error",
            "error": {
                "type": _error_type_for_status(status_code),
                "message": message,
            },
        },
    )


def count_tools(body: dict) -> int:
    return len(body.get("tools") or [])


def antigravity_model_to_anthropic(model_id: str, info: dict) -> dict:
    return {
        "id": model_id,
        "type": "model",
        "display_name": info.get("displayName", model_id),
        "created_at": info.get("createTime") or info.get("createdAt") or "1970-01-01T00:00:00Z",
    }


async def load_anthropic_models() -> list[dict]:
    try:
        data = await client.fetch_models()
    except AntigravityError as exc:
        raise anthropic_error(exc.status_code, exc.message)

    items = []
    for model_id, info in (data.get("models") or {}).items():
        if info.get("displayName") and not info.get("isInternal"):
            items.append(antigravity_model_to_anthropic(model_id, info))
    return sorted(items, key=lambda item: item["id"])


@app.get("/")
@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "antigravity-anthropic-proxy",
        "version": __version__,
        "token_file": str(config.TOKEN_FILE),
        "refresh_token_file": str(config.REFRESH_TOKEN_FILE),
    }


@app.get("/v1/models")
@app.get("/models")
async def models():
    items = await load_anthropic_models()
    return {
        "data": items,
        "first_id": items[0]["id"] if items else None,
        "has_more": False,
        "last_id": items[-1]["id"] if items else None,
    }


@app.get("/v1/models/{model_id:path}")
async def model_detail(model_id: str):
    for item in await load_anthropic_models():
        if item["id"] == model_id:
            return item
    print(f"ERROR model_not_found model={model_id}")
    raise anthropic_error(404, f"Model not found: {model_id}")


@app.post("/v1/messages/count_tokens")
async def count_tokens(request: Request):
    try:
        body = await request.json()
    except Exception:
        print("ERROR count_tokens invalid_json")
        raise anthropic_error(400, "Invalid JSON body.")
    tokens = estimate_count_tokens(body)
    return {"input_tokens": tokens}


@app.post("/v1/messages")
async def create_message(request: Request):
    try:
        body = await request.json()
    except Exception:
        print("ERROR message invalid_json")
        raise anthropic_error(400, "Invalid JSON body.")

    for field in ("model", "max_tokens", "messages"):
        if field not in body:
            print(f"ERROR message missing_field={field}")
            raise anthropic_error(400, f"Missing required field: {field}")

    model, ag_request = anthropic_to_antigravity_request(body)
    requested_model = body.get("model", "")
    stream = bool(body.get("stream"))
    if config.LOG_PAYLOADS:
        print(f"DEBUG payload={json.dumps({'model': model, 'request': ag_request}, ensure_ascii=False)[:4000]}")

    if body.get("stream"):
        return StreamingResponse(
            stream_anthropic(model, ag_request),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    last_exc: AntigravityError | None = None
    for attempt in range(_MAX_RETRIES + 1):
        chunks = []
        try:
            async for chunk in client.stream_generate_content(model, ag_request):
                chunks.append(chunk)
            last_exc = None
            break
        except AntigravityError as exc:
            last_exc = exc
            if exc.status_code == 429 and attempt < _MAX_RETRIES:
                wait = _RETRY_429_WAIT * (attempt + 1)
                print(f"RETRY 429 model={model} attempt={attempt + 1} wait={wait}s")
                await asyncio.sleep(wait)
                continue
            print(f"ERROR message model={model} stream={stream} upstream_status={exc.status_code} detail={exc.message[:300]}")
            raise anthropic_error(exc.status_code, exc.message)
    if last_exc is not None:
        raise anthropic_error(last_exc.status_code, last_exc.message)
    agg = aggregate_chunks(chunks)
    print(
        f"MODEL model={model} requested={requested_model} stream=false "
        f"input_tokens={agg.input_tokens} output_tokens={agg.output_tokens} stop_reason={agg.stop_reason}"
    )
    return JSONResponse(content=aggregate_to_anthropic(agg, model))


async def stream_anthropic(model: str, ag_request: dict) -> AsyncIterator[str]:
    message_id = f"msg_{uuid.uuid4().hex}"
    yield sse(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": message_id,
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        },
    )

    text_index: int | None = None
    thinking_index: int | None = None
    next_index = 0
    agg = AggregatedResponse()
    try:
        async for chunk in client.stream_generate_content(model, ag_request):
            usage = chunk.get("response", {}).get("usageMetadata", {})
            agg.input_tokens = max(agg.input_tokens, int(usage.get("promptTokenCount", 0) or 0))
            agg.output_tokens = max(agg.output_tokens, int(usage.get("candidatesTokenCount", 0) or 0))
            for part in extract_parts(chunk):
                # Handle thinking blocks from upstream (thinking models)
                if "thought" in part or part.get("_partType") == "thinking":
                    thought_text = part.get("thought", "") or part.get("text", "")
                    if thought_text:
                        if thinking_index is None:
                            thinking_index = next_index
                            next_index += 1
                            yield sse("content_block_start", {
                                "type": "content_block_start",
                                "index": thinking_index,
                                "content_block": {"type": "thinking", "thinking": ""},
                            })
                        yield sse("content_block_delta", {
                            "type": "content_block_delta",
                            "index": thinking_index,
                            "delta": {"type": "thinking_delta", "thinking": thought_text},
                        })
                elif "text" in part:
                    # Close thinking block before starting text
                    if thinking_index is not None:
                        yield sse("content_block_stop", {"type": "content_block_stop", "index": thinking_index})
                        thinking_index = None
                    if text_index is None:
                        text_index = next_index
                        next_index += 1
                        yield sse("content_block_start", {
                            "type": "content_block_start",
                            "index": text_index,
                            "content_block": {"type": "text", "text": ""},
                        })
                    text = part.get("text", "")
                    agg.text += text
                    agg.output_tokens = max(agg.output_tokens, max(1, len(agg.text) // 4))
                    yield sse("content_block_delta", {
                        "type": "content_block_delta",
                        "index": text_index,
                        "delta": {"type": "text_delta", "text": text},
                    })
                elif "functionCall" in part:
                    # Close thinking block before tool use
                    if thinking_index is not None:
                        yield sse("content_block_stop", {"type": "content_block_stop", "index": thinking_index})
                        thinking_index = None
                    fc = part["functionCall"]
                    tool_index = next_index
                    next_index += 1
                    tool_id = f"toolu_{uuid.uuid4().hex[:24]}"
                    tool_input = fc.get("args", {}) or {}
                    agg.tool_uses.append({
                        "type": "tool_use",
                        "id": tool_id,
                        "name": fc.get("name", ""),
                        "input": tool_input,
                    })
                    yield sse("content_block_start", {
                        "type": "content_block_start",
                        "index": tool_index,
                        "content_block": {
                            "type": "tool_use",
                            "id": tool_id,
                            "name": fc.get("name", ""),
                            "input": {},
                        },
                    })
                    yield sse("content_block_delta", {
                        "type": "content_block_delta",
                        "index": tool_index,
                        "delta": {
                            "type": "input_json_delta",
                            "partial_json": json.dumps(tool_input, ensure_ascii=False),
                        },
                    })
                    yield sse("content_block_stop", {"type": "content_block_stop", "index": tool_index})
                elif "_finishReason" in part:
                    reason = str(part["_finishReason"]).upper()
                    agg.stop_reason = {
                        "MAX_TOKENS": "max_tokens",
                        "STOP": "end_turn",
                        "SAFETY": "stop_sequence",
                        "RECITATION": "stop_sequence",
                    }.get(reason, "end_turn")
    except AntigravityError as exc:
        print(f"ERROR message model={model} stream=true upstream_status={exc.status_code} detail={exc.message[:300]}")
        yield sse("error", {
            "type": "error",
            "error": {"type": _error_type_for_status(exc.status_code), "message": exc.message},
        })
        return

    if thinking_index is not None:
        yield sse("content_block_stop", {"type": "content_block_stop", "index": thinking_index})
    if text_index is not None:
        yield sse("content_block_stop", {"type": "content_block_stop", "index": text_index})
    if agg.tool_uses:
        agg.stop_reason = "tool_use"
    print(
        f"MODEL model={model} stream=true "
        f"input_tokens={agg.input_tokens} output_tokens={agg.output_tokens} stop_reason={agg.stop_reason}"
    )
    yield sse(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": agg.stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": agg.output_tokens},
        },
    )
    yield sse("message_stop", {"type": "message_stop"})


def main() -> None:
    import uvicorn

    print(f"Antigravity Anthropic Proxy listening on http://127.0.0.1:{config.PORT}")
    print(f"Token file: {config.TOKEN_FILE}")
    uvicorn.run(
        "ag_proxy.server:app",
        host="0.0.0.0",
        port=config.PORT,
        reload=False,
        access_log=True,
        log_level="info",
    )


if __name__ == "__main__":
    main()
