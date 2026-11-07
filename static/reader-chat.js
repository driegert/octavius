(function() {
  const $ = (id) => document.getElementById(id);

  const tab = $('chat-tab');
  const panel = $('chat-panel');
  const closeBtn = $('chat-close');
  const newBtn = $('chat-new');
  const docEl = $('chat-doc');
  const modelRow = $('chat-model-row');
  const modelSel = $('chat-model');
  const MODEL_KEY = 'octavius-reader-chat-model';
  let answeredBy = '';  // the model behind the reply being committed (doc_chat_answered_by)
  const logEl = $('chat-log');
  const statusEl = $('chat-status');
  const form = $('chat-form');
  const input = $('chat-input');
  const sendBtn = $('chat-send');
  const micBtn = $('chat-mic');

  const OPEN_KEY = 'octavius-reader-chat-open';

  let open = false;
  let controller = null;
  let ws = null;
  let connected = false;
  let openedDocId = null;    // the document the server-side chat is currently scoped to
  let loading = false;       // waiting for doc_chat_opened
  let recording = false;
  let busy = false;          // a turn is in flight (transcribing, thinking or speaking)
  let dropAudio = false;
  let acceptPartials = false;
  let liveUserEl = null;
  let streamingEl = null;
  let streamingText = '';
  // Open state from last visit; applied once a document is open (it may load after this script).
  let wantOpen = false;
  try { wantOpen = localStorage.getItem(OPEN_KEY) === '1'; } catch (_err) {}

  // ---------- Markdown (same sanitising as index-app.js) ----------

  if (typeof marked !== 'undefined') {
    marked.setOptions({ breaks: true, gfm: true });
    try {
      marked.use({
        renderer: {
          html(token) {
            const raw = typeof token === 'string' ? token : (token && (token.raw || token.text)) || '';
            return OctaviusApp.escapeHtml(raw);
          },
          // The CSP blocks remote images, so link them instead.
          image(token) {
            const href = (token && token.href) || '#';
            const label = (token && token.text) || 'image';
            return `<a href="${OctaviusApp.escapeHtml(href)}" target="_blank" rel="noopener noreferrer">[image: ${OctaviusApp.escapeHtml(label)}]</a>`;
          },
        },
        walkTokens(token) {
          if ((token.type === 'link' || token.type === 'image') && !/^(https?:|mailto:)/i.test(token.href || '')) {
            token.href = '#';
          }
        },
      });
    } catch (_err) {
    }
  }

  function renderMarkdown(text) {
    if (typeof marked !== 'undefined' && text) {
      try {
        return marked.parse(text);
      } catch (_err) {
      }
    }
    return OctaviusApp.escapeHtml(text || '').replace(/\n/g, '<br>');
  }

  // ---------- Citations ----------
  // The model cites paragraphs as [§14], [§3, §7] or [§2–5] (local_tool_reader_lookup.py).
  // Each number becomes a link that shows that paragraph without moving the place.

  const CITE = /\[§([^[\]]{0,40})\]/g;

  function citeLink(n) {
    const known = window.OctaviusReader && window.OctaviusReader.hasChunk(n);
    const title = known ? `Show paragraph ${n}` : 'Not a paragraph of this document';
    return `<button type="button" class="cite${known ? '' : ' cite-missing'}" data-chunk="${n}" title="${title}"${known ? '' : ' disabled'}>§${n}</button>`;
  }

  // On rendered HTML: the document text never reaches here, only the model's reply,
  // already escaped by marked; the tag's contents are digits by the time they are used.
  function linkCitations(html) {
    return html.replace(CITE, (whole, inner) => {
      const nums = (inner.match(/\d+/g) || []).map(Number);
      if (!nums.length || !/^[\s§\d,–\-and]*$/.test(inner)) return whole;
      return '<span class="cites">' + nums.map(citeLink).join('') + '</span>';
    });
  }

  function renderReply(text) {
    return linkCitations(renderMarkdown(text));
  }

  // ---------- Preferences (the main page's voice, not the reader's) ----------

  function mainPrefs() {
    try {
      return JSON.parse(localStorage.getItem('octavius-prefs')) || {};
    } catch (_err) {
      return {};
    }
  }

  function chatVoice() {
    return mainPrefs().voice || 'Delia';
  }

  function voicePrefs() {
    const prefs = mainPrefs();
    return (prefs.voices || {})[chatVoice()] || { speed: 1.4, trim: true, normalize: true };
  }

  // ---------- Audio ----------

  const audioPlayer = OctaviusVoiceAudio.createStreamingAudioPlayer({
    getPlaybackRate() { return voicePrefs().speed || 1.4; },
    getMathPlaybackRate() { return Number(mainPrefs().mathSpeed) || 1.2; },
    shouldTrimSilence() { return voicePrefs().trim !== false; },
    shouldNormalizeVolume() { return voicePrefs().normalize !== false; },
    onPlaybackStart() { refreshUi(); },
    onPlaybackIdle() { busy = false; refreshUi(); },
  });

  // ---------- Transcript ----------

  function nearBottom() {
    return logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 80;
  }

  function scrollToEnd(force) {
    if (force || nearBottom()) logEl.scrollTop = logEl.scrollHeight;
  }

  function addTurn(role, cls) {
    const el = document.createElement('div');
    el.className = `chat-turn ${role}${cls ? ' ' + cls : ''}`;
    logEl.appendChild(el);
    return el;
  }

  function showNote(text) {
    logEl.innerHTML = '';
    const el = document.createElement('p');
    el.className = 'chat-empty';
    el.textContent = text;
    logEl.appendChild(el);
  }

  function clearLog() {
    logEl.innerHTML = '';
    liveUserEl = null;
    streamingEl = null;
    streamingText = '';
  }

  function renderMessages(messages) {
    clearLog();
    for (const m of messages || []) {
      if (m.role === 'user') addTurn('user').textContent = m.content;
      else if (m.role === 'assistant') OctaviusMath.setMarkdown(addTurn('assistant'), m.content, renderReply);
    }
    if (!logEl.children.length) {
      showNote('Ask about this document. Type, or tap the mic and speak.');
    }
    scrollToEnd(true);
  }

  function dropEmptyNote() {
    const note = logEl.querySelector('.chat-empty');
    if (note) note.remove();
  }

  function dropLiveUser() {
    if (liveUserEl) { liveUserEl.remove(); liveUserEl = null; }
  }

  function showPartial(text) {
    if (!text || !acceptPartials) return;
    dropEmptyNote();
    if (!liveUserEl) liveUserEl = addTurn('user', 'live');
    liveUserEl.textContent = text;
    scrollToEnd();
  }

  function commitUser(text) {
    dropEmptyNote();
    acceptPartials = false;
    const el = liveUserEl || addTurn('user');
    el.classList.remove('live');
    el.textContent = text;
    liveUserEl = null;
    streamingEl = addTurn('assistant', 'pending');
    streamingText = '';
    busy = true;
    scrollToEnd(true);
  }

  function appendDelta(text) {
    if (!text) return;
    if (!streamingEl) streamingEl = addTurn('assistant');
    streamingEl.classList.remove('pending');
    streamingEl.classList.add('streaming');
    streamingText += text;
    OctaviusMath.setMarkdown(streamingEl, streamingText, renderReply);
    scrollToEnd();
  }

  function commitAssistant(text) {
    dropEmptyNote();
    const el = streamingEl || addTurn('assistant');
    el.classList.remove('pending', 'streaming');
    OctaviusMath.setMarkdown(el, text, renderReply);
    if (answeredBy) {
      const by = document.createElement('p');
      by.className = 'chat-by';
      by.textContent = '— ' + answeredBy;
      el.appendChild(by);
      answeredBy = '';
    }
    streamingEl = null;
    streamingText = '';
    scrollToEnd();
  }

  // A turn ended without a reply (error, empty, cancelled): drop the placeholder.
  function settlePending() {
    if (streamingEl && !streamingText) streamingEl.remove();
    if (streamingEl) streamingEl.classList.remove('pending', 'streaming');
    streamingEl = null;
    streamingText = '';
  }

  function setStatus(text) {
    statusEl.textContent = text || '';
  }

  // ---------- UI state ----------

  function hasDoc() {
    return !!(window.OctaviusReader && window.OctaviusReader.currentDoc());
  }

  function refreshUi() {
    const doc = hasDoc();
    const ready = doc && connected && !loading;
    tab.disabled = !doc;
    tab.title = doc ? 'Chat about this document (c)' : 'Open a document to chat about it';
    input.disabled = !ready;
    // The server drops a turn sent while one is running, so typed text waits in the box.
    sendBtn.disabled = !ready || busy || !input.value.trim();
    micBtn.disabled = !ready;
    newBtn.disabled = !ready;
    modelSel.disabled = !ready || busy;
    micBtn.classList.toggle('recording', recording);
    micBtn.setAttribute('aria-pressed', recording ? 'true' : 'false');
    micBtn.setAttribute('aria-label', recording ? 'Stop recording' : 'Speak your question');
    micBtn.title = recording ? 'Stop recording' : 'Speak your question';
    if (!recording) micBtn.style.setProperty('--level', '0');
  }

  logEl.addEventListener('click', (event) => {
    const cite = event.target.closest('button.cite');
    if (!cite || cite.disabled || !window.OctaviusReader) return;
    if (!window.OctaviusReader.peek(Number(cite.dataset.chunk))) return;
    // On a phone the panel covers the page: get out of the way (it reopens with the tab).
    if (window.matchMedia('(max-width: 860px)').matches) setOpen(false, false);
  });

  function setOpen(next, remember) {
    open = next;
    panel.hidden = !open;
    tab.hidden = open;
    tab.setAttribute('aria-expanded', open ? 'true' : 'false');
    document.body.classList.toggle('chat-open', open);
    if (remember !== false) {
      try { localStorage.setItem(OPEN_KEY, open ? '1' : '0'); } catch (_err) {}
    }
    if (open) {
      ensureSocket();
      refreshUi();
      if (!input.disabled) input.focus();
    } else {
      stopEverything();
      if (hasDoc()) tab.focus();
    }
  }

  function stopEverything() {
    capture.cancel();
    recording = false;
    audioPlayer.stop();
    refreshUi();
  }

  // ---------- Socket ----------

  function send(msg) {
    if (!ws || ws.readyState !== WebSocket.OPEN) return false;
    ws.send(JSON.stringify(msg));
    return true;
  }

  // ---------- Model menu ----------

  function savedModel() {
    try { return localStorage.getItem(MODEL_KEY) || ''; } catch (_err) { return ''; }
  }

  function fillModels(models, current) {
    if (!Array.isArray(models) || !models.length) { modelRow.hidden = true; return; }
    modelSel.replaceChildren(...models.map((m) => {
      const opt = document.createElement('option');
      opt.value = String(m.id);
      opt.textContent = String(m.label || m.id);
      return opt;
    }));
    if (current) modelSel.value = current;
    modelRow.hidden = false;
  }

  modelSel.addEventListener('change', () => {
    try { localStorage.setItem(MODEL_KEY, modelSel.value); } catch (_err) {}
    send({ type: 'doc_chat_model', model: modelSel.value });
  });

  function sendDocOpen(fresh) {
    const doc = window.OctaviusReader && window.OctaviusReader.currentDoc();
    if (!doc) return;
    openedDocId = doc.id;
    loading = true;
    clearLog();
    setStatus('Opening chat…');
    docEl.textContent = doc.title || '';
    const msg = { type: 'doc_chat_open', doc_id: doc.id };
    if (fresh) msg.new = true;
    const model = savedModel();
    if (model) msg.model = model;
    send(msg);
    refreshUi();
  }

  function ensureSocket() {
    if (controller) return;
    controller = OctaviusApp.createWebSocket({
      binaryType: 'arraybuffer',
      heartbeat: true,
      onOpen(_evt, socket) {
        ws = socket;
        connected = true;
        send({ type: 'settings', voice: chatVoice(), tts: false });
        if (hasDoc()) sendDocOpen(false);
        else { loading = false; setStatus(''); refreshUi(); }
      },
      onMessage(evt) {
        ws = controller.getSocket();
        if (evt.data instanceof ArrayBuffer) {
          const isMath = nextClipIsMath;
          nextClipIsMath = false;
          if (dropAudio) return;
          audioPlayer.enqueueAudio(evt.data, isMath);
          return;
        }
        if (typeof evt.data !== 'string') return;
        let msg;
        try { msg = JSON.parse(evt.data); } catch (_err) { return; }
        if (msg && typeof msg === 'object') handleMessage(msg);
      },
      onClose() {
        connected = false;
        loading = false;
        busy = false;
        capture.cancel();
        recording = false;
        setStatus('Disconnected. Reconnecting…');
        refreshUi();
      },
    });
    ws = controller.connect();
  }

  function handleStatus(text) {
    if (text === 'audio_done') {
      audioPlayer.signalDone();
      settlePending();
      busy = false;
      setStatus('');
    } else if (/^Couldn't hear anything/.test(text)) {
      dropLiveUser();
      busy = false;
      setStatus(text);
    } else if (text === 'Ready') {
      setStatus('');
    } else if (!recording) {
      setStatus(text);
    }
    refreshUi();
  }

  let nextClipIsMath = false; // speech_clip: the next audio frame is a formula

  function handleMessage(msg) {
    switch (msg.type) {
      case 'doc_chat_opened':
        if (msg.doc_id !== openedDocId) return;
        loading = false;
        docEl.textContent = msg.title || docEl.textContent;
        renderMessages(msg.messages);
        fillModels(msg.models, msg.model);
        setStatus('');
        refreshUi();
        if (open && !input.disabled && !recording) input.focus();
        break;
      case 'doc_chat_model':
        if (msg.doc_id === openedDocId && msg.model) modelSel.value = msg.model;
        break;
      case 'doc_chat_answered_by':
        answeredBy = String(msg.label || msg.model || '');
        break;
      case 'doc_chat_error':
        if (msg.doc_id != null && msg.doc_id !== openedDocId) return;
        loading = false;
        setStatus(String(msg.error || 'Chat error'));
        refreshUi();
        break;
      case 'status':
        handleStatus(String(msg.text || ''));
        break;
      case 'speech_clip':
        nextClipIsMath = msg.math === true;
        break;
      case 'stt_auto_stop':
        // Server heard the end of speech: finish our side of the capture.
        capture.cancel();
        recording = false;
        setStatus('Transcribing…');
        refreshUi();
        break;
      case 'transcript_partial':
        showPartial(msg.text);
        break;
      case 'transcript':
        dropAudio = false;
        commitUser(msg.text);
        refreshUi();
        break;
      case 'response_delta':
        appendDelta(msg.text);
        break;
      case 'response':
        commitAssistant(msg.text);
        break;
      default:
        // session_id, confirm_request, delegation_* and the rest are not for this panel.
        break;
    }
  }

  // ---------- Turns ----------

  // Everything that must happen before a turn: note where Dave is reading (before pausing,
  // since pausing clears the heard place), stop the reading audio, and pick silent or spoken.
  // "Here" for the next question: Dave's selection, else the marked sentence (read
  // before pausing, which would otherwise leave it as is anyway).
  function beginTurn(spoken) {
    const place = window.OctaviusReader.place();
    window.OctaviusReader.pause();
    audioPlayer.stop();
    send({ type: 'settings', voice: chatVoice(), tts: spoken });
    if (place && place.doc_id != null && place.doc_id === openedDocId) {
      const frame = {
        type: 'doc_chat_position',
        doc_id: place.doc_id,
        chunk_index: place.chunk_index,
        sentence_index: place.sentence_index,
      };
      if (place.end_chunk_index != null) {
        frame.end_chunk_index = place.end_chunk_index;
        frame.end_sentence_index = place.end_sentence_index;
      }
      send(frame);
    }
  }

  const aboutEl = $('chat-about');
  const aboutText = $('chat-about-text');
  function refreshAbout() {
    const place = window.OctaviusReader ? window.OctaviusReader.place() : {};
    const sel = place && place.end_chunk_index != null && place.text ? place.text.replace(/\s+/g, ' ').trim() : '';
    aboutEl.hidden = !sel;
    aboutText.textContent = sel ? 'About your selection: \u201c' + sel + '\u201d' : '';
  }
  document.addEventListener('selectionchange', refreshAbout);
  $('chat-about-clear').addEventListener('click', () => {
    window.OctaviusReader.clearSelection();
    refreshAbout();
    if (!input.disabled) input.focus();
  });

  function submitText() {
    const text = input.value.trim();
    if (!text || input.disabled || recording || busy) return;
    dropAudio = false;
    beginTurn(false);
    if (send({ type: 'text_input', text })) {
      busy = true;  // before the transcript echo too, so a quick second Enter waits
      input.value = '';
      autosize();
      setStatus('Sending…');
    }
    refreshUi();
  }

  const capture = OctaviusVoiceCapture.createCapture({
    send(frame) {
      if (!ws || ws.readyState !== WebSocket.OPEN) return false;
      if (frame instanceof Uint8Array) ws.send(frame);
      else ws.send(JSON.stringify(frame));
      return true;
    },
    onLevel(level) { micBtn.style.setProperty('--level', String(level.toFixed(2))); },
    onError() {
      recording = false;
      setStatus('Microphone access denied. Allow it in the browser to talk.');
      refreshUi();
    },
  });

  async function toggleMic() {
    if (micBtn.disabled) return;
    if (recording) {
      if (capture.stop()) setStatus('Transcribing…');
      recording = false;
      refreshUi();
      return;
    }
    // Opening the mic mid-reply interrupts it (the server cancels the turn on stt_start).
    dropAudio = true;
    beginTurn(true);
    acceptPartials = true;
    recording = true;
    setStatus('Recording…');
    refreshUi();
    const started = await capture.start();
    if (!started) recording = false;
    else dropAudio = false;
    refreshUi();
  }

  // ---------- Wiring ----------

  function autosize() {
    input.style.height = 'auto';
    input.style.height = `${Math.min(input.scrollHeight, 140)}px`;
  }

  tab.addEventListener('click', () => { if (!tab.disabled) setOpen(true); });
  closeBtn.addEventListener('click', () => setOpen(false));
  newBtn.addEventListener('click', () => {
    if (newBtn.disabled) return;
    stopEverything();
    sendDocOpen(true);
  });
  micBtn.addEventListener('click', toggleMic);
  form.addEventListener('submit', (e) => { e.preventDefault(); submitText(); });
  input.addEventListener('input', () => { autosize(); refreshUi(); });
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      submitText();
    }
  });
  function isTypingTarget(el) {
    if (!el) return false;
    const tag = el.tagName;
    return tag === 'TEXTAREA' || tag === 'SELECT' || el.isContentEditable ||
      (tag === 'INPUT' && !['checkbox', 'radio', 'range', 'button'].includes(el.type));
  }

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && open) {
      e.preventDefault();
      setOpen(false);
      return;
    }
    // c: chat, as in the Inbox. Opens the panel, or puts the cursor in it if it is open.
    if (e.key !== 'c' || e.metaKey || e.ctrlKey || e.altKey) return;
    if (!hasDoc() || isTypingTarget(document.activeElement)) return;
    e.preventDefault();
    if (!open) setOpen(true);
    else if (!input.disabled) input.focus();
  });

  if (window.OctaviusReader) {
    window.OctaviusReader.onDocChange(() => {
      const doc = window.OctaviusReader.currentDoc();
      if (doc) docEl.textContent = doc.title || '';
      if (!doc) {
        // Document closed: the chat has nothing to be about.
        stopEverything();
        openedDocId = null;
        loading = false;
        docEl.textContent = '';
        showNote('Open a document to chat about it.');
        if (open) setOpen(false, false);
      } else if (wantOpen && !open) {
        // Restoring the remembered open state, now that a document is there.
        wantOpen = false;
        setOpen(true);
      } else if (connected && doc.id !== openedDocId) {
        stopEverything();
        sendDocOpen(false);
      }
      refreshUi();
    });
  }

  showNote('Open a document to chat about it.');
  if (wantOpen && hasDoc()) { wantOpen = false; setOpen(true); }
  refreshUi();
})();
