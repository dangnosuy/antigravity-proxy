from __future__ import annotations

import argparse
import base64
import json
import os
import platform
import re
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

from . import config


def default_db_candidates() -> list[Path]:
    home = Path.home()
    system = platform.system().lower()
    candidates: list[Path] = []

    if system == "windows":
        appdata = os.environ.get("APPDATA")
        localappdata = os.environ.get("LOCALAPPDATA")
        if appdata:
            candidates.append(Path(appdata) / "Antigravity" / "User" / "globalStorage" / "state.vscdb")
        if localappdata:
            candidates.append(Path(localappdata) / "Antigravity" / "User" / "globalStorage" / "state.vscdb")
        candidates.append(home / "AppData" / "Roaming" / "Antigravity" / "User" / "globalStorage" / "state.vscdb")
        candidates.append(home / "AppData" / "Local" / "Antigravity" / "User" / "globalStorage" / "state.vscdb")
    elif system == "darwin":
        candidates.append(home / "Library" / "Application Support" / "Antigravity" / "User" / "globalStorage" / "state.vscdb")
        candidates.append(home / ".config" / "Antigravity" / "User" / "globalStorage" / "state.vscdb")
    else:
        candidates.append(home / ".config" / "Antigravity" / "User" / "globalStorage" / "state.vscdb")

    # Some builds may use lowercase app directory names.
    candidates.extend([
        home / ".config" / "antigravity" / "User" / "globalStorage" / "state.vscdb",
        home / ".antigravity" / "User" / "globalStorage" / "state.vscdb",
    ])

    unique: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        key = str(path)
        if key not in seen:
            unique.append(path)
            seen.add(key)
    return unique


def default_db_path() -> Path:
    candidates = default_db_candidates()
    for path in candidates:
        if path.exists():
            return path
    return candidates[0]


DEFAULT_DB_PATH = default_db_path()
OAUTH_TOKEN_KEY = "antigravityUnifiedStateSync.oauthToken"
AUTH_STATUS_KEY = "antigravityAuthStatus"

REFRESH_RE = re.compile(r"1//[A-Za-z0-9_+/=-]{20,}")
ACCESS_RE = re.compile(r"ya29\.[A-Za-z0-9_.-]{50,}")
BASE64_CHUNK_RE = re.compile(r"[A-Za-z0-9+/=_-]{40,}")


@dataclass
class ExtractedTokens:
    access_token: str = ""
    refresh_token: str = ""
    email: str = ""
    name: str = ""
    source_db: str = ""

    def as_redacted_dict(self) -> dict:
        return {
            "access_token": mask_secret(self.access_token),
            "refresh_token": mask_secret(self.refresh_token),
            "email": self.email,
            "name": self.name,
            "source_db": self.source_db,
        }


def mask_secret(value: str, left: int = 18, right: int = 8) -> str:
    if not value:
        return ""
    if len(value) <= left + right + 3:
        return value[:4] + "..."
    return f"{value[:left]}...{value[-right:]}"


