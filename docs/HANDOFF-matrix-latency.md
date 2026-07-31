# HANDOFF — Matrix turn latency (streaming + subagent routing)

_Last updated 2026-07-31. Spans `octavius` + `matrix-agent-sidecar`, both on branch
`matrix-streaming`. Resume point: chase the `lilripper:8020` 502s._

## Why this work exists

Measured from `messages.latency_ms` (July, live DB at
`/media/extra_stuff/octavius/octavius_history.db`), first assistant reply per conversation:

| Channel | median | p90 |
|---|---|---|
| Matrix | **17.2 s** | 65.8 s |
| voice  | 8.1 s | 28.0 s |

Where it goes, same window:

- **`consult_specialist` dominates**: 53 calls since Jul 1, **14.9 s avg, 50.6 s max**, 792 s
  total — more than every other tool combined. `search_papers` is second (23.2 s avg, n=3).
- Median tool time on those first turns is only **2.7 s**, so the typical 17 s is LLM
  turnaround across tool rounds, not the tools. The 60–90 s tail is where consults live.
- **74 turns in 14 days silently fell back** off `lilripper:8020` to `lilbuddy:8010`
  (much slower, cold cache) after 502s. That is most of the p90.

**Prefix-cache invalidation is NOT the problem** — measured on `:8020` with the real system
prompt + tool list: cold 2.77 s TTFT, warm identical 0.06 s, same system block with a new
question 0.29 s, **timestamp changed by one minute 0.29 s**. llama.cpp re-prefills only from
the first changed token. Don't re-litigate this.

## What shipped

**Octavius** (`3d53382`, deployed 2026-07-30 — user service restarted, `/health` ready, 8/8 MCP):
- `websocket_session.run_turn` emits a `response_delta` frame per sentence before the final
  authoritative `response`. Browser clients ignore unknown frame types.
- `settings.subagent_llm_chain` primary → `lilripper:8010` / `qwen3.6-35b-a3b-general`,
  `capacity: 3` (matches its `--parallel 3`); `:8020` demoted to HTTP-level fallback.
  `consult_specialist` reserves a dispatcher ticket, so the old `capacity: 1` serialised
  consults against each other *and* against the main agent's own turn on single-slot `:8020`.

**Sidecar** (`520ef60`, `45e82c4` — **built, NOT installed**, see below):
- Posts an `m.notice` placeholder into the thread on first progress, edits it in place
  (`m.replace`, throttled to 1.5 s) with tool statuses then answer sentences.
- Final answer posts as its **own** `m.text` message, then the notice is redacted. Mobile push
  is built from the original event, so an edit would notify `⋯ Thinking...` forever — and Dave
  reads these on mobile. Notices are push-suppressed, so streaming never buzzes the phone.
- Sends `{"type":"settings","tts":false}` on connect. **Every Matrix reply up to 2026-07-30 was
  synthesised to speech and discarded** (all 133 assistant messages since Jul 1 carry a
  `tts_model`) — ~1.2 s per three-sentence reply, awaited *between* sentences, which would have
  throttled the new deltas.

## Not done

1. **Install the sidecar binary** (needs sudo; sudo on triplestuffed requires a password, so
   Dave runs these):
   ```
   sudo install -m755 ~/school_lab/git_repositories/matrix-agent-sidecar/target/release/matrix-agent-sidecar /usr/local/bin/octavius-matrix-sidecar
   sudo systemctl restart octavius-matrix-sidecar
   ```
   Until then the old binary runs; it ignores `response_delta` via its catch-all arm, so the
   two halves are compatible in either order.

2. **Chase the `:8020` 502s** (next task). Lead: they reproduced live when a client *aborted*
   a stream early (`max_tokens=1`, returning before draining) — three requests in a row 502'd,
   then behaved once streams were fully drained. Fits a `--parallel 1` server wedging briefly
   on client disconnect. Check whether anything sits in front of `:8020` (`/v1/models` returns
   a router-shaped payload with `models[]`; `/running` 404s, so it is not llama-swap).
   `service_clients` uses `httpx.AsyncClient(timeout=120.0)`, so a *hanging* primary — as
   opposed to a fast 502 — costs up to 120 s before failover.

3. **Truly progressive text (optional).** `agent.py` accumulates `buffered_sentences` and only
   yields them after a round completes without tool calls — necessary because some Qwen builds
   emit tool calls as XML inside `content` (`agent.py:321`). So `response_delta` frames all fire
   at the end; what streams today is the *progress line*, not the text. To make text progressive:
   emit sentences optimistically and use the existing `TurnEvent::Restart` path in the sidecar
   bridge to discard them when the round turns out to be a tool call. Risk: briefly flashing
   tool-call XML into the thread.

4. **Keep-warm ping** for the 2.77 s cold-start hit, and cross-host failover for the subagent
   chain (see `status.md` Near-Term Work #4 — both tiers are still on `lilripper`).

## Things worth not rediscovering

- **Thread isolation is sound.** Every Matrix conversation is keyed by thread root (83 of them
  in the live DB, all `$eventid`); `handle_attach_session` reloads only that key's rows, trimmed
  to the last 40 messages. Two legacy `main:<room_id>` rows from Jun 27–28 predate Model A.
  Cross-thread bleed is only *distilled*: memory-service profile (~1.4 k chars) every turn, plus
  on a thread's first message up to 2 summaries from `_episodic_recall` (any source, labelled
  non-authoritative).
- Per-turn memory work is **not** a latency factor: `fetch_injection` 0.03–0.06 s,
  `_episodic_recall` 0.03 s.
- First-turn payload is ~27.6 k chars ≈ 6.9 k tokens; **tool schemas are 72% of it** (16 local
  = 10,517 chars, 7 core MCP = 9,524).
- Octavius sessions default to `tts_enabled = True` — any new WS client must opt out.
- Tests: `python -m unittest discover -s tests` (372 green, incl. 2 new
  `response_delta` tests); sidecar `cargo test` (10 green). The `ExceptionGroup` traceback in
  the journal on octavius restart is pre-existing MCP-stdio teardown noise (78 since Jul 1).
