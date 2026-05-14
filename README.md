# Antigravity Anthropic Proxy

`ag_proxy` is a self-contained proxy toolkit that exposes an Anthropic-compatible
API backed by the local user's Antigravity account. It includes:

- a token extractor for Antigravity's local SQLite state database;
- a FastAPI proxy that serves `/v1/messages`, `/v1/messages/count_tokens`, and `/v1/models`;
- Anthropic-style model listing and model detail endpoints.

## Requirements

- Python 3.10+
- Antigravity installed and logged in on the same machine
- Network access to Google/Antigravity APIs

Install dependencies:

```bash
cd ag_proxy
python3 -m pip install -r requirements.txt
```

## Extract Tokens

Dry-run first. Output is redacted and no files are written:

```bash
cd ag_proxy
python3 extract_token.py --dry-run
```

Write local auth files inside the `ag_proxy` folder:

```bash
python3 extract_token.py
```

This creates:

- `token.txt`
- `refresh_token.txt`
- `oauth_client.json` if the installed Antigravity app exposes OAuth client credentials

By default the extractor reads:

- Linux: `~/.config/Antigravity/User/globalStorage/state.vscdb`
- Windows: `%APPDATA%\Antigravity\User\globalStorage\state.vscdb`
- macOS: `~/Library/Application Support/Antigravity/User/globalStorage/state.vscdb`

To see all paths the tool checks:

```bash
python3 extract_token.py --print-paths
python3 extract_token.py --print-app-paths
```

Custom locations:

```bash
python3 extract_token.py --db /path/to/state.vscdb --out-dir /path/to/output
```

PowerShell example on Windows:

```powershell
python extract_token.py --db "$env:APPDATA\Antigravity\User\globalStorage\state.vscdb" --dry-run
python extract_token.py
```

The extractor only reads local Antigravity files. It does not send tokens
anywhere. Refresh tokens are appended uniquely by default.

## Run Proxy

From inside the shared folder:

```bash
cd ag_proxy
PORT=5005 python3 run_proxy.py
```

Useful environment variables:

```bash
AG_ANTIGRAVITY_VERSION=1.107.0
AG_DEFAULT_MODEL=gemini-3-flash
AG_PROXY_LOG_PAYLOADS=0
```

## Logging

The server keeps Uvicorn's default access logs and adds one compact model usage
line for each `/v1/messages` request:

```text
INFO:     127.0.0.1:46994 - "POST /v1/messages HTTP/1.1" 200 OK
MODEL model=gemini-3-flash requested=gemini-3-flash stream=false input_tokens=4 output_tokens=2 stop_reason=end_turn
```

Errors are printed as `ERROR ...` lines. Prompt text, tool results, and tokens
are not logged by default. For local debugging only, set:

```bash
AG_PROXY_LOG_PAYLOADS=1 python3 run_proxy.py
```

## Use With Claude Code

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:5005
export ANTHROPIC_AUTH_TOKEN=local-anything
claude
```

The incoming Anthropic token is ignored. The proxy uses `token.txt` and
`refresh_token.txt` from this `ag_proxy` folder unless `AG_TOKEN_FILE` or
`AG_REFRESH_TOKEN_FILE` is set.

## API Endpoints

- `GET /health`
- `GET /v1/models`
- `GET /v1/models/{model_id}`
- `POST /v1/messages/count_tokens`
- `POST /v1/messages`

`POST /v1/messages` supports both non-streaming JSON responses and Anthropic SSE
streaming responses. `GET /models` is also accepted as a convenience alias, but
`GET /v1/models` is the Anthropic-compatible path.

Example:

```bash
curl http://127.0.0.1:5005/v1/models \
  -H "anthropic-version: 2023-06-01" \
  -H "x-api-key: local-anything"
```

## Sharing Notes

Share the `ag_proxy/` folder only. Do not share `token.txt`,
`refresh_token.txt`, captured traffic, Burp files, logs, or `__pycache__/`.