def connect_readonly(db_path: Path) -> sqlite3.Connection:
    uri = f"file:{db_path}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def read_state_value(db_path: Path, key: str) -> str:
    with connect_readonly(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM ItemTable WHERE key = ?", (key,))
        row = cursor.fetchone()
    return row[0] if row else ""


def decode_possible_base64(chunk: str) -> list[str]:
    decoded: list[str] = []
    for decoder in (base64.b64decode, base64.urlsafe_b64decode):
        for padded in (chunk, chunk + "=", chunk + "=="):
            try:
                raw = decoder(padded)
                decoded.append(raw.decode("utf-8", errors="replace"))
            except Exception:
                continue
    return decoded


def extract_from_oauth_token(raw_b64: str) -> tuple[str, str]:
    """Extract access and refresh tokens from Antigravity's oauthToken blob.

    The value is base64-encoded protobuf-like bytes. Readable strings inside it
    may contain nested base64 strings, so the extractor decodes both layers and
    searches for Google token shapes.
    """
    if not raw_b64:
        return "", ""
    try:
        decoded = base64.b64decode(raw_b64)
    except Exception:
        return "", ""

    text = decoded.decode("utf-8", errors="replace")
    all_text = [text]
    for match in BASE64_CHUNK_RE.finditer(text):
        all_text.extend(decode_possible_base64(match.group()))
    joined = "\n".join(all_text)

    refresh_matches = REFRESH_RE.findall(joined)
    access_matches = ACCESS_RE.findall(joined)
    refresh_token = max(refresh_matches, key=len) if refresh_matches else ""
    access_token = max(access_matches, key=len) if access_matches else ""
    return access_token, refresh_token


def extract_from_auth_status(raw_json: str) -> tuple[str, str, str]:
    if not raw_json:
        return "", "", ""
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError:
        return "", "", ""
    return data.get("apiKey", "") or "", data.get("email", "") or "", data.get("name", "") or ""


def extract_tokens(db_path: Path = DEFAULT_DB_PATH) -> ExtractedTokens:
    if not db_path.exists():
        raise FileNotFoundError(f"Antigravity database not found: {db_path}")

    oauth_value = read_state_value(db_path, OAUTH_TOKEN_KEY)
    auth_status_value = read_state_value(db_path, AUTH_STATUS_KEY)

    oauth_access, refresh_token = extract_from_oauth_token(oauth_value)
    status_access, email, name = extract_from_auth_status(auth_status_value)

    return ExtractedTokens(
        access_token=status_access or oauth_access,
        refresh_token=refresh_token,
        email=email,
        name=name,
        source_db=str(db_path),
    )


def write_token_files(tokens: ExtractedTokens, out_dir: Path, append_refresh: bool = True) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    token_path = out_dir / "token.txt"
    refresh_path = out_dir / "refresh_token.txt"

    if tokens.access_token:
        token_path.write_text(tokens.access_token.strip() + "\n", encoding="utf-8")

    if tokens.refresh_token:
        if append_refresh and refresh_path.exists():
            existing = [
                line.strip()
                for line in refresh_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            if tokens.refresh_token not in existing:
                existing.append(tokens.refresh_token)
            refresh_path.write_text("\n".join(existing) + "\n", encoding="utf-8")
        else:
            refresh_path.write_text(tokens.refresh_token.strip() + "\n", encoding="utf-8")

    return token_path, refresh_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Extract local Antigravity OAuth tokens into token.txt and refresh_token.txt.",
    )
    parser.add_argument("--db", type=Path, default=None, help="Path to Antigravity state.vscdb")
    parser.add_argument("--out-dir", type=Path, default=config.DEFAULT_TOKEN_DIR, help="Directory for token files")
    parser.add_argument("--dry-run", action="store_true", help="Print redacted results without writing files")
    parser.add_argument("--overwrite-refresh", action="store_true", help="Overwrite refresh_token.txt instead of appending unique token")
    parser.add_argument("--json", action="store_true", help="Print redacted JSON result")
    parser.add_argument("--print-paths", action="store_true", help="Print auto-detected database candidate paths")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.print_paths:
        for path in default_db_candidates():
            marker = "exists" if path.exists() else "missing"
            print(f"{marker}: {path}")
        return 0

    db_path = args.db or default_db_path()
    try:
        tokens = extract_tokens(db_path)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        if args.db is None:
            print("checked paths:", file=sys.stderr)
            for path in default_db_candidates():
                print(f"  {path}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(tokens.as_redacted_dict(), indent=2, ensure_ascii=False))
    else:
        print("Antigravity token extractor")
        print(f"Database: {tokens.source_db}")
        if tokens.name or tokens.email:
            print(f"Account: {tokens.name or '?'} <{tokens.email or '?'}>")
        print(f"Access token:  {mask_secret(tokens.access_token) or 'not found'}")
        print(f"Refresh token: {mask_secret(tokens.refresh_token) or 'not found'}")

    if args.dry_run:
        return 0

    if not tokens.refresh_token:
        print("error: refresh token not found; make sure Antigravity is logged in", file=sys.stderr)
        return 1

    token_path, refresh_path = write_token_files(
        tokens,
        args.out_dir,
        append_refresh=not args.overwrite_refresh,
    )
    print(f"Saved access token:  {token_path}")
    print(f"Saved refresh token: {refresh_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
