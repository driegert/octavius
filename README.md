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
- LLM endpoint auth (`OCTAVIUS_LR_API_KEY` for the lilripper ports, exported from `~/.config/secrets.env`; `OCTAVIUS_LLM_API_KEYS`, a JSON map of `scheme://host:port` → bearer token, for any other keyed endpoint)
- LLM model routing per role in `~/.config/octavius/models.json`
- reader storage and reader LLM settings
- download and reader directories
- summary and embedding service endpoints

Non-secret config belongs in `~/.config/octavius/env` (mode 0600, outside the repo); the
lilripper token lives only in `~/.config/secrets.env`. Never put secrets in the tree. See
`docs/runbooks/config-and-secrets.md`; `AGENTS.md` indexes the other runbooks.

## Development

Run the app:

```bash
set -a; . ~/.config/octavius/env; . ~/.config/secrets.env; set +a   # secrets only if a keyed endpoint is in the chain
OCTAVIUS_DB_PATH=/media/extra_stuff/octavius/octavius_history.db uv run python main.py
```

Stop the service first (it holds `:8030`). Without `OCTAVIUS_DB_PATH` the app opens the stale
repo copy of the history DB and its startup check refuses to run.

Run the tests:

```bash
python -m unittest discover -s tests
```
