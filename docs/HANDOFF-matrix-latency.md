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
  (much slower, cold cache) after 502s. That is most of the p90. **These stopped after
  2026-07-26** — see the 502 section below; the p90 above is a July-wide figure and should
  be re-measured before it is used to justify more work.

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

**Sidecar** (`520ef60`, `45e82c4`, `40827df`):
- Progress is a **typing indicator**, held open for the turn (refresh 3.5 s: the SDK suppresses
  resends within 3 s, the server expires at 4 s). Nothing is left in the timeline.
- The streamed-`m.notice` version (`520ef60`/`45e82c4`) was **reverted in `40827df`**: it redacted
  the notice after posting the answer, but Matrix has no silent delete, so every turn left a
  "Message deleted" tombstone — in the *main* timeline, because redaction strips `m.relates_to`
  and the tombstone falls out of its thread. Don't reintroduce a redact-based cleanup.
- Final answer posts as its **own** `m.text` message. Mobile push is built from the original
  event, so delivering it as an edit would notify `⋯ Thinking...` forever — and Dave reads these
  on mobile.
- Sends `{"type":"settings","tts":false}` on connect. **Every Matrix reply up to 2026-07-30 was
  synthesised to speech and discarded** (all 133 assistant messages since Jul 1 carry a
  `tts_model`) — ~1.2 s per three-sentence reply, awaited *between* sentences, which would have
  throttled the new deltas.

## Not done

1. ~~Install the sidecar binary~~ — **done 2026-07-31 09:43**, service active, session restored,
   E2EE ready. (The two `rustls_platform_verifier` CA-cert permission warnings on startup are
   pre-existing, present since well before this work.)

2. ~~Chase the `:8020` 502s~~ — **closed 2026-07-31, see below.**

3. ~~Truly progressive text~~ — **moot for Matrix as of 2026-07-31.** Rendering progressive text
   requires a real Matrix message, and every real message is either permanent clutter or (if
   cleaned up) a tombstone. Progress is a typing indicator only.

   Octavius still emits `response_delta` and the sidecar still drains it without rendering, so the
   plumbing survives for any client that *can* render in place (the PWA). If that's ever picked
   up: `agent.py` accumulates `buffered_sentences` and only yields them after a round completes
   without tool calls, because some Qwen builds emit tool calls as XML inside `content`
   (`agent.py:321`) — so deltas all fire at the end. Making them progressive means emitting
   optimistically and using the `TurnEvent::Restart` path to discard on tool-call rounds. Risk:
   briefly flashing tool-call XML.

4. **Keep-warm ping** for the 2.77 s cold-start hit, and cross-host failover for the subagent
   chain (see `status.md` Near-Term Work #4 — both tiers are still on `lilripper`).

## The `:8020` 502s — closed 2026-07-31

**They stopped on their own.** Last one 2026-07-26 23:20; 103 requests to `:8020` since,
zero failures of any kind. The journal retains back to 2026-06-26, so this is a real gap,
not a retention artifact, and traffic genuinely continued (5/42/29/24/3 requests Jul 27–31).

**Root cause was the service behind `:8020` being restarted or reconfigured on lilripper —
not octavius, and not request handling.** Three different error shapes, all "upstream is
changing underneath us", each appearing on different days:

| Error | Body | Who emits it |
|---|---|---|
| 503 | `{"error":{"message":"Loading model","type":"unavailable_error"}}` | llama.cpp, reloading |
| 404 | `{"error":{"message":"The model ... does not exist.","type":"NotFoundError"}}` | a **router**, not today's llama.cpp |
| 502 | *empty* | Caddy (`Via: 1.1 Caddy`), upstream unreachable |

The 404 is the tell: today `:8020` is bare llama.cpp `b9282`, which **ignores the `model`
field entirely** — a deliberately bogus model name returns 200 and is served by
`qwen3.6-35b-a3b`. So whatever returned `NotFoundError` on Jul 20 was a different process
listening on that port. The failures are bursty (11 bursts, 2–11 errors over 19–225 s),
which is the shape of one swap/restart window, and on Jul 23–26 *100%* of `:8020` requests
failed — the endpoint was down, not overloaded.

**The `--parallel 1` client-abort hypothesis is refuted.** Tested directly: three streams
opened and abandoned after one chunk, each followed by four probes — 12/12 returned HTTP 200
in ~0.12 s. llama.cpp handles client disconnect cleanly. The 502s seen during the original
timing run were the tail of the Jul 26 window, not something the test client caused.

**What was actually fixable, and was fixed** (`3c4c520`): all four chain call sites used a
flat `httpx.AsyncClient(timeout=120.0)`, which covers *connect* as well as read. A host that
refuses or 502s fails over instantly; a host that swallows SYNs (mid-reboot) held the chain
for the full 120 s per entry. Now `httpx.Timeout(120.0, connect=5.0)` — read stays long
because generation legitimately runs for minutes. Verified against a blackholed primary:
failover in **5.8 s** instead of 120 s+.

Scale, honestly: only 4 turns since Jul 1 exceeded 100 s (max 142 s), so this was worth a
few lines, not a project. If 502s return, the question is "what restarted on lilripper",
not "what is octavius doing wrong" — check `/props` `build_info` and whether `/v1/models`
still reports a single bare-llama.cpp model.

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
