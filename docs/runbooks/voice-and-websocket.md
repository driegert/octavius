# Voice, WebSocket protocol and TTS

Read this before changing the `/ws` protocol, STT/VAD/`audio_done` behaviour, talk modes, TTS or spoken-text normalisation. The PWA and the Android client both depend on it.

## Session handling

- `/ws` → `websocket_session.handle_websocket_session` → `WebSocketSessionHandler` (core: dispatch, restore/attach/reset/load, text input, turns), composed from mixins: `ws_stt.py` (audio, `stt_*`), `ws_reader.py` (reader playback), `ws_doc_chat.py` (Reader chat + agent model menu), `ws_media.py` (`image_input`/`file_input`), `ws_item_chat.py` (Inbox item chat), `ws_delegation.py` (inline consults, dormant async delegation, proactive speech). `ws_common.py` holds the shared `WebSocket`/`WebSocketDisconnect` imports. Each module logs under its own name (`ws_stt`, `ws_media`, …), so grep the journal by that.
- Each connection gets its own `Conversation`. Conversation IDs live in browser `localStorage` and are restored with `restore_session`.
- **A reconnect continues the same record** (since 2026-11-13): `restore_session` retires the empty connect-time record and resumes the old one (`HistoryRecorder.resume_conversation`), so `session_id` returns the id the client sent. `load_conversation` does the same for a voice conversation and follows `conversation_loaded` with a `session_id` naming the record new turns go to. Only `service='octavius'`, `source='voice'` records are resumed; a loaded Matrix thread, Reader chat or item chat is read into memory and new turns go to a new record. An id with no messages gets no reply. Ending a resumed record with no new turn rewrites nothing; with new turns, the summary covers its last 40 earlier messages plus the new ones. Two connections may hold the same id (phone and PWA); each sees the other's turns only after its next restore.
- WS disconnects arrive as a `websocket.disconnect` **message**; calling `receive()` again raises `RuntimeError`. `run` breaks on the message itself (reaching cleanup through the RuntimeError chained tracebacks into every cleanup warning).
- Every turn path must end with `status: audio_done` (`run_turn`'s `finally`; rejected media frames send their own), or clients' turn state sticks.

## Turn behaviour

- The agent buffers sentences during tool-call rounds and only emits spoken text once tool use is done. Rounds are capped at `OCTAVIUS_MAX_TOOL_ROUNDS` (7) with a nudge to stop from round 5-6. Tool results are truncated to 4000 chars. Qwen `<think>...</think>` is stripped before display or TTS. History trims to 40 messages.
- **Channel-aware style**: `stream_agent_turn(source=...)` folds a per-turn directive into `messages[0]` (same mechanism as the memory block): `voice` → short, spoken, no markdown; every other source (`text`/`matrix`/`image`/`file`/`inbox_chat`) → light markdown, complete answers. Base `settings.system_prompt` is channel-neutral; `VOICE_STYLE`/`TEXT_STYLE` in `agent.py` are the knobs. Default `voice`. The prompt is in memory, so a change needs a restart.
- **Barge-in**: `handle_stt_start` cancels any in-flight `turn_task` (LLM stream + TTS). A no-op in normal flows; Android uses it for "interrupt while speaking" (energy-gated detector during playback).

## STT and VAD

- **STT**: faster-whisper large-v3, int8_float16, CUDA, on **triplestuffed** (moved from lilripper 2026-11-03), listening on `127.0.0.1:8502`; Caddy exposes `triplestuffed:8552`. `OCTAVIUS_STT_URL` defaults to `http://127.0.0.1:8502/api/transcribe`; running Octavius on another host means `http://triplestuffed:8552/api/transcribe`. No STT failover.
- **Streaming**: the client sends 16 kHz Float32 LE PCM every 250 ms between `stt_start`/`stt_stop` (browser: `static/voice-capture.js`). The server buffers, runs background Whisper, and sends `transcript_partial`. On stop (manual or VAD), it waits up to `STT_FINAL_WAIT_S` (3 s) for an in-flight run — the latest partial usually lags one run and misses the last words — and transcribes once more only if VAD heard speech past what the text covers. No partial is sent after stop (it used to show as a "ghost" line under the reply); `StreamingSTTState.generation` keeps one capture's run out of the next.
- **VAD**: Silero VAD v6 ONNX (`models/silero_vad.onnx`, CPU, onnxruntime; `vad.py` `SileroVAD`, one per session). Each 512-sample window (32 ms) gets a 64-sample context prefix; LSTM state is carried between chunks and reset per turn. Auto-stop after `VAD_SILENCE_SECONDS` (1.5 s) of silence **after speech**. If the model file is missing, VAD is skipped and auto-stop does not work.

**The Android dependency** (`../octavius-android`, Kotlin/Compose foreground service, its own `CLAUDE.md`; an independent client of the same `/ws`): pure silence never auto-stops, and an empty transcription sends **no `audio_done`** (just a "Couldn't hear anything" status). Its continuous-conversation loop and silence watchdog are built on exactly this (wake word shelved 2026-07-11; it holds the mic only during a capture or conversation). Change it and the PWA *and* Android change together.

## Talk modes (web `/`)

Hold to talk (default), tap to talk, and continuous conversation (VAD auto-stops, Octavius replies, the mic reopens; "End Conversation" exits). **One turn** (`T` or the dock button, any mode): a pause ends capture, the reply plays, the mic stays closed; pressing again, Space or the orb sends early, `Esc` cancels; during hands-free it makes the current turn the last. Pure silence never auto-stops, so with nothing said the mic stays open until a key.

## WebSocket message families

- **Voice**: `status`, `transcript`, `transcript_partial`, `response`, `reset`, `restore_session`, `session_id`, `load_conversation`, `conversation_loaded`, `stt_start`, `stt_stop`, `stt_auto_stop`; `typing_start` (warms pi; no client sends it yet).
- **Text streaming** (server→client): `response_delta` — one sentence as it streams, before the authoritative `response`. The Matrix sidecar edits its message from these; the browser ignores it.
- **Media** (client→server): `image_input`, `file_input` — frozen contract `docs/ws-media-contract.md`; see [media-turns.md](media-turns.md).
- **Reader playback**: `reader_play`, `reader_pause`, `reader_stop`, `reader_position`, `reader_audio_done`, `reader_heard`; see [reader.md](reader.md).
- **Speech clips** (server→client, since 2026-11-07): `speech_clip {math: true}` — the next binary frame is a large formula (see below).
- **Item chat**: `item_chat`, `item_chat_load`, `item_chat_reset`, `item_chat_response`, `item_chat_loaded`, `item_chat_status`.
- **Reader chat**: `doc_chat_open {doc_id, new?}` → `doc_chat_opened {doc_id, title, conversation_id, messages}` or `doc_chat_error {doc_id, error}`; `doc_chat_position {doc_id, chunk_index, sentence_index[, end_chunk_index, end_sentence_index]}`; `doc_chat_model {model}` → `doc_chat_model {doc_id, model}`; server `doc_chat_answered_by {model, label}` before a reply's `response`.
- **Agent model menu**: `chat_model`, `answered_by` — see [pi-brain.md](pi-brain.md).
- **Confirmation** (pi only): `confirm_request` (`title`, `message`), sent with the spoken question and an `audio_done`; the next transcript/`text_input` answers.
- **Delegations** (DORMANT, not emitted): `delegation_update`, `delegation_removed` (server→client); `delegation_list`, `delegation_pull` (mode=merge|new), `delegation_dismiss` (client→server).

## TTS (Phonon)

- **Phonon 31m** at `lilripper:8881/v1/audio/speech` (CPU, open) is the only TTS since 2026-10-31 (Voxtral retired from the code then, with its voices and circuit breaker). Env: `OCTAVIUS_TTS_URL`, `OCTAVIUS_TTS_MODEL`, `OCTAVIUS_TTS_FORMAT`, `OCTAVIUS_TTS_VOICE` (server/reader default `Imogen`), `OCTAVIUS_TTS_AGENT_VOICE` (agent default on `/` until Dave picks one, `Delia`), `OCTAVIUS_TTS_VOICES` (16 Phonon voices), `OCTAVIUS_TTS_FEMALE_VOICES` (8 of those); code defaults match `~/.config/octavius/env`.
- A voice not in `OCTAVIUS_TTS_VOICES` is sent as `OCTAVIUS_TTS_VOICE`: Phonon **400s on unknown voices** and the sentence plays silent (`TTS failed for chunk`), so list only names `GET /v1/audio/voices` returns.
- **The agent's name follows its voice**: Octavja in a female voice, Octavius in a male one (`TTSSettings.agent_name`; `/api/voices` returns `agent_names`). Both system prompts (native and pi_harness's octavius profile) only say it answers to either name.
- Latency: ~0.09 s + ~0.011 s/word per request (p50 0.27-0.31 s, p90 0.4 s over 1-31 words, 2026-10-30) vs Kokoro-GPU ~0.07 s; later sentences synth while earlier ones play, so only the first sentence pays.
- **Rollback**: Kokoro is still up on `lilripper:8880`: set `OCTAVIUS_TTS_URL` to it, `_MODEL=kokoro`, and the voice vars to Kokoro names, then restart.

