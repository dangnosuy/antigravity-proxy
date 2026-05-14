from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from . import config


class TokenStore:
    """Loads Antigravity access tokens and refreshes them when possible."""

    def __init__(
        self,
        token_file: Path = config.TOKEN_FILE,
        refresh_token_file: Path = config.REFRESH_TOKEN_FILE,
    ) -> None:
        self.token_file = token_file
        self.refresh_token_file = refresh_token_file
        self._refresh_index = 0
        self._lock = asyncio.Lock()

    def load_access_token(self) -> str:
        if not self.token_file.exists():
            return ""
        token = self.token_file.read_text(encoding="utf-8").strip()
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
        return token

    def write_access_token(self, token: str) -> None:
        self.token_file.write_text(token.strip(), encoding="utf-8")

    def load_refresh_tokens(self) -> list[str]:
        if not self.refresh_token_file.exists():
            return []
        return [
            line.strip()
            for line in self.refresh_token_file.read_text(encoding="utf-8").splitlines()
            if line.strip().startswith("1//")
        ]

    async def refresh_access_token(self, refresh_token: str) -> str:
        if not config.OAUTH_CLIENT_ID or not config.OAUTH_CLIENT_SECRET:
            return ""
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                config.OAUTH_TOKEN_URL,
                data={
                    "client_id": config.OAUTH_CLIENT_ID,
                    "client_secret": config.OAUTH_CLIENT_SECRET,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
            )
        if resp.status_code != 200:
            return ""
        token = resp.json().get("access_token", "")
        if token:
            self.write_access_token(token)
        return token

    async def refresh_with_fallback(self) -> str:
        async with self._lock:
            tokens = self.load_refresh_tokens()
            if not tokens:
                return ""
            start = self._refresh_index % len(tokens)
            for offset in range(len(tokens)):
                idx = (start + offset) % len(tokens)
                new_token = await self.refresh_access_token(tokens[idx])
                if new_token:
                    self._refresh_index = idx
                    return new_token
            return ""
