# Configuration, secrets and endpoint auth

Read this when changing env config, rotating the lilripper token, or when a chain looks flaky and `/health` shows auth or failover errors.

## Where config comes from

**Nothing loads a `.env` file.** `settings.py` reads `os.environ` directly; `.env.example` is a reference for names and defaults only. A key put in a `.env` is silently ignored.

- **Service**: `~/.config/systemd/user/octavius.service.d/env.conf` does two things:
  - `EnvironmentFile=-%h/.config/octavius/env` supplies **non-secret** config (mode 0600, outside the repo).
  - An `ExecStart=` override sources `~/.config/secrets.env` in a subshell and exports `OCTAVIUS_LR_API_KEY` (and pi's `PI_AGENT_LR_API_KEY`, see [pi-brain.md](pi-brain.md)) alone, so the token lives in exactly one file fleet-wide. The value never reaches argv, so it stays out of `ps`. Same pattern as `notesmd.service`.
  - Why a subshell, not a second `EnvironmentFile` (verified 2026-08-26): `secrets.env` uses `export FOO=...` lines, which systemd's parser does **not** understand — it logs `Ignoring invalid environment assignment` and sets *nothing*, so Octavius would start keyless and 401 every keyed hop silently. The subshell also keeps that file's other keys out of the service environment (only `OCTAVIUS_LR_API_KEY` and `PI_AGENT_LR_API_KEY` reach `/proc/<pid>/environ`).
  - After editing either file: `systemctl --user daemon-reload && systemctl --user restart octavius`.
- **Foreground**: stop the service first (it holds `:8030`), then `set -a; source ~/.config/octavius/env; source ~/.config/secrets.env; set +a; OCTAVIUS_DB_PATH=/media/extra_stuff/octavius/octavius_history.db uv run python main.py`. The unit drop-in sets `OCTAVIUS_DB_PATH` (with `RequiresMountsFor=/media/extra_stuff`); without it the app opens the stale repo copy and `assert_history_serveable` refuses to start.
- **By hand (e.g. `scripts/octavius-models check`)**: `set -a; source ~/.config/octavius/env; source ~/.config/secrets.env; set +a`. Every line of `secrets.env` must be an `export` line: until 2026-10-28 `OCTAVIUS_LR_API_KEY` was a bare assignment, so `source` set it in the shell but not in children, and `check` reported every lilripper entry as a 401. (The service was unaffected — its wrapper echoes the value out of a subshell.)
- **Unit environment** (e.g. `OCTAVIUS_DB_PATH`) is the authority for what the service actually runs with: `systemctl --user show octavius -p Environment`.
- Model routing has its own file, `~/.config/octavius/models.json` — see [model-routing.md](model-routing.md).

## LLM endpoint auth

Only the two lilripper ports (`:8010`, `:8020`) are behind auth. Two env vars feed `settings.llm_api_keys`, which `service_clients.auth_headers()` resolves **by URL** for every `LLMChainClient` request path (`stream_chat`, `complete`, `complete_with_tools`); `SummaryClient` attaches them too.

- **`OCTAVIUS_LR_API_KEY`** (preferred; `LR` = lilripper) — a bare token applied to **both** lilripper ports. It is the value that rotates, so it **wins** over the JSON map; the name is stable across rotations. A blank/whitespace value is ignored rather than sending `Bearer ` (tested).
  - Scoped to lilripper, *not* to a port: `lilbuddy:8010`, `workhorse:8010` and `triplestuffed:8010` also listen on 8010 and are open, so they must never receive the header. `settings.KEYED_LR_ORIGINS` pins this (tested).
  - Widened from `:8010`-only on 2026-08-26, when `:8020` went behind the same token: with the key pinned to `:8010`, `:8020` got no header and its 401 read as an ordinary failover.
- **`OCTAVIUS_LLM_API_KEYS`** — general mechanism: JSON mapping endpoint *origin* (`scheme://host:port`) to a token, e.g. `{"http://lilripper:8010":"sk-..."}`. In an `EnvironmentFile`, single-quote it: `OCTAVIUS_LLM_API_KEYS='{"http://lilripper:8010":"sk-..."}'`.

Endpoints absent from both get no `Authorization` header. Keys are per origin, not per chain entry, because one endpoint serves several chains. As of 2026-10-28 `lilripper:8020` is the **fallback of every role** and every primary is on an open host, so a stale key no longer breaks the first hop — it silently removes the safety net. Watch `/health`'s `endpoints_rejecting_credentials`.

**Why this matters (2026-08-26 incident):** a stale token in `OCTAVIUS_LLM_API_KEYS` and no `OCTAVIUS_LR_API_KEY` meant both lilripper hops 401'd and every turn landed on the third hop — 28 s to first word instead of 3 s — while consults, vision, reader and summaries (no third hop) failed outright. Nothing was "down"; only `/health` said so. Run `scripts/octavius-models check` before assuming an application bug.

## `/health` failure classification

A 401 still burns a failover hop (it is an `HTTPStatusError`), but it does not hide: `service_clients.classify_chain_error` buckets every chain failure, and a 401 logs at ERROR naming the origin and the env var to check.

`/health` → `llm_chain`: `endpoints_rejecting_credentials`, `auth_failures`, `last_failure_kind`, failover count, terminal failures, last successful endpoint, and per endpoint `last_error_kind` / `last_error_status` / `authenticated`. `last_error_*` clears on that endpoint's next success (non-null = "currently believed broken, this way"); counters are lifetime.

| kind | meaning |
|---|---|
| `auth` | key problem — check `endpoints_rejecting_credentials` first |
| `connect` / `connect_timeout` | host down (no TCP handshake) |
| `timeout` | "zombie": accepted the connection, never generated (`/v1/models` answers, completions hang) |
| `client_error` (usually 400/404) | model alias missing from that endpoint's catalog |
| `server_error`, `bad_response` | upstream 5xx / unparseable reply |

Empty content *with no failover* is usually a thinking model spending a small `max_tokens`, not auth (see [model-routing.md](model-routing.md)).

`/health` also reports `alive`, `ready`, `degraded`, per-server `mcp.servers`, `embedding_chain`, and `pi_brain`. `degraded = mcp_degraded or llm terminal_failures > 0` (`routes/system.py`), and `terminal_failures` is a lifetime counter, so one all-endpoints-failed request latches `degraded` until restart. Not changed yet because something might alert on it.
