# Model routing and the LLM fleet

Read this before touching `models.json`, adding or moving an LLM endpoint, changing generation params, or when a role is slow, empty or silently failing over.

## `models.json`

Every LLM role — `main`, `subagent`, `vision`, `reader`, `summary` — is routed from `~/.config/octavius/models.json` (`OCTAVIUS_MODELS_FILE` to relocate), shaped `{"roles": {"<role>": [entries]}}`. It is the only file the app loads and holds no secrets.

- Precedence: **code defaults < `models.json` < environment** (`OCTAVIUS_LLM_CHAIN`, `OCTAVIUS_SUBAGENT_LLM_CHAIN`, `OCTAVIUS_VISION_LLM_CHAIN`, …). A role absent from the file keeps its code default. The code defaults in `settings.py` and `.env.example` match the live file (updated 2026-10-30), so a missing file routes the same; `scripts/octavius-models show` says which file loaded.
- Malformed JSON **raises**, and so does a chain role (`main`, `subagent`, `vision`) with an empty list or an entry lacking `url` — restarting onto routing nobody asked for is the failure the file exists to prevent. The single-endpoint roles (`reader`, `summary`, via `_role_single`) are looser: a missing or empty field or entry falls back to the code default for that field.
- Entry: `{url, model, params?}`, plus `role`/`capacity` for subagent entries. **`params` merges into the request body** — the only way to reach knobs the agent path never builds:
  ```json
  { "url": "http://lilripper:8010/v1/chat/completions",
    "model": "qwen3.6-35b-a3b-mtp-general",
    "params": { "chat_template_kwargs": { "enable_thinking": false } } }
  ```
  The caller's payload wins on conflict; `model` is always the endpoint's. A non-dict `params` logs a warning and is ignored (bad routing must degrade, never take a voice turn down).
- `scripts/octavius-models show | check | apply`. `check` probes every endpoint (reachable? alias present? authed?) and exits nonzero on any failure; `apply` = check, then daemon-reload + restart + confirm `/health`. It exists because a vanished alias (hard 400) and a rotated token (401) both look like ordinary failovers. Backup before the 2026-10-31 edit: `~/.config/octavius/models.json.bak-2026-10-31-pre-workhorse`.

## Current routing (as of 2026-10-31, verified against the live file 2026-11-24)

| role | primary | fallback(s) |
|---|---|---|
| main | `lilbuddy:8010` `gemma4-26b-a4b` | `lilripper:8020` `xray` → `workhorse:8010` `gamma` |
| subagent | `workhorse:8010` `gamma`, `capacity: 3` | `lilripper:8020` `xray`, `capacity: 2` |
| vision | `lilbuddy:8010` `gemma4-26b-a4b` | `lilripper:8020` `xray` |
| reader | `workhorse:8010` `gamma` | `lilripper:8020` `xray` |
| summary | `workhorse:8010` `gamma` | `lilripper:8020` `xray` |

All URLs are `/v1/chat/completions`. Why it looks like this:

