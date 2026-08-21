# Octavius Status

This document holds change-oriented project status that is useful in the short to medium term:

- refactor progress
- current hotspots
- recent bug fixes that should not regress
- near-term design or implementation pressure

Keep durable architecture and contributor workflow in `CLAUDE.md`.

## START HERE (2026-08-13)

Everything below this section is history. This is the live picture.

**State.** `main` is current at `eb196b0`; nothing unmerged, tree clean, 448 tests
passing, service healthy (`ok`, MCP 8/8). Both lilbuddy and lilripper are up.

**The embedding-latency work is done and now VERIFIED IN PRODUCTION**, not just by
tests. 14 real voice turns on 2026-08-13 morning, measuring VAD end-of-speech → LLM
response complete:

| | |
|---|---|
| median | **0.29 s** |
| min / max | 0.11 s / 9.82 s |

That gap was **~10 s on every turn** before. The 9.82 s outlier is turn 1 only — a cold
model load on `:8020`, not the embed path; turns 2-14 all fall between 0.11 s and 0.61 s.
The mechanism is visible in the journal: at `07:50:08.210` VAD ends, `.241` the detached
embed returns (31 ms, concurrent), `.344` the LLM returns, `.897` TTS is speaking. Both
halves of the original fault are gone, embed backlog is 0/0, and conversation 1770
summarised → tagged → indexed → pushed cleanly.

**Where the time goes now** (this is the useful part for whoever picks this up):

1. **`consult_specialist` is the latency frontier.** Simple turns reach speech in
   0.7-1.4 s. Tool turns ran 8-51 s to first audio on 2026-08-13, entirely tool rounds
   (5-6 rounds per consult against `:8010`), matching the "~15 s average, 50 s worst"
   already in CLAUDE.md. Embeddings are no longer anywhere in this picture. Dave raised
   `:8020` to `--parallel 3` on 2026-08-13, which is the most likely lever.
2. **`end_async` costs ~8.8 s on the WS control path**, now measured rather than
   theorised. Conversation 1770's last turn ended `07:50:08`; summary landed
   `07:50:13.5` (+5.2 s), tags `07:50:14.7` (+1.1 s), memory push `07:50:17.0` (+2.4 s),
   session closed `07:50:17.02`. Four blocking remote calls in a row on "New Chat".
   **Same shape as the embedding bug — none of it needs to be on the turn path.** This is
   the obvious next piece of work.

**Open items, in the order they're worth doing** (details live in the task list, which
carries full runbooks — read the task before starting):

