## Octavius

Self-hosted voice assistant with a FastAPI backend, browser client, MCP-integrated tools, a knowledge inbox, and a document reader.

## Runtime Configuration

Runtime settings are environment-backed through [`settings.py`](settings.py), which reads
`os.environ` directly — **no `.env` file is loaded**. [`.env.example`](.env.example) is a
reference for variable names and defaults, not a file the app consumes. Config reaches the
app through the process environment: the systemd user unit pulls it from
`~/.config/octavius/env` via an `EnvironmentFile` drop-in.

Key groups:
- STT, TTS, and LLM endpoints
- LLM endpoint auth (`OCTAVIUS_LLM_API_KEYS`, a JSON map of `scheme://host:port` → bearer token)
- reader storage and reader LLM settings
- download and reader directories
- summary and embedding service endpoints

Secrets belong in `~/.config/octavius/env` (mode 0600, outside the repo), never in the
tree. See "Configuration and secrets" in `CLAUDE.md`.

## Development

Run the app:

```bash
set -a; . ~/.config/octavius/env; set +a   # only if a keyed endpoint is in the chain
uv run python main.py
```

Run the tests:

```bash
python -m unittest discover -s tests
```