## Spoken text

- `tts.synthesize` runs every string through `speechify` (the single choke point for main voice, proactive, item chat and reader): strips `**bold**`/`*italic*`/`` `code` ``/links/headings/line-leading list markers, and ORPHAN emphasis left when a span splits across streamed sentences (the "asterisk asterisk" bug), while preserving `3 * 4` and `foo_bar`. Lighter than `reader_text.clean_for_speech`. It also drops reader `[§N]` citation tags and respells pi as "pie". Pronunciation notes: `docs/pronunciation.md`.
- **LaTeX on screen, words in the voice** (since 2026-11-07): `VOICE_MATH_STYLE`/`TEXT_MATH_STYLE` in `agent.py` ask for LaTeX unless the client cannot show it (a Matrix thread, `state.renders_math` False, set on attach; the Inbox). Text to clients/history keeps it; `run_turn` hands TTS `tts.speakable(sentence)`, which rewrites only sentences holding math through the reader's `_llm_convert_math` (0.1-0.4 s on workhorse's 12B). Past `SPEECH_MATH_TIMEOUT_S` (4) it uses local `strip_latex` and skips the LLM for 60 s. `breaks_outside_math` keeps both sentence splitters out of equations. Phrasing: `MATH_TO_SPEECH_PROMPT` (commas after "the integral"/"the sum", after the limits and before the differential are the pauses Dave asked for; Phonon gives a comma ~0.3 s and has no pause control or SSML); `speakable` forces "zed" in code.
- **Formula clips**: `tts.speech_audio` makes a large formula (`$$`, an integral, sum, product, fraction or root, or 15+ chars) its own clip with shortened join silence, announced by `speech_clip {math: true}`, so `/` and `/reader`'s chat can play it at "Formula speed" (`mathSpeed` in `octavius-prefs`, default 1.2, never faster than speaking speed). A client that ignores the frame plays every clip at one speed.

## Caddy note

A client that stalls without dying (phone in Doze) used to leak one CLOSE_WAIT socket to `127.0.0.1:8030` per stall in Caddy. Mitigated with `stream_timeout 24h` in the octavius `reverse_proxy` block (safe: app and PWA auto-reconnect); a Caddy restart clears any backlog.
