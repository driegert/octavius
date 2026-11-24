# Frontend

Read this before editing anything under `static/`, adding an external resource, or browser-verifying a UI change.

## Files

- `octavius.css` — shared tokens, base styles, components (Newsreader/Spline Sans, switch, slider, dropdown); `index.css` / `reader.css` / `inbox.css` — page layouts (desktop-first; the side panel becomes a drawer on phones).
- `app-common.js` — shared WS setup, HTML escaping, voice-list loading. Extend only for genuinely shared behaviour.
- `/` : `index.html`, `index-app.js` (controller: conversation sidebar, settings, transcript, `confirm_request` card, shortcuts, WS client), `index-audio.js` (streamed TTS queue, silence trimming, playback), `index-orb.js` (eight-lobed canvas orb driven by mic/playback levels; also play/pause on `/reader`), `voice-capture.js`.
- `voice-capture.js` — `OctaviusVoiceCapture.createCapture`: mic → 16 kHz Float32 PCM between `stt_start`/`stt_stop`. Used by both `index-app.js` and `reader-chat.js` (the duplicate copy in `index-app.js` was removed 2026-11-24).
- `/reader` : `reader.html`, `reader-app.js` (library, reading column with highlight and follow, Original/Reading view, resizable panels, edit panel, retry, shortcuts, playback), `reader-original.js` (`OctaviusReaderOriginal.render`: a chunk's `source_text` to a DocumentFragment via a private `marked` + `OctaviusMath`; raw HTML escaped except bare `sub`/`sup`/`br`/emphasis, Quarto `{#...}` attributes and `:::` fences dropped, images named not loaded), `reader-chat.js`.
- `/inbox` : `inbox.html`, `inbox-app.js` (list with search/filters, item view, item chat). Untrusted item markdown renders with raw HTML escaped, non-http(s)/mailto links neutralised, images never loaded.
- `math-render.js` — `OctaviusMath`: pulls LaTeX out before markdown runs (not from code) and typesets with KaTeX; used by `/`, the reader chat and the Original view. Android already rendered math.
- `manifest.json` — PWA manifest.
- Caching: `ui_security.install_ui_headers` sends `Cache-Control: no-cache` on the UI pages and `/static`, so browsers revalidate (ETag/304); no `?v=` cache-busting needed.

## Content-Security-Policy (since 2026-10-30)

`ui_security.py`'s `UI_CSP` / `UI_SECURITY_HEADERS` go on `/`, `/inbox`, `/reader` and on every other `text/html` response, so the same pages fetched as `/static/index.html` etc. are covered too (plus `nosniff`, `Referrer-Policy: same-origin`) — defence in depth for pages rendering untrusted text.

- **No `'unsafe-inline'` or `'unsafe-eval'`.** Never add inline `<script>`, `on*=` handlers, `style="..."` (in HTML, `innerHTML` templates, or `setAttribute('style')` — `el.style.x = ...` is fine), `eval`/`new Function`, or `javascript:` URLs.
- `script-src 'self'` only: third-party scripts are vendored in `static/vendor/` (`marked-15.0.12.min.js`, `katex-0.16.22/` with `katex.min.js`, `katex.min.css` and woff2 fonts — hence `font-src 'self'`), each the byte-identical npm/jsdelivr build with its `integrity` hash on the tag. Recompute the hash when bumping.
- **KaTeX: call `katex.render` into a node, never `renderToString`** — the string form carries `style="..."` attributes the policy blocks, and fractions and sizes then render wrong.
- A new external stylesheet, font, image or fetch origin must be added to the matching directive; `tests/test_main.py` checks the pages against the policy. Remote images in markdown are blocked by `img-src` on purpose; `/` and `/inbox` render them as links.
- **Browser-verify** with headless Chrome and a `securitypolicyviolation` listener. Playwright's `wait_for_function` needs eval and fails under this policy — poll with `page.evaluate`.

## `/` UI details worth knowing

- Model menu (pi brain, since 2026-11-04): shown in the header with the voice, hidden unless `enabled` and more than one entry; saved as `chatModel` in `octavius-prefs` and sent on every connect even if never picked (null = default), so `/` always gets `answered_by`. Each reply is captioned ("— Qwen3.8 27B"), in coral with "(X failed)" when a picked non-Auto model did not answer. `conversation_loaded` carries `answered_by` on assistant messages recorded as `pi:<model>` (native turns record the chain's first model, so they get none). Protocol: [pi-brain.md](pi-brain.md).
- The wordmark, title and placeholder follow the picked voice's agent name (Octavius/Octavja).
- `/`'s rail resizes like `/reader`'s (`railW` in `octavius-prefs`).
- Prefs keys: `octavius-prefs` (`/`: `chatModel`, `mathSpeed`, `railW`, voice), `octavius-reader-prefs` (`railW`, `chatW`).
