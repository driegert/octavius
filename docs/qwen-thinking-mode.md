# Qwen3.6 Thinking vs. Non-Thinking Mode

Reference notes for deciding whether (and where) to run the Qwen3.6 models
with reasoning enabled. Not yet implemented — this captures the research so
we can act on it later without re-deriving it.

## Why this matters

Octavius is a voice assistant. The `<think>...</think>` block the Qwen3.6
models emit before their visible answer adds latency to every turn. For a
spoken interface that latency is felt directly, and it is not obvious the
reasoning quality gain is worth it for the kinds of tasks Octavius runs.

The codebase already disables thinking in one place: `history_enrichment.py`
(summary/tag generation) sends `chat_template_kwargs={"enable_thinking": False}`
plus a `/no_think` prompt prefix. That is the proven pattern to copy.

## Qwen3.6 is hybrid-reasoning

Thinking is meant to be toggled per-request, not baked into the model. The
toggle is the `enable_thinking` chat-template kwarg.

Three ways to set it:

- **Per-request API** (preferred here): include
  `"chat_template_kwargs": {"enable_thinking": false}` in the request body.
  Confirmed working on our stack via `history_enrichment.py`.
- **Server-side llama.cpp**: launch with
  `--chat-template-kwargs '{"enable_thinking":false}'`. Sets a global default
  for that server — bad fit for us because `lilripper:8010` is a shared
  llama-swap host (the reader's `qwen3.5-9b` rides the same port).
- **Transformers**: `apply_chat_template(..., enable_thinking=False)` — not a
  path Octavius uses.

`true`/`false` can be passed interchangeably as strings or bools.

## Sampling parameters differ by mode

This is the important catch: Qwen's guidance gives **different sampler
profiles** for thinking vs. non-thinking. Disabling thinking without also
adjusting samplers leaves the model running with mismatched settings.

| Mode                       | Temp | Top-p | Top-k | Min-p | Presence penalty |
|----------------------------|------|-------|-------|-------|------------------|
| Thinking (general)         | 1.0  | 0.95  | 20    | 0.0   | 1.5              |
| Thinking (precise coding)  | 0.6  | 0.95  | 20    | 0.0   | 0.0              |
| **Non-thinking (general)** | 0.7  | 0.8   | 20    | 0.0   | 1.5              |
| Non-thinking (reasoning)   | 1.0  | 0.95  | 20    | 0.0   | 1.5              |

The biggest delta between thinking and non-thinking *general* use is
**top-p: 0.95 → 0.8**.

## Current server config (as of 2026-05-14)

The `lilripper:8010` llama.cpp launch preset for `qwen3.6-35b-a3b`:

```
--jinja --min-p 0 --reasoning-format deepseek
--temperature 0.65 --top-k 20 --top-p 0.95 --ctx-size 262144
```

That is a **thinking-mode** profile (close to the "thinking, precise coding"
preset). If we send `enable_thinking: false` per-request but leave these
server defaults, we run non-thinking mode with thinking-tuned samplers —
works, but not what Qwen recommends. The fix is to send the matching
`temperature`/`top_p` in the same payload that disables thinking.

## Where thinking helps (and where it doesn't)

The value of thinking tracks two things: how multi-step the tool work is,
and how much the path can hide latency.

- **Web search (main agent)** — low benefit. Shallow loop: form a query,
  call searxng, summarize. It is also the streaming, live, latency-critical
  path. Recommendation: thinking **off**.
- **Email / task search (subagent)** — better case for keeping it on. The
  subagent does genuine multi-step tool work (choosing between
  `search_emails` / `semantic_search` / `list_conversations`, constructing
  args, judging "do I have enough?"). It is also backgrounded behind the
  "Agents at Work" badge, so its latency is partially hidden.
  Recommendation: **A/B test** before deciding.

## Proposed implementation (not yet done)

Make `enable_thinking` plus its sampler pair a **per-chain config block** so
main agent and subagent can differ without code changes:

```
{ "enable_thinking": false, "temperature": 0.7, "top_p": 0.8 }
```

- Main agent chain → default to the non-thinking general profile.
- Subagent chain → start configurable; A/B test thinking on vs. off on real
  email/task queries to see if tool selection degrades without it.
- Keep the existing `THINK_RE` stripping and the streaming `</think>`
  handling in `agent.py` — they become harmless no-ops when no think block
  is emitted, and stay as a safety net.

## Sources

- Unsloth — Qwen3.6 How to Run Locally: <https://unsloth.ai/docs/models/qwen3.6>
- vLLM Recipes — Qwen3.5 & Qwen3.6 Usage Guide:
  <https://docs.vllm.ai/projects/recipes/en/latest/Qwen/Qwen3.5.html>
- QwenLM/Qwen3 Discussion #1300 — disabling thinking in vLLM deploy:
  <https://github.com/QwenLM/Qwen3/discussions/1300>
- unsloth/Qwen3.5-9B-GGUF — enabling/disabling reasoning:
  <https://huggingface.co/unsloth/Qwen3.5-9B-GGUF/discussions/2>