- **2026-10-28:** `lilripper:8010` now serves only `glm-5.3-flash`, so Octavius no longer uses it. Interactive roles went to lilbuddy's 26B; non-interactive to the 12B; `:8020` became every role's fallback.
- **2026-10-31:** triplestuffed stopped serving `gemma4-12b` (its router 502'd and four roles were silently failing over), so they moved to workhorse.
- **2026-11-26:** the three primary models got role names, which the servers answer to alongside the old ids: `gamma` = Gemma4-12B, `xray` = Inkling2-mini, `uv` = GLM-5.3-Flash. Config and pi model ids use the new names. `:8020` switched from Qwen3.8-27B to Inkling2-mini at the same time; it still answers to `qwen3.8-27b`, but that name now serves Inkling (commit `12e47fe`'s message says xray = Qwen3.8-27B, which is wrong).
- **`:8020` is never a primary in `models.json`**: Dave loads other models onto it by hand, and as a primary every turn would evict them (demoted 2026-08-18). Exception (2026-11-26): pi's primary is `lrv-p01/xray`, set in `~/.config/octavius/env` (see [pi-brain.md](pi-brain.md)). Same reason summaries are workhorse-first: they fire at conversation end, long after Dave moved on.
- **lilbuddy's two slots** (`--parallel 2`) are shared by the main and vision primaries — fine for one user; a voice turn plus two concurrent image threads queue.
- **workhorse** (vLLM, open, 64K ctx, text-only) is shared with pi_harness's research agents (6 concurrent requests measured, none queued), which is why subagent `capacity` is 3. Main hop 3 there means voice survives lilbuddy and lilripper both being down.
- **Remaining hole (deferred by Dave):** lilripper + workhorse down strands consults, reader and summaries; lilbuddy + lilripper down strands image turns.

Role specifics:

- **Subagent dispatcher** (`subagent_dispatcher.py`) routes by `role`: `primary` (first try / concurrency, with optional `secondary` as concurrency overflow only) and `fallback` (the single per-call HTTP failover target). **It keeps only the first `fallback`** (`elif ep.role == "fallback" and fallback is None`), so a second one is dead config. Cross-host resilience therefore goes in `fallback`, not `secondary`. Capacity on vLLM is a guess — its concurrency is not readable from `/v1/models`.
- **Model is per endpoint, not per domain**: `subagent.py::_model_for_url` resolves it from the chain entry matching the assigned URL. Per-domain models would need a `model` key on `SUBAGENT_DOMAINS`. Only email/research/tasks consults use this chain; `web_search`/`read_url` are main-agent tools.
- **Reader**: single endpoint per call, but its second entry is the failover target (`ReaderSettings.llm_fallback_url/model`, read via `_role_single(..., index=1)`; a half-configured entry raises). `reader_text._llm_convert_math` tries each before degrading to local `strip_latex`. Sends `enable_thinking: false` (with thinking on, the 12B spent all 8192 `max_tokens` reasoning and returned null after ~53 s). A models.json listing a single `reader` entry means "no fallback"; the default fallback applies only when the file omits `reader`. **Trap:** `LLMChainClient.complete()` merges per-URL `params` from the **main** chain, and both reader URLs are also main-chain hops, so `params` set there silently apply to reader calls (none today).
- **Summary/tags**: `SummaryClient` is not an `LLMChainClient` but carries a model **per URL** (`summary_model` / `summary_fallback_model`; one model for both URLs was a guaranteed 400 once routers stopped sharing aliases). Payloads send `enable_thinking: false` with `max_tokens` 1024 (summary) / 768 (tags). A failed summary is invisible: history just ends up unsummarised and untagged. **Only the summary primary comes from `models.json`**: the fallback is `OCTAVIUS_SUMMARY_FALLBACK_URL` / `OCTAVIUS_SUMMARY_FALLBACK_MODEL` (env or code default), so a second `summary` entry in the file is ignored.
- **Vision** stays a separate chain (`vision_llm_client`, `agent.py`'s `use_vision`) because the main chain's third hop is chosen for availability, not modality: separate chains stop a future "add another fallback" edit from widening where an image turn can land. Never add workhorse's text-only 12B here.

## Endpoint catalogs (re-curled 2026-10-28; they churn — re-curl before trusting)

`complete_with_tools` uses each *entry's* model (payload model ignored) and fails over on any 4xx/5xx, so an alias missing from an endpoint hard-400s and silently burns a hop. Dave rebuilds catalogs; after any router work, a config naming a missing alias is the first suspect (the cause on 2026-08-26 and 2026-10-28).

- `lilripper:8010` (llama.cpp, auth): `uv` only, text-only. Unused by Octavius.
- `lilripper:8020` (**vLLM**, auth): `xray` = `Inkling-2-Mini-W4A16-g128-gptq` (a looping MoE; also answers to `inkling-2-mini` and `qwen3.8-27b`), `max_model_len` 267520, takes images (checked 2026-11-27), fast enough to be pi's primary.
- `workhorse:8010` (**vLLM**, open): `gamma`, `max_model_len` 65536, text-only.
- `triplestuffed:8010`: no longer serves `gemma4-12b`; 502 on 2026-10-31. Unused. (Also pulled once on 2026-08-08 as a zombie: listed fine, never generated, so failover burned the full 120 s read timeout.)
- `lilbuddy:8010` (llama.cpp router, open, 12 aliases, all `--parallel 2`): loaded `gemma4-26b-a4b` (295k ctx, `--ubatch-size 2048`, text+image+video) and `nemotron-3.5-super` (307k ctx, text+image+video); unloaded `DEFAULT`, `agents-a1-4b`, `gemma4-12b` (text+image+audio — a different build from workhorse's), `muse-glimmer-30b`, `neohorse-1-9b`, `ornith-1.5-9b`, `qwen3.5-4b`, `qwen3.5-9b`, `qwen3.6-35b-a3b-mtp-q4-{code,general}`.
- **Modalities churn too**: read `architecture.input_modalities` before assuming a hop takes images, in either direction.
- **Keep each endpoint's consumers on one alias**: two roles naming different models on one router thrash it between resident models. Today workhorse's four consumers all name `gamma`; lilbuddy's two name `gemma4-26b-a4b`.

### Inspecting a llama.cpp router

`/v1/models` on llama.cpp routers returns each model's **full launch argv** under `status.args`, `status.value` (`loaded`/`unloaded`) and `architecture.input_modalities`, so `--parallel`, `--ctx-size`, `--ubatch-size` and image support are readable without asking Dave. Don't stop at `data[].id` (that cost a wrong answer on 2026-08-13):

```bash
# keyed routers (lilripper) also need: -H "Authorization: Bearer $OCTAVIUS_LR_API_KEY"
curl -s http://lilbuddy:8010/v1/models \
| python3 -c "
import json,sys
for m in json.load(sys.stdin)['data']:
    a = (m.get('status') or {}).get('args') or []
    g = lambda f: a[a.index(f)+1] if f in a else '?'
    mods = ','.join((m.get('architecture') or {}).get('input_modalities') or ['?'])
    print(f\"{m['id']:34s} {(m.get('status') or {}).get('value','?'):9s} par={g('--parallel'):>2s} ctx={g('--ctx-size'):>7s} ub={g('--ubatch-size'):>5s} in={mods}\")"
```

vLLM endpoints (`lilripper:8020`, `workhorse:8010`) have no `status.args` (they report `max_model_len`), so the recipe doesn't apply. Neither tells you whether a model actually *generates* — send a real completion.

## Thinking, latency and `max_tokens`

- **Every role runs thinking on** (Qwen3 template default, no `params` needed) except where noted (reader, summary). Measured 2026-08-26 on a Qwen 35B: 2.25 s to first content token with thinking, **0.22 s** with `enable_thinking: false`, tool selection unchanged. It ran on the main primary for two days and was **reverted 2026-08-28**: Dave did not trust the answers. End-to-end cost: first audio ~2.2-4.0 s with thinking vs ~1.5-1.8 s without. If traded back, prefer `reasoning_effort: "low"`/`"medium"` (a dial) over the switch.
- **gemma4 runs uncapped.** `reasoning_effort: "low"` on the 26B was tried and removed 2026-09-10: warm, trivial prompts take ~0.2-0.3 s either way; a paragraph question ~4.6-5.7 s uncapped (~3500-4300 reasoning chars) vs ~3.6-3.9 s capped. A quarter to a third, not an order of magnitude, and it shortened answers. gemma4 is fast because it is an A4B.
- **gemma4 returns reasoning in a separate `reasoning_content` field**, not inline `<think>` tags, so the stripper never sees it and `agent.py`'s `delta.get("content", "")` drops it for free.
- **`max_tokens` on a thinking model must budget for reasoning**, or the model spends it all thinking and returns `finish_reason: length` with **empty** `content`.

## Host rules

- **Vision hops: check modality *and* micro-batch.** A gemma4v image encodes to 70-1120 tokens and llama.cpp decodes the whole image chunk as one non-causal ubatch, so a hop at the default `-ub 512` aborts (`GGML_ASSERT ... n_ubatch >= n_tokens`) on any image bigger than a thumbnail; the router reloads and the turn comes back empty ("I'm not sure how to respond to that."). Found 2026-09-14; fixed then with `ubatch-size = 1152` in a router `preset.ini`. Today's lilbuddy primary runs `--ubatch-size 2048`. Read `--ubatch-size` from `status.args` before pointing image turns at any llama.cpp hop.
- **lilbuddy OOM cascade.** An OOM there takes everything on the box down: on 2026-08-18 loading a dense 35B gave 502 on every alias and port for ~2 min, because the router ran `max_models=3` with generous `--ctx-size` and exhausted the 128 GB unified memory (now `max_models=2`; Dave re-sized what lilbuddy serves for the 2026-10-28 move). Size any new lilbuddy alias against the **whole box's** residency budget — its `:8020` bge-m3 embedder serves search.
- After any router work, re-probe the affected ports: an alias is a distributed config dependency with no compile-time check.
