## Octavius

Self-hosted voice assistant with a FastAPI backend, browser client, MCP-integrated tools, a knowledge inbox, and a document reader.

## Runtime Configuration

Runtime settings are environment-backed through [`settings.py`](/home/dave/git_repos/octavius-refactor/settings.py). Copy values from [`.env.example`](/home/dave/git_repos/octavius-refactor/.env.example) into your environment or service manager.

Key groups:
- STT, TTS, and LLM endpoints
- reader storage and reader LLM settings
- download and reader directories
- summary and embedding service endpoints

## Development

Run the app:

```bash
uv run python main.py
```

Run the tests:

```bash
python -m unittest discover -s tests
```