- **Task #8 — cross-host vision failover.** Model DECIDED with Dave:
  `qwen3.6-35b-a3b` on `lilbuddy:8010` (already loaded there, ctx 153600, takes images,
  already the main chain's 3rd hop on that host so no extra model swap). Ready to build.
  Today one lilripper outage means no image turns at all. Probe with a real image first —
  `input_modalities` is not proof a model generates.
- **Task #6 — `OCTAVIUS_LR_API_KEY` (renamed from `OCTAVIUS_8010_API_KEY` 2026-08-21). Needs Dave, nobody else can do it.** His shell
  exports a *different* token than `~/.config/octavius/env` holds. Both authenticate
  today, so nothing is broken; the risk is a silent 401 when one is revoked.
- **`end_async` detach** — see item 2 above.
- **TTS is still a single point of failure.** Kokoro lives only on lilbuddy; the four
  days it was unreachable Octavius was mute everywhere. A Kokoro on triplestuffed is the
  fix. Note TTS may still be toggled OFF in Dave's UI settings from that outage.
- Deferred, unchanged: `tools.py` dispatches sync local tools on the event loop;
  `/health` latches `degraded` until restart (`terminal_failures` is a lifetime counter);
  the subagent chain has the same single-host problem vision does, and a `secondary`
  entry won't fix it — cross-host resilience needs the `fallback` slot.

**Two habits this week earned.** (a) Re-probe both lilripper ports after any lilripper
work — a model alias is a distributed config dependency with no compile-time check, and
`LLMChainClient` treats the resulting 400 as an ordinary failure to fail past, so it
degrades quietly. (b) Read the *whole* `/v1/models` payload, not just `data[].id` — it
carries each model's launch argv (`--parallel`, `--ctx-size`), load state, and
`input_modalities`. Recipe is in CLAUDE.md's Runbook. Assuming it didn't cost a wrong
answer on 2026-08-13.

## lilbuddy's whole-box outage root-caused: `max_models=3` (2026-08-21)

Closes the open item from the 2026-08-18 section below.

**Symptom (2026-08-18).** Repointing the main chain's third hop at
`qwen3.6-35b-a3b-mtp-q4-general` and asking lilbuddy to load it gave 25 s of
nothing, then **502 on every port on the box at once** — `:8010` (llama.cpp
router), `:8020` (bge-m3 embeddings), `:8880` (Kokoro TTS). Caddy stayed up, so
it read as 502 rather than connection-refused. Everything recovered on its own
~2 min later with the router holding no model.

**Root cause (Dave, 2026-08-21).** The router was configured `max_models=3` with
over-generous per-model `--ctx-size`, so it kept up to three models resident
simultaneously. The dense 35B landing on top of two incumbents exhausted the
128 GB of unified memory; the OOM killer took the neighbouring services with it,
which is why three unrelated processes died together. **Now `max_models=2`.**

**The Octavius-side lesson is bigger than the third hop.** `:8020` and `:8880`
are separate processes from the router, but both are on the **live** turn path:

| lilbuddy port | Octavius role | fallback if it dies |
|---|---|---|
| `:8010` | main LLM chain, **third** hop | n/a — it *is* the last resort |
| `:8020` | embedding chain **primary** | `workhorse:11434` (breaker demotes after 2 failures) |
| `:8880` | Kokoro TTS — **the live default** | **none** |

So memory pressure created by a rarely-used *LLM fallback* silently breaks every
turn's audio. That last row is the residual risk: `TTSSettings.voxtral_enabled`
is `False`, so `service_clients.py:931` disables the Voxtral primary and every
synth call goes to the Kokoro fallback on lilbuddy. **TTS is single-homed with no
cross-host failover.** Not changing it here — Voxtral was disabled for output-level
reasons, not availability ones — but it is the one dependency the embedding chain's
breaker pattern has no analogue for.

Rule going forward: size a lilbuddy hop against the *box's* residency budget, not
against the model in isolation.

**Verified after the fix (2026-08-21), all three ports:**

| probe | result |
|---|---|
| `:8010` `/v1/models` | 200, 10 aliases, all `unloaded` |
| `:8020` embeddings | 200 in 0.014 s |
| `:8880` Kokoro | 200 in 1.40 s, 67 KB wav |
| `gemma4-26b-a4b` cold load + reply | **9.6 s** (was 16 s on 08-18) |
| warm short reply | **0.44 s** |
| warm tool call | **1.02 s**, `finish_reason: tool_calls`, correct shape |

`/health`: `alive`/`ready` true, `degraded` false, MCP 8/8, both embedding
endpoints untripped with zero consecutive failures, no `last_error_kind` on any
LLM hop. The cold-load improvement (16 s → 9.6 s) is presumably the trimmed
context budgets.

Deliberately **not** re-run: loading a dense 35B on lilbuddy. That is the only
test that would actually prove the cascade is gone, and it risks a second outage
of the live TTS/embedding path, so it needs Dave's go-ahead.

## `:8010` promoted to primary on every lilripper chain (2026-08-18)

**Change.** `settings.py` — the main, vision, and summary chains all swapped so
`lilripper:8010` is the first hop and `:8020` the fallback. The subagent chain
was already `:8010`-first and is unchanged; the reader was already `:8010`.
Mirrored in `.env.example`. No env override exists for any of these
(`~/.config/octavius/env` holds only `OCTAVIUS_LLM_API_KEYS`), so the committed
defaults are what runs — no drift to reconcile.

| chain | was | now |
|---|---|---|
| `OCTAVIUS_LLM_CHAIN` | `:8020` → `:8010` → `lilbuddy:8010` | `:8010` → `:8020` → `lilbuddy:8010` |
| `OCTAVIUS_VISION_LLM_CHAIN` | `:8020` → `:8010` | `:8010` → `:8020` |
| summary URL / fallback | `:8020` / `:8010` | `:8010` / `:8020` |

**Why.** Dave's reason, and it is an operational one rather than a latency one:
`:8020` is the port he loads other models onto by hand, and with `:8020` as the
main primary *every Octavius turn evicted whatever he had there*. Confirmed live
at the time of the swap — `/v1/models` showed `qwen3.8-27b` **loaded** on `:8020`
and `qwen3.6-35b-a3b-mtp-general` **loaded** on `:8010`, i.e. the next voice turn
would have thrown away the 27B to reload a model that was already resident one
port over. The two supporting facts that make the demotion free: `:8010` has been
`--parallel 3` since 2026-08-13 (so it is no longer the narrower endpoint), and
the specialist subagents already live there.

The consolidation is the real win. `:8010` now serves the main turn, consults,
vision, the reader, and summaries, **all naming the same alias**, so a
turn → consult → document read sequence keeps one model resident for the whole
sequence with zero router swaps.

**Two things to watch.**

1. **Auth is now load-bearing on the first hop.** `:8010` is the only endpoint
   behind auth. A missing or stale key used to 401 a *fallback*; it now 401s the
   primary of every turn, and a 401 burns a failover hop like any other error.
   `/health` → `llm_chain.endpoints_rejecting_credentials` is the first thing to
   check if the chain looks flaky. Verified working against the live endpoint
   before the swap.
2. **Concurrency on `:8010`.** Three slots now absorb main turns + consults +
   reader + summaries. Single-user this is fine and `consult_specialist` is
   *inline* (the main agent is blocked awaiting it, so they never contend at the
   same instant), but if several sessions run at once, measure rather than reason.

**Found while checking the above: the main chain's third hop was silently
dead.** `lilbuddy:8010` was configured with the bare `qwen3.6-35b-a3b`, and a
real completion returns `400 model 'qwen3.6-35b-a3b' not found` in 2 ms — that
alias is gone from lilbuddy's catalog too (now 10 aliases, down from 14), so it
exists on **no** host in the fleet. Repointed — after one wrong turn — at
`gemma4-26b-a4b`, Dave's pick.

**The wrong turn is the useful part.** The hop first went to
`qwen3.6-35b-a3b-mtp-q4-general`, the closest same-family alias lilbuddy
carries. Asking lilbuddy to actually load it gave 25 s of nothing and then
**502 on every alias and every port on the box at once** — the `:8010` router,
the `:8020` bge-m3 embedding server, and `:8880` Kokoro TTS all went down
together, which is a much wider blast radius than one bad model id. Caddy stayed
up (hence 502 rather than connection-refused), and everything came back on its
own about two minutes later with the router holding nothing. Reading it as an
OOM cascade on a 128 GB unified-memory box fits: an A4B was already resident and
a dense 35B was requested on top. **So lilbuddy's third-hop alias is a capacity
decision, not just a naming one** — the fleet-wide consequence of getting it
wrong is that Octavius loses TTS and embeddings too, not merely a fallback.

`gemma4-26b-a4b` measured after recovery: 16 s cold load, then 0.5-0.9 s for
short replies, tool calls in correct OpenAI shape in ~0.9 s, image input
supported. It is a thinking model that puts reasoning in a separate
`reasoning_content` field instead of inline `<think>` tags, which costs nothing
— `agent.py` reads `delta.get("content", "")` and the reasoning simply never
appears. It does think hard (714 reasoning deltas to 30 content deltas on a
one-sentence question, ~6.5 s to first visible token), which is fine for a hop
that only runs during a lilripper outage. The one latent trap: at
`max_tokens=120` it burned the whole budget on reasoning and returned
`finish_reason: length` with empty content. Octavius sets `max_tokens` nowhere
today, so nothing is broken — but anything that starts to must budget for it.

This is worth remembering because of how quietly it fails. A missing alias 400s
*instantly*, `LLMChainClient` buckets it as `client_error` like any other
failover, and `/health` cheerfully lists three configured endpoints — so the
chain looked like it had a cross-host last resort when it had none. It would
only have surfaced during a full lilripper outage, i.e. exactly when it mattered.
Same bug class as the 08-13 catalog rebuild below; the lesson is that
**lilbuddy's catalog churns too**, and its hop needs the same re-verification the
lilripper ones get. Not fixed by the reorder — found by it.

Also corrected: the note that the vision chain stays separate because the main
chain's third hop "might not support images". That was a hedge, and the real
reason is different — the third hop is chosen for *availability*, and separate
chains are what stop a future "add a fallback so voice survives a lilripper
outage" edit from widening where an image turn can land. It is a guard against a
future edit, not a claim about today's catalog. Today the q4 MTP alias on
lilbuddy *does* take images, which means **cross-host vision failover is
currently available** — still the open item from 2026-08-13.

**Catalogs re-curled the same day** (they churn; CLAUDE.md's list was 08-13):
`:8020` gained `qwen3.8-27b` + `qwen3.8-27b-non-thinking` (5 aliases);
`:8010` gained the same pair and lost `qwen3.6-27b-mtp-{code,general}`
(9 aliases). Notably **every alias on both ports now reports `text,image`**, so
"that fallback is text-only" is no longer a safe assumption on lilripper in
either direction — read `architecture.input_modalities`. The vision chain is
still kept separate from the main chain, but the reason has narrowed to the
main chain's *third* hop (`lilbuddy:8010`), whose image support is not
guaranteed across rebuilds.

448 tests pass unchanged; no test pinned the chain order.

## Email and paper search cost 10-73 s per call (2026-08-14) — none of it was Octavius

Dave asked why email search felt slow and whether the folder defaults needed tuning.
Folder scope turned out to be **irrelevant to latency**: every `search_emails` call took
~10.4 s whether it matched 0 emails or 17, one folder or all 107. The cost was two
unrelated full scans in `mcp-tools`, and the same class of bug was costing far more in the
paper library.

**Fault 1 — `_index_health()` ran on every search.** It counted mail with content but no
embedding via an unscoped `LEFT JOIN` over 28k rows / ~500 MB of `body_text` plus a vec0
point lookup per row: 10.35 s, to produce a number that is always 0. `_attach_health` only
*attaches* it when unhealthy, so it was computed and discarded nearly every call. Now
scoped to a 30-day window on `idx_emails_date` (~5 ms); the emitted key became
`unembedded_recent_emails` so it doesn't claim more than it checks.

**Fault 2 — the dense arm was O(N).** `hybrid_search` and `search_papers` brute-forced
`vec_distance_cosine` over every stored vector through a JOIN. Both now use vec0's native
KNN (`WHERE embedding MATCH ? AND k = ?`).

| | before | after |
|---|---|---|
| `search_emails` | 10.4 s | **0.15 s** |
| `semantic_search` | 10.6 s | **0.24 s** |
| `hybrid_search` | 40.1 s | **0.21 s** |
| `search_papers` | 72.9 s | **0.41 s** |

**The trap, and the reason this is worth reading.** Swapping in the KNN operator alone
would have silently corrupted results. **vec0 defaults to L2**, and these bge-m3 vectors
are stored *unnormalized* — stored norm ~26 against a unit-norm query vector — so L2 and
cosine rank almost nothing alike: measured **3/50 overlap**, no error raised. The fix is
declaring `distance_metric=cosine` on the embedding *column* (as a *table* option
sqlite-vec v0.1.7 errors "Unknown table option"). Both vec0 tables were rebuilt to declare
it; the vectors themselves were untouched, so no re-embedding. Post-rebuild the KNN
matches the old cosine ranking: top-10 identical, 50/50 candidate overlap, across 4 email
and 3 paper queries, plus folder-filtered cases.

Rebuild mechanics, learned the hard way: **`ALTER TABLE ... RENAME` reports success and
leaves a broken vec0 table** — the shadow tables (`_info`, `_chunks`, `_rowids`,
`_vector_chunks00`) keep the old name, and the next query dies with
`no such table: <name>_rowids`. So: stage rows into a plain table → `DROP` → recreate under
the final name with the metric → refill. 18 s for 28,466 email vectors, 120 s for 174,876
chunk vectors. Backups at `evangeline.db.bak-20260814-premetric` and
`papers.db.bak-20260814-premetric`. Both sync timers were stopped during the rebuild —
`evangeline-sync.timer` fires every 20 min and would otherwise have written mid-migration.

Filtered KNN was the one semantic worth verifying: vec0 applies an `id IN (subquery)`
filter *during* the search, not after, so a folder holding fewer than `k` rows returns all
of them rather than a slice of a global top-k (Todo 24/24, Follow-Up 8/8, Read Later 9/9).

**Two bugs that were ours** (`subagent.py`): the email prompt told the model to scope to
`folder="INBOX"`, which matches **nothing** — folder comparison is case-sensitive and the
folder is `Inbox`. And it had no folder vocabulary, so "my to-do folder" cost 38 s guessing
`"To Do"` → `email_stats` → `"Todo"`. Scope is now constrained to five values — `Inbox`
(default), `Read Later`, `Follow-Up`, `Todo`, or All (`folder=null`) — with All gated
behind an explicit request from Dave.

**Still open:** `find_similar_responses` is dead for an unrelated, pre-existing reason —
it resolves the sent folder from a `folders` table that is **empty** (confirmed empty in
the pre-migration backup too, so it is not a regression from this work). The
`hybrid-search-db` skill in `pi_harness` documents the KNN swap as its scale-up path
without mentioning the metric requirement — that is task #11, and it would hand the same
silent corruption to the next corpus built from it.

## lilripper rebuilt its model catalogs (2026-08-13) — four chains were pointing at a dead alias

Dave reconfigured lilripper. `qwen3.6-35b-a3b-mtp-q4-general` **no longer exists on
either lilripper port**, and four config sites named it. Verified by probe, not by
reading `/v1/models`:

```
POST lilripper:8010 model=qwen3.6-35b-a3b-mtp-q4-general
  -> 400 {"error":{"message":"model 'qwen3.6-35b-a3b-mtp-q4-general' not found"}}
```

Caught before it bit: the journal shows subagent consults succeeding on `:8010` at
07:49 that day and **no 400s anywhere**, so the rebuild landed after that. Blast radius
had it gone unnoticed, worst first:

- **Reader LLM — hard broken.** The reader has no failover at all, so every math chunk
  would 400 and silently degrade to dollar-stripping. Exactly the `qwen3.5-9b` failure
  from 2026-08-08, different cause, same silence.
- **Subagent primary — every `consult_specialist` 400s on hop 1** and lands on the
  `:8020` fallback. It still *answers*, which is what makes this nasty: the visible
  symptom is only that consults got slower, and they'd pile onto `:8020`, the endpoint
  the 2026-07-30 work moved them *off*. (That work's stated reason — `:8020` being
  single-slot — no longer holds; see below.)
- **Main chain hop 2** — 400 burns the hop, falls through to `lilbuddy:8010`. Works.
- **Vision fallback** — 400; image turns lose their only fallback.

**Fix.** All four sites retargeted to `qwen3.6-35b-a3b-mtp-general`, which exists on
`:8010` and is *also* what `:8020` serves. The old invariant ("every `:8010` consumer
names one alias, or a consult interleaved with a document read thrashes the router")
now holds more cheaply than before: both ports share one alias, so there is nothing
left to thrash. The old q4 rationale — speculative decoding beating Q5 weights on
tool-calling — is noted in `settings.py` against the day it matters again.

Three things this surfaced that are **not** fixed:

1. ~~`capacity: 3` is an unverified assumption.~~ **VERIFIED, and the claim that it
   couldn't be checked was wrong.** `/v1/models` on this llama.cpp build returns each
   model's full launch argv under `status.args`, plus `status.value` and
   `architecture.input_modalities`. `:8010`'s `qwen3.6-35b-a3b-mtp-general` is
   `--parallel 3`, so `capacity: 3` is correct. The original claim came from listing only
   `data[].id` and generalising from the parser rather than the payload — recipe now in
   CLAUDE.md's Runbook so it isn't repeated.
2. **The q4 aliases moved to `lilbuddy:8010`**, which also now serves
   `qwen3.6-35b-a3b-q6` and `qwen3-vl-30b-a3b`.
3. **`qwen3-vl-30b-a3b` accepts image input on a non-lilripper host.** Near-Term Work #4
   says cross-host vision failover is blocked because no non-lilripper endpoint takes
   images. **That is no longer true** — the prerequisite it was waiting on now exists.

### What the full `/v1/models` payload showed (2026-08-13)

Dave curled `:8020` directly and the response was far richer than the id list above.
Reading it properly corrected three things:

| endpoint / alias | state | `--parallel` | ctx | image in |
|---|---|---|---|---|
| `:8020` qwen3.6-35b-a3b-mtp-general | loaded | **3** | 614400 | yes |
| `:8010` qwen3.6-35b-a3b-mtp-general | loaded | **3** | 614400 | yes |
| `:8020` muse-glimmer-30b | unloaded | 1 | 131072 | yes |
| lilbuddy qwen3.6-35b-a3b | loaded | ? | 153600 | **yes** |
| lilbuddy qwen3.6-35b-a3b-mtp-q4-general | unloaded | 3 | 460800 | yes |
| lilbuddy qwen3-vl-30b-a3b | unloaded | ? | 32768 | yes |

1. **`capacity: 3` is right** — see item 1 above.
2. **`:8020` is not single-slot any more.** It is `--parallel 3`, same as `:8010`.
   Dave confirmed he raised it **on 2026-08-13** — it is in fact why this whole review
   started. So "the single-slot `:8020`", the stated rationale for the 2026-07-30
   subagent tier swap, was *correct when written* and simply expired; it is not a
   documentation error, it is a fact with a shelf life. It appears in `settings.py`,
   `CLAUDE.md`, and here, all now dated. The tier split still earns its keep (consults
   stay isolated from the main agent's turn), but with both endpoints at `--parallel 3`
   the two tiers are near-interchangeable, so **collapsing them is now a live option** —
   worth measuring, not worth assuming.
3. **lilbuddy:8010 is not text-only.** `CLAUDE.md` described it as a "plain single-model
   server, text-only", which was the stated reason the vision chain must stay separate
   from the main chain. It is a 14-alias router and `qwen3.6-35b-a3b` takes images. The
   separation is still correct (not every fallback is image-capable) but the reason was
   wrong.

Also: `:8010` and `:8020` now serve the **identical** `mtp-general` — same gguf, same
614400 ctx, same `--parallel 3`, both resident. That is a stronger argument for the
retarget above than the probe that motivated it.

General lesson, and the second time this shape has bitten in a week: **a model alias is
a distributed config dependency with no compile-time check.** `/v1/models` drift is
invisible until a request fails, and `LLMChainClient` treats the resulting 400 as an
ordinary failure to fail over past, so a missing alias degrades quietly instead of
erroring loudly. Re-probe both ports after any lilripper work.

## Deployment State (2026-08-11) — DEPLOYED

Restarted 2026-08-11 07:20. `/health`: `ok`, ready, **not** degraded, MCP 8/8.
This deployed both the 2026-08-10 endpoint work (a92c231) *and* the embedding-latency
work below. The prior "RESTART PENDING" backlog — router model ids,
`OCTAVIUS_8010_API_KEY` support, `/health` failure classification, the lilripper summary
chain plus `SummaryClient` auth headers, and reader pasted text — is now live.

Confirmed fixed by the restart: conversation-end summaries. The stale build had been
failing over `lilbuddy:8010 → triplestuffed:8010` (both dead) and **15 conversations
went unsummarised and unindexed between Aug 8 and Aug 11**.

TTS came back with lilbuddy on 2026-08-12.

Still outstanding: **`OCTAVIUS_LR_API_KEY` (renamed from `OCTAVIUS_8010_API_KEY`
2026-08-21) is not set in `~/.config/octavius/env`.**
Runbook to adopt it, and the drift to resolve first, are under "Adopt
`OCTAVIUS_LR_API_KEY`" in Near-Term Work.

## Embeddings off the turn path (2026-08-11)

**Symptom.** ~10 s between send and any GPU activity, and ~10 s of stuck "Thinking..."
after the reply was already on the client — every turn. Journal:
`11:13:35.800` LLM 200 → `11:13:46.995` embed 200, an 11.2 s tail.

**Cause — three compounding faults, not one.**

1. `lilbuddy:8020` was the embedding chain *primary* and the machine is unreachable. It
   drops packets rather than refusing connections, so each attempt burned the full
   connect budget (`HTTP 000` after a 6 s probe, no TCP handshake).
2. `EmbeddingClient._is_retry_worthy` **retried connect timeouts**, contradicting its own
   docstring: `httpx.ConnectTimeout` subclasses `httpx.TimeoutException`, and
   `requests.exceptions.ConnectTimeout` subclasses `Timeout`. So a dead host cost
   `5 + 0.3 + 5 = 10.3 s` instead of 5 s. Same bug class as the `classify_chain_error`
   fix on 2026-08-08.
3. `EmbeddingClient` had **no failure memory at all** — its only state was `self.chain`,
   so every embed re-walked from index 0 and re-paid the whole budget.

And the reason it landed on the turn path: `history.add_message_async` **awaited**
`store_embedding_async` inline — once before `stream_agent_turn` (user message) and once
before `audio_done` (assistant message). `audio_done` is what re-arms client recording,
so both the PWA and the Android app sat waiting on an embedding.

**Fixes.** Chain reordered to workhorse-first in `settings.py` (not the env file — a
second source of truth would drift against the committed default). `ConnectTimeout` no
longer retries. Per-endpoint circuit breaker with single-owner half-open probing.
Message embeds detached via `spawn_embedding`. New `history_sweeper.py` repairs anything
that never landed. `conversations.indexed` persists the summariser's index decision.
See CLAUDE.md's Embeddings bullet for the durable description.

**Verified live — and the first deploy found a real bug.** The first sweep repaired 5
rows and then "aborted on a failing chain". The chain was fine. Message 1524 is **20,034
characters**, and workhorse 500s on anything past roughly 4-6k (Ollama's default
`num_ctx` is 2048 tokens; measured: 4000 chars → 200, 6000 → 500). Because the batch is
ordered newest-first and the pass aborted on the first `None`, that one row sat at the
head of the queue and blocked the other 12 **permanently**. The convergence test missed
it because every row in it was the same small size.

Two fixes, both pinned by tests:

- `history_enrichment.EMBED_MAX_CHARS = 4000` clips input at the choke point every embed
  goes through. Not `num_ctx`: bge-m3 caps at 8192 tokens so a 20k-char input still 500s
  with `num_ctx=8192`, and that call took 4.5 s versus 0.25 s.
- The sweeper now separates "this row is bad" from "the chain is down". It skips a
  failing row and gives up only when the breaker reports every endpoint tripped, or
  after `MAX_CONSECUTIVE_FAILURES` in a row.

Worth remembering as a general shape: **a per-item failure and a whole-dependency
outage look identical when the item is the thing you retry first.**

**Chain order restored to lilbuddy-first (2026-08-12).** lilbuddy came back (a tailscale
fault, not the machine). The reorder above was a workaround for a *cost* problem — a dead
primary burning the full connect budget on every embed — and that problem no longer
exists: the `ConnectTimeout` fix plus the breaker cap a dead primary at two failures and
then 300 s of silence. So the order is now decided on speed alone. Measured on a
realistic ~1800-char payload, three calls each: **lilbuddy 0.107 / 0.107 / 0.109 s,
workhorse 2.67 (cold) / 0.326 / 0.304 s** — ~3x, plus a cold-start penalty after idling.
Note this is *not* visible in turn latency any more, since embeds are off the turn path;
it shows up in sweeper throughput and in search-query embeds.

`EMBED_MAX_CHARS = 4000` stays as it is. The cap has to suit the weakest endpoint in the
chain, and workhorse (Ollama, `num_ctx=2048`) is still in it as the fallback.

**Two gotchas worth keeping.**

- The **live database is `/media/extra_stuff/octavius/octavius_history.db`** (set by
  `OCTAVIUS_DB_PATH` in the unit, *not* in `~/.config/octavius/env`). The repo-local
  `octavius_history.db` is a stale leftover and will happily answer queries with wrong
  numbers.
- `main.py`'s lifespan calls `app.state.db_init()` with **no argument**, so an injected
  `db_path` never reaches the initializer. Tests that only patch `db_init` leave
  `app.state.db_path` pointing at the real database — which now matters, because the
  lifespan hands `db_path` to the sweeper. `tests/test_main.py::_isolated_app` patches
  all three.

**Follow-ups, deliberately not done here.**

- `tools.py:112-121` dispatches sync local-tool handlers directly on the event loop, so
  `search_conversation_history` still blocks it during its query embed. Fixing it means
  touching the shared dispatch contract for every local tool.
- `end_async` still awaits four remote calls (summary LLM, summary embed, tags LLM,
  memory push) on the WS control path, so New Chat / reset / attach stall behind them.
  The real fix is detaching the whole of `end_async`, not just the embed.

What a restart fixes: summaries/tags start working again, the main chain gets a
*working* second hop (`lilripper:8010`) instead of two dead ones, and failing over
stops costing 120 s on the triplestuffed zombie.

What a restart does NOT fix: **voice**. TTS is `lilbuddy:8880` Kokoro and lilbuddy is
still fully down — Octavius stays mute until it returns or a Kokoro runs elsewhere.
Text and Matrix turns are unaffected.

Also note `/health` will report `degraded` again the first time any request exhausts
the whole chain, and stay that way until restart — that flag is a lifetime counter,
not current state (see Stability Notes).

## OPEN: Android client misbehaving, Octavius server looks clean (2026-08-10)

Dave reported Octavius "misbehaving". Server-side investigation found **nothing wrong**:
25/26 chain requests succeeded on `:8020`, zero failovers, MCP 8/8 connected, and the
only journal errors since 2026-08-08 were the one `:8020` model-load 500 and a 403 from
a paywalled inc.com download. Nothing recurring.

Dave's read: **the PWA is working fine, so the fault is likely in the Android client**
(`../octavius-android`), not the server. Start there when picking this up.

Rule out the known-broken-but-unrelated things first, none of which are the Android app's
fault and all of which look like misbehaviour from a client:

- **No TTS at all** — `lilbuddy:8880` Kokoro is down, so every voice turn returns text and
  no audio on *both* clients. Not a client bug.
- **`/health` reports `degraded`** — latched lifetime counter from Friday, not live state.
- **Summaries/tags failing silently** — dead summary chain; fixed in the tree, needs the restart.

Note the Android client depends on WS behaviour the server did NOT change this session
(STT/VAD/`audio_done`/empty-transcription semantics are untouched — see CLAUDE.md "Native
Android client"), so a protocol regression from this session's work is unlikely.

## Reader: pasted text (2026-08-10)

The reader now accepts raw text alongside files, URLs, and inbox items.

- `POST /api/reader/documents {"source":"text","text":...}` already existed and was
  built for the Android client (`ReaderRepository.addText`). What was missing was the
  agent tool and the web UI.
- `read_document` takes `text` as an alternative to `path` (either one suffices), and
  delegates to `start_text_ingest` so the agent and the UI can't drift.
- `/reader` gained a Paste panel (`static/reader.html` + `reader-app.js`); the main
  voice UI and the WS protocol are untouched, so no Android rebuild is needed.
- Titles are derived from the first line when omitted (`document_sources.derive_title_from_text`),
  which also stops Android's blank-title pastes from all landing as "Untitled".
- Pasted text is persisted to `<reader_dir>/pasted/<id>-<slug>.md` and recorded as
  `source_path`, which is what makes it retryable — `start_retry_task`'s existing
  `markdown` branch needed no changes. Best-effort: a write failure costs retry, not
  the document.

## Refactor Status

The codebase has been through a reliability and maintainability refactor focused on reducing orchestration-heavy modules and making external-service boundaries clearer.

Completed work:

- runtime settings moved to `settings.py` with env-backed defaults
- core STT, TTS, main LLM chat, summary-generation, and embedding HTTP integrations now live behind `service_clients.py`
- reader ingest orchestration extracted from `main.py` into `reader_ingest_service.py`
- WebSocket session and conversation handling extracted from `main.py` into `websocket_session.py`
- history responsibilities split across:
  - `history.py` for DB bootstrap, conversation/session recording, and compatibility re-exports
  - `history_enrichment.py` for embeddings, summaries, and tags
  - `history_store.py` for queries, inbox CRUD/search, and stats
- local tool responsibilities split across:
  - `tools.py` as the public entrypoint used by the agent loop
  - `local_tool_specs.py` for schemas
  - `local_tool_registry.py` for dispatch
  - `local_tool_downloads.py`, `local_tool_inbox.py`, and `local_tool_reader.py` for execution logic
- document source handling centralized in `document_sources.py`
- test coverage baseline added under `tests/` for the major subsystems
- request handlers and background reader jobs now use short-lived SQLite connections instead of sharing one app-wide connection
- route groups were split out of `main.py` into dedicated router modules for inbox, conversations, and reader APIs
- shared browser helpers were extracted into `static/app-common.js` to reduce duplicated WebSocket and voice-loading logic across inline pages
- the inbox and reader pages now load page-specific behavior from `static/inbox-app.js` and `static/reader-app.js` instead of keeping those scripts inline
- the main voice UI now loads page behavior from `static/index-app.js`, with streamed TTS queue and silence-trimming logic isolated in `static/index-audio.js`
- reader responsibilities were split across `reader_store.py`, `reader_text.py`, and `reader_playback.py`
- reader ingest entrypoints were narrowed in `reader_ingest_service.py`, with source-specific URL/PDF/file handling moved to `reader_ingest_handlers.py`
- local tool dispatch now routes through `tools.py` and `local_tool_registry.py`
- local tool execution was further split by domain into `local_tool_downloads.py`, `local_tool_inbox.py`, and `local_tool_reader.py`
- internal callers now use the concrete reader and local-tool modules directly; the old `reader.py`, `local_tool_handlers.py`, and `config.py` shims have been removed
- STT moved from batch record-then-transcribe to streaming partial transcription using faster-whisper on lilripper
- server-side Silero VAD added for automatic end-of-speech detection (1.5s silence threshold)
- continuous conversation mode added: hands-free multi-turn loop where the mic auto-reopens after TTS playback
- talk mode selector replaced the toggle-to-talk checkbox (hold / tap / continuous)

## Current Hotspots

These areas still carry the most complexity or coupling:

- `main.py` still owns startup wiring and top-level app composition, but the main REST route groups have been split into dedicated router modules
- reader ingest and playback are cleaner, but the overall reader flow still spans several modules and background-task boundaries
- frontend logic is now extracted into JS assets, but the UI still relies on large static HTML shells

## Reader And PDF Fixes

These behaviors were fixed recently and should not regress:

- local files are identified as PDFs by content, not only by `.pdf` suffix
- arXiv `/pdf/` downloads are saved with a `.pdf` suffix
- the `read_document` local tool now starts PDF conversion instead of only creating a DB row
- reader startup marks stale `reader_documents.status='processing'` rows as failed because in-memory jobs do not survive restart
- post-conversion markdown lookup is resilient to mismatched output filenames from the remote processor
- failed or interrupted reader documents can now be requeued from stored source metadata through the retry API/UI

## Matrix Media / Docproc Fixes (2026-07-20)

These were fixed in **mcp-tools** (`server_documents_voice_wrapper.py` /
`server_documents_wrapper.py`, commits `cc9c82b` + `98191f1`) but the symptom
appears in Octavius, so they're recorded here; should not regress:

- **Matrix PDF conversions failed with a permission error** at the download
  step: the wrapper created the output dir *next to the source PDF*, and the
  Matrix spool (`/media/extra_stuff/octavius/matrix_media/`, owned by
  `octavius-matrix`, mode 755) is read-only for the service user. The wrapper
  now falls back to `~/docproc-output/<stem>-<source-path-hash>/` when the
  source's parent isn't writable (`W_OK|X_OK`), pre-creates the target, and
  scp's the remote dir's *contents* (`/.` suffix — plain `scp -r` nests into
  a pre-existing target and breaks re-conversions).
- **Remote dedup hits returned a wrong md path**: the cached output's `.md`
  carries the *original* upload's filename stem, and the Matrix sidecar
  prefixes every upload with a unique id — so re-sending the same PDF always
  mismatched and Octavius logged "Could not read converted markdown" (the
  agent then silently compensated via web search). The wrapper now globs the
  downloaded dir for the actual `.md` instead of assuming the stem.
- Cross-channel history access shipped the same day: `read_conversation` +
  `source`/`since` filters on `search_conversation_history` (see CLAUDE.md
  "Conversation History").

## Voice / TTS Fixes

These behaviors were fixed recently and should not regress:

- spoken text is markdown-normalized before TTS via `speechify` in `tts.py`,
  applied at the shared `synthesize` choke point (main voice, proactive,
  item-chat, reader playback). It strips conversational markdown AND orphan
  emphasis left when a bold/italic span is split across streamed sentences (the
  cause of TTS reading "asterisk asterisk"), while preserving `3 * 4` / `foo_bar`.
- response style is channel-aware: `stream_agent_turn(source=...)` injects a
  per-turn directive (`VOICE_STYLE`/`TEXT_STYLE`), so voice replies are short and
  markdown-free while typed/Matrix replies may use light markdown and be fuller.
  Note: this needs the service restarted to take effect (in-memory prompt).

## Web Search

- The main-agent web search moved from the varlabz `searxng-mcp` (`search` tool,
  SearXNG-only, no fallback) to mcp-tools' `server_serper.py`, registered as the
  `web-search` MCP server exposing `web_search` (page reading stays on Crawl4AI
  via `web-reader`). The agent scopes it by the `web-search` server key in
  `agent.py`. See CLAUDE.md "Configured MCP servers".
- **Provider order inverted 2026-08-20 — Serper.dev (Google) is now primary and
  SearXNG is the backstop.** It answers only when Serper *errors*; an empty
  Serper result is a valid answer to an obscure query and is not retried. The
  old order was silently serving junk: SearXNG's `bing` engine had started
  returning topic-unrelated pages while reporting HTTP 200 with
  `unresponsive_engines: []` (AutoZone for "Slepian sequences prolate"), and
  because results interleave bing/ddg, roughly two hits in three were unrelated
  while the fallback never fired — its trigger was "SearXNG returned nothing".
  The backstop is now pinned to `engines=duckduckgo,wikipedia` (`SEARX_ENGINES`)
  so the degraded path has no scraper to babysit. A degraded answer is
  self-identifying: `provider: "searxng"` plus a `fallback_reason` field.
- `SERPER_API_KEY` in `mcp-tools/.env` is now load-bearing rather than a
  nice-to-have: without it the primary arm is inert and every search runs on the
  degraded backstop. The stdio subprocess reads `.env` at spawn, so a service
  restart is required to pick up a newly-added key.
- The same inversion lives in `pi_harness/extensions/web-search/src/index.ts`
  (pi's native tool). The two are deliberate mirrors — change one, change the
  other.
- **Cert gotcha (should not regress):** SearXNG is fronted by Caddy's internal CA,
  which Python's bundled certifi does not trust. The SearXNG client must point at
  the system CA bundle (`/etc/ssl/certs/ca-certificates.crt`), NOT
  `/etc/ssl/cert.pem` (absent on this Ubuntu host) — a wrong path silently fails
  every SearXNG call with `CERTIFICATE_VERIFY_FAILED`. `server_serper.py` handles
  this itself (honors `SSL_CERT_FILE`, else the system bundle). This was the cause
  of the post-triplestuffed-migration "web search returns nothing" outage.

## Stability Notes

Operational assumptions worth keeping in mind during debugging:

- external service reachability problems can look like application bugs if STT, TTS, LLM, or MCP endpoints are unavailable
- `/health` now exposes `alive`, `ready`, `degraded`, per-server MCP status, and `llm_chain` failover information, so degraded runtime behavior should be checked there first
- reader ingest jobs are in-memory background tasks and do not survive restart
- docproc job ids are in-process state in the document-processing stdio wrapper and also do not survive an Octavius restart — `check_document_status` on a pre-restart id reports it unknown (graceful, but the model may go hunting)
- restart recovery is now manual requeue rather than automatic job resurrection
- live conversation and item-chat history sessions still keep their own dedicated SQLite connection until they are ended
- the browser UIs are less script-heavy than before, but layout and markup are still concentrated in large static HTML files
- Silero VAD requires `models/silero_vad.onnx` to be present; if the file is missing, VAD is skipped and auto-stop will not work
- STT failover (lilripper primary, lilbuddy fallback) is not yet implemented — switching requires a settings change
- **`lilripper:8010` is behind auth (2026-07-13).** It 401s without a bearer token. The key lives in `~/.config/octavius/env` and reaches the service through the `EnvironmentFile` drop-in. As of 2026-08-08 the preferred variable is a dedicated bare token (no JSON quoting to get wrong), now **`OCTAVIUS_LR_API_KEY`** (renamed from `OCTAVIUS_8010_API_KEY` 2026-08-21) which takes precedence over the older `OCTAVIUS_LLM_API_KEYS` JSON map; the token rotates but the variable name does not. It is bound to `lilripper:8010` specifically (`settings.KEYED_8010_ORIGIN`) — `lilbuddy:8010` and `triplestuffed:8010` share the port, are open, and must stay unkeyed. `service_clients.auth_headers()` attaches it by URL on every `LLMChainClient` request path. Three consumers depend on it: the reader LLM, the vision chain, and — since 2026-07-30 — the subagent **primary** tier, so a bad key breaks every `consult_specialist` on its first hop rather than only on failover. Two failure modes to keep apart when debugging: a **missing/wrong key** is a 401 → `HTTPStatusError` → burns a failover hop, while **empty content with no failover** is usually Qwen think-mode eating a small `max_tokens`, not auth. Nothing loads a `.env` file, so a key placed there is silently ignored.

  **Since 2026-08-08 a 401 identifies itself.** `/health`'s `llm_chain` now carries `endpoints_rejecting_credentials` (URLs whose most recent attempt was 401/403), `auth_failures`, `last_failure_kind`, and per-endpoint `last_error_kind` / `last_error_status` / `authenticated`; a 401 also logs at ERROR naming the origin and the env var to check. Triage order when a chain looks flaky: `endpoints_rejecting_credentials` non-empty → key problem; `last_error_kind` of `connect`/`connect_timeout` → host down (no TCP handshake); `timeout` → host accepted the connection but never generated (zombie); `last_error_kind == "client_error"` with 400/404 → the model alias isn't in that endpoint's catalog. `last_error_*` clears on the endpoint's next success, so it reflects current belief, not history.
- **Memory push was silently dead 2026-07-02 → 2026-07-13 (fixed; should not regress).** When the memory service was extracted to the `agent-memory` repo, `history.py`'s push path kept doing `import memory` for three watermark helpers, so every conversation end logged "Memory client unavailable; skipping push" and skipped the push. The helpers (`get_memory_watermark` / `set_memory_watermark` / `messages_after_watermark`) now live in `history_store.py` — they only touch Octavius's own tables (`conversations.last_extracted_message_id` + `messages`), so Octavius no longer imports anything from agent-memory except over HTTP via `memory_client.py`. Conversations that *ended* during the gap were never mined for facts (push happens at conversation end; watermarks stayed put but closed conversations don't re-push) — a backfill would need a one-off script.
- **WS disconnects arrive as messages, not exceptions (fixed; should not regress).** Starlette's `ws.receive()` returns a `websocket.disconnect` message; calling `receive()` again raises `RuntimeError`. The run loop used to reach cleanup *through* that RuntimeError, which chained the traceback into every `exc_info` warning logged during cleanup (confusing journal noise). `websocket_session.run` now breaks on the disconnect message itself.
- **Caddy leaks upstream WS sockets when a client stalls silently (mitigated 2026-07-13).** A downstream client that freezes without dying (phone in Doze: app stops reading, kernel keeps ACKing) blocks Caddy's copy goroutines; when uvicorn's WS ping timeout then closes the upstream leg, Caddy never reaps its side — one CLOSE_WAIT socket to `127.0.0.1:8030` per stalled client (~3/day observed; clean closes and RSTs do NOT leak — verified by live probe). Mitigation: `stream_timeout 24h` in the octavius `reverse_proxy` block in the Caddyfile (safe because the app and PWA both auto-reconnect). A Caddy restart clears any backlog.
- **Subagent chain has no cross-host failover — and as of 2026-08-08 neither does the vision chain.** `consult_specialist` routes primary `lilripper:8010` (`qwen3.6-35b-a3b-mtp-general` since the 2026-08-13 rebuild, `--parallel 3`, `capacity: 3`) → fallback `lilripper:8020` (`qwen3.6-35b-a3b-mtp-general`). The tiers swapped on 2026-07-30 to get consults off the main agent's single-slot `:8020` (see `HANDOFF-matrix-latency.md`). Both tiers are on `lilripper`, so if that host is down the specialist has nowhere to go (the `lilbuddy:8010` / `triplestuffed:8010` tiers were dropped for latency). The dispatcher only tries `[assigned_url, fallback_url]` per call, and `secondary` is concurrency-overflow only — so re-adding resilience means putting a remote host in the **`fallback`** slot, not `secondary`.

  The **vision chain** now has the same shape: `lilripper:8020` → `lilripper:8010`. It gained a second entry on 2026-08-08 (it was previously a single `:8010` entry with no failover at all, so this is an improvement, not a regression) — but both are on lilripper. Restoring cross-host vision failover is harder than for subagents: the remote host must accept **image input**, and neither `lilbuddy:8010` nor `triplestuffed:8010` currently does. Until one of them serves a multimodal alias, the third entry doesn't exist to add. The reader (`:8010`) has no failover either and never has.

  Net: **lilripper is a single point of failure for consults, image turns, and the reader.** The main text chain is the only LLM path with cross-host redundancy on paper (`lilbuddy:8010`, `triplestuffed:8010`) — but see below, both were dead when measured.

- **lilbuddy was fully down 2026-08-08 → 2026-08-12 (RESOLVED — it was a tailscale fault, not the machine).** Dave couldn't ping or ssh it and wasn't physically near it; no ICMP response, `:8010`/`:8020`/`:8880` all unreachable. Re-verified 2026-08-12: embeddings `200` in 0.11 s, Kokoro `200` in 1.4 s. Keep the breakdown below — it is the record of what a lilbuddy outage actually costs, and the TTS single-point-of-failure it exposes is still unfixed. **General lesson: "host unreachable" was a tailnet-layer fault the whole time. Check the overlay network before concluding hardware is dead.** What lilbuddy carries:
  - **`:8880` Kokoro — the live TTS path. This is the unfixed structural risk.** `TTSSettings.voxtral_enabled` is False, so *every* synth call goes straight to this "fallback" and the Voxtral primary is never attempted. With lilbuddy gone there was **no working TTS at all**: Octavius was mute on the voice UI, the Android client, and continuous conversation, and text/Matrix turns were the only unaffected paths. `lilripper:8030` (the configured Voxtral primary) 502s — Caddy is up, no upstream behind it — so flipping `OCTAVIUS_TTS_VOXTRAL_ENABLED=1` does not help. Kokoro answers again as of 2026-08-12, but nothing about the topology changed: **one host going away still means no voice.** A Kokoro instance on triplestuffed (co-located with Octavius, no network hop) remains the obvious fix.
  - **`:8020` bge-m3 — the embedding primary.** Degraded cleanly: the `workhorse:11434` Ollama fallback stayed up returning 1024-dim vectors, so semantic history/inbox search kept working. It was promoted to primary for the outage and demoted back on 2026-08-12 (see below).
  - **`:8010` — main-chain fallback.** Now the third hop; costs only a fast connect failure while down.

- **triplestuffed:8010 is a zombie (2026-08-08).** `/v1/models` answers `200` in ~1 ms, but `/v1/chat/completions` never returns (>30 s by hand, 120 s `ReadTimeout` in the app) — its GPUs are serving Positron IDE autocomplete/NES models. Reachability checks pass while generation is dead, the same failure shape as the reader's stale `qwen3.5-9b`: **do not treat a `/v1/models` probe as proof an endpoint works.** Removed from the main chain and from the summary chain as a result.

  Consequence for the cross-host work: the two hosts that were candidates for the subagent/vision `fallback` slot are exactly these two. Fix the hosts before wiring anything to them, and verify with a real completion, not a model list.

- **`/health` latches `degraded` until restart.** `main.py` computes `degraded = mcp_degraded or llm_health["terminal_failures"] > 0`, and `terminal_failures` is a lifetime counter — so a single all-endpoints-failed request pins `status: "degraded"` forever even after the chain fully recovers. Observed 2026-08-08: one failed turn at 13:48, chain healthy at 14/15 successes afterwards, still reporting degraded. Contrast the per-endpoint `last_error_kind`, which deliberately clears on success. Making `degraded` reflect current state (e.g. terminal failure since last success, or a recent window) is a small `main.py` change; not done yet because it changes a signal anything might be alerting on.

- **Both lilripper LLM ports are llama.cpp routers now (2026-08-08), so model ids are load-bearing.** `:8020` stopped being a single-model server; the bare `qwen3.6-35b-a3b` alias exists on **neither** lilripper port (only on `triplestuffed:8010` / `lilbuddy:8010`). `complete_with_tools` and `stream_chat` send each chain *entry's* model, and an alias missing from that endpoint's catalog hard-400s — which `LLMChainClient` treats as an ordinary failure, so it silently burns a failover hop instead of surfacing as a config error. This bit the subagent fallback, which sat on the dead bare alias until 2026-08-08 (every subagent failover was a guaranteed 400 — i.e. `consult_specialist` effectively had no failover at all). Full per-port catalogs are in CLAUDE.md under "Router model ids"; re-curl `/v1/models` on both ports before trusting a model id.

## Near-Term Work

Likely refactor targets, in rough priority order:

1. Further narrow `main.py` so it remains a routing layer rather than a coordination module.
2. Reduce the size of the remaining static HTML shells by extracting reusable frontend structure or templates.
3. Continue replacing coarse integration paths with narrower behavior-level tests where the boundary is now stable.
4. Restore cross-host failover for the subagent chain (see Stability Notes; Dave flagged 2026-08-08, targeting the next couple of days): decide whether a remote host should occupy the `fallback` slot, and/or extend the dispatcher so more than one host is tried per call. Consider whether `secondary`/`fallback` role semantics should be reworked so cross-host resilience and concurrency overflow aren't mutually exclusive. Two prerequisites are infrastructure-side, not code: (a) the remote host needs a model alias that actually exists there, and (b) for the **vision** chain it must accept image input. **(b) was unblocked on 2026-08-13**: `lilbuddy:8010` now serves `qwen3-vl-30b-a3b`. Verify it actually generates on an image payload before wiring it in — `/v1/models` is not proof. Sequence it as: serve a multimodal alias on `triplestuffed:8010` or `lilbuddy:8010` → add it as the vision `fallback` → then revisit the subagent slot.
5. **Adopt `OCTAVIUS_LR_API_KEY` (Dave to action; 2026-08-12; var renamed from `OCTAVIUS_8010_API_KEY` 2026-08-21).** The service authenticates to `lilripper:8010` through the older `OCTAVIUS_LLM_API_KEYS` JSON map. That works, so this is hygiene, not an outage — the point is that the bare var is the one that rotates, has no JSON quoting to get wrong, and has a name that survives rotations.

   **Resolve the drift first.** Dave's interactive shell exports an `OCTAVIUS_8010_API_KEY` whose value **differs** from the token in `~/.config/octavius/env` (fingerprints `32173c2e…` vs `94745832…`). Probed 2026-08-12: `/v1/models` returns **401 unauthenticated and 200 for *both* tokens** — lilripper:8010 accepts them both, so nothing is broken today, but two live keys for one endpoint is exactly the drift shape that turns into a silent 401 the moment one is revoked. Decide which token is canonical before copying anything. The shell export is not in any dotfile (`.bashrc`/`.profile`/`environment.d`/systemd user env are all clean), so it came from an ad-hoc `export` and will vanish with that terminal.

   Then:

   ```bash
   # 1. add the chosen token (single line, no quotes needed — it is not JSON)
   printf 'OCTAVIUS_LR_API_KEY=%s\n' "$TOKEN" >> ~/.config/octavius/env
   chmod 600 ~/.config/octavius/env

   # 2. the unit reads this file via EnvironmentFile; no daemon-reload needed
   #    unless the drop-in itself changed
   systemctl --user restart octavius

   # 3. verify: the var must actually reach the process
   systemctl --user show octavius -p Environment | tr ' ' '\n' | grep -c OCTAVIUS_LR_API_KEY

   # 4. verify auth is live — after a turn that hits :8010 (a consult, a reader
   #    doc, or an image), this must stay empty and auth_failures must stay 0
   curl -s localhost:8030/health | jq '.llm_chain
     | {rejecting: .endpoints_rejecting_credentials, auth_failures}'
   ```

   Keep the `OCTAVIUS_LLM_API_KEYS` line in place: the bare var **wins** over the map (`settings._llm_api_keys`), so leaving the map is a working fallback, not a conflict. Delete it only after step 4 passes. Watch for two traps: a blank/whitespace value is ignored by design rather than sending `Bearer ` (there is a test), and **nothing loads a `.env` file** — a key put there is silently ignored.

6. Transcription/dictation mode (Dave, 2026-07-12): capture speech (Android app first), transcribe, and save the transcript to the `saved_items` stash — deliberately NOT the vault. No agent turn, no TTS. Revives the stash write path for non-note payloads; needs a WS message or REST route for STT-to-stash (the unwired `save_to_stash` helper in `local_tool_inbox.py` is a starting point). App-side sketch in `../octavius-android/docs/HANDOFF.md` NEXT WORK #3.

## Planned: `deep_research` domain via headless pi (2026-08-08)

Dave raised replacing the in-process subagent loop with `pi --mode json -p --no-session`
so specialist behavior isn't re-implemented in Octavius every time an MCP server changes.
Conclusion: **do it for a new async `deep_research` domain only; leave the inline quick
domains (email/tasks/research) on `subagent.py`.**

Why not for the inline domains:

- Warm MCP sessions are the whole latency story. `run_subagent` borrows the main
  process's already-connected `MCPManager` sessions. A headless pi call is a cold
  process: interpreter start + an MCP handshake per server, *per call*.
  `consult_specialist` already averages ~15 s (50 s worst) and is the dominant Matrix
  first-turn cost — process startup would land directly on voice latency.
- Shelling out bypasses `SubagentDispatcher`, so `:8010`'s `--parallel 3` capacity
  accounting is lost unless the ticket is re-wrapped around the subprocess (doable —
  the ticket is independent of the LLM call, see `run_inline_subagent`).
- The `===TOOL DATA===` block in `_compose_result` (verbatim tool observations, so the
  main agent lifts exact IDs rather than trusting paraphrased numbers) has no pi
  equivalent; it would mean parsing raw tool results out of pi's json event stream.
- Per-tool `status_callback` → UI status line would likewise need re-deriving from
  pi events.

Also worth recording, because it undercuts the original motivation: **MCP tool schemas
already flow through dynamically.** `mcp.get_tools_for_servers()` filters live session
tools, so a server adding or renaming a tool needs no Octavius change. What doesn't
auto-update is the per-domain system prompt — the evangeline `hybrid_search`
folder=null/`1970-01-01` defaults, the Vikunja `done=false` trap, the don't-search-twice
guard in `SUBAGENT_DOMAINS`. That is accumulated tuning, and moving to pi relocates it
rather than removing it.

Why it fits `deep_research`:

- The async delegation path is already built and reserved for exactly this:
  `delegate_task` / `pull_delegation` / `list_pending_delegations` / `cancel_delegation`,
  the "Agents at Work" badge, `spawn_delegation` / `_run_and_announce`, the
  `proactive_speak` setting, and the `delegation_*` WS messages all still exist and are
  merely unregistered. Re-enabling is one tool spec plus one registry line.
- Process startup is noise against a multi-minute research run.
- pi already has a parallel `deep_research` orchestrator, which is the part genuinely
  not worth reimplementing.

Sketch: a `deep_research` entry in the local tool specs → `spawn_delegation` →
`_run_and_announce` shells out to `pi --mode json -p --no-session`, parses the final
assistant `message_end`, and announces via the existing badge/pull lifecycle. Keep the
dispatcher ticket around the subprocess so long research runs still respect endpoint
capacity. Not started; no code written.

## Migration Note

- `reader_documents.saved_item_id` is still enforced as a plain foreign key. If inbox deletion should eventually null out that reference automatically, that will require a real SQLite migration to add `ON DELETE SET NULL`, not just a schema-file edit.

## Related Design Work

The Android companion app remains exploratory rather than committed implementation work. See `octavius-android-design.md` for that design thread.
