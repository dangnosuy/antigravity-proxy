from __future__ import annotations

import json
from typing import AsyncIterator

import httpx

from . import config
from .auth import TokenStore


class AntigravityError(RuntimeError):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


class AntigravityClient:
    def __init__(self, token_store: TokenStore | None = None) -> None:
        self.token_store = token_store or TokenStore()
        self.project = ""

    def _headers(self, token: str) -> dict[str, str]:
        return {**config.HEADERS_BASE, "authorization": f"Bearer {token}"}

    async def _token(self) -> str:
        token = self.token_store.load_access_token()
        if not token:
            token = await self.token_store.refresh_with_fallback()
        if not token:
            raise AntigravityError(401, "Missing Antigravity token. Create token.txt or refresh_token.txt.")
        return token

    async def post_internal(self, endpoint: str, body: dict) -> dict:
        token = await self._token()
        url = f"{config.ANTIGRAVITY_BASE_URL}/v1internal:{endpoint}"
        async with httpx.AsyncClient(timeout=httpx.Timeout(20, read=120)) as client:
            resp = await client.post(url, headers=self._headers(token), json=body)
            if resp.status_code == 401:
                token = await self.token_store.refresh_with_fallback()
                if token:
                    resp = await client.post(url, headers=self._headers(token), json=body)
        if resp.status_code >= 400:
            raise AntigravityError(resp.status_code, resp.text[:1000])
        return resp.json()

    async def init_project(self) -> str:
        if self.project:
            return self.project
        try:
            data = await self.post_internal(
                "loadCodeAssist",
                {
                    "metadata": {
                        "ide_type": 9,
                        "ide_version": config.ANTIGRAVITY_VERSION,
                        "plugin_version": "",
                        "platform": 0,
                        "update_channel": "",
                        "duet_project": "",
                        "plugin_type": 0,
                        "ide_name": "antigravity",
                    }
                },
            )
            self.project = data.get("cloudaicompanionProject", "")
        except Exception:
            self.project = ""
        return self.project

    async def fetch_models(self) -> dict:
        return await self.post_internal("fetchAvailableModels", {"project": ""})

    async def stream_generate_content(self, model: str, request_body: dict) -> AsyncIterator[dict]:
        project = await self.init_project()
        token = await self._token()
        url = f"{config.ANTIGRAVITY_BASE_URL}/v1internal:streamGenerateContent"
        body = {"model": model, "project": project, "request": request_body}

        async with httpx.AsyncClient(timeout=httpx.Timeout(20, read=300)) as client:
            async with client.stream("POST", url, headers=self._headers(token), json=body) as resp:
                if resp.status_code == 401:
                    await resp.aread()
                    token = await self.token_store.refresh_with_fallback()
                    if not token:
                        raise AntigravityError(401, "Antigravity token expired and refresh failed.")
                elif resp.status_code >= 400:
                    err = await resp.aread()
                    raise AntigravityError(resp.status_code, err.decode("utf-8", errors="replace")[:1000])
                else:
                    async for obj in parse_json_stream(resp):
                        yield obj
                    return

            async with client.stream("POST", url, headers=self._headers(token), json=body) as retry:
                if retry.status_code >= 400:
                    err = await retry.aread()
                    raise AntigravityError(retry.status_code, err.decode("utf-8", errors="replace")[:1000])
                async for obj in parse_json_stream(retry):
                    yield obj


async def parse_json_stream(resp: httpx.Response) -> AsyncIterator[dict]:
    buf = ""
    async for chunk in resp.aiter_text():
        if not chunk:
            continue
        buf += chunk
        while True:
            obj, end = parse_one_json_object(buf)
            if obj is None:
                break
            buf = buf[end:]
            yield obj


def parse_one_json_object(text: str) -> tuple[dict | None, int]:
    offset = 0
    while offset < len(text) and text[offset] in " \t\n\r[,]":
        offset += 1
    if offset >= len(text) or text[offset] != "{":
        return None, 0

    depth = 0
    in_string = False
    escaped = False
    for i in range(offset, len(text)):
        ch = text[i]
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[offset : i + 1]), i + 1
                except json.JSONDecodeError:
                    return None, 0
    return None, 0
