(function() {
  const $ = (id) => document.getElementById(id);
  const esc = (v) => OctaviusApp.escapeHtml(v);

  const shell = $('shell');
  const docList = $('doc-list');
  const ingestForm = $('ingest-form');
  const ingestInput = $('ingest-input');
  const addMsg = $('add-msg');
  const pasteToggle = $('paste-toggle');
  const pasteBar = $('paste-bar');
  const pasteInput = $('paste-input');
  const pasteTitle = $('paste-title');
  const pasteSubmit = $('paste-submit');
  const pasteCancel = $('paste-cancel');
  const searchInput = $('doc-search');
  const statusFilter = $('status-filter');
  const playerTitle = $('player-title');
  const playerEdit = $('player-edit');
  const connEl = $('conn-state');
  const pageEl = $('page');
  const emptyEl = $('empty-state');
  const docStateEl = $('doc-state');
  const noticeEl = $('read-along');
  const sectionsEl = $('sections');
  const followBtn = $('follow-btn');
  const progressBar = $('progress-bar');
  const progressCur = $('progress-current');
  const progressTot = $('progress-total');
  const statusEl = $('status');
  const whereEl = $('where');
  const playPauseBtn = $('play-pause-btn');
  const skipBack = $('skip-back');
  const skipFwd = $('skip-fwd');
  const stepBack = $('step-back');
  const stepFwd = $('step-fwd');
  const speedSlider = $('reader-speed');
  const speedVal = $('reader-speed-val');
  const voiceSelect = $('reader-voice');
  const editBar = $('edit-bar');
  const editTitleInput = $('edit-title');
  const editAppendInput = $('edit-append');
  const editReplaceCheckbox = $('edit-replace');
  const editError = $('edit-error');
  const editHint = $('edit-hint');
  const editCancelBtn = $('edit-cancel');
  const editSaveBtn = $('edit-save');
  const railOpen = $('rail-open');
  const railClose = $('rail-close');
  const scrim = $('scrim');
  const railTabs = $('rail-tabs');
  const tabLibrary = $('tab-library');
  const tabContents = $('tab-contents');
  const paneLibrary = $('pane-library');
  const paneContents = $('pane-contents');
  const tocDoc = $('toc-doc');
  const tocTree = $('toc-tree');

  let ws = null;
  let connected = false;
  let docs = [];
  let filter = 'all';
  let query = '';

  let currentDocId = null;
  let currentDoc = null;
  let isPlaying = false;
  let finished = false;
  let stopped = false;
  let currentChunk = 0;
  let currentSentence = 0;
  let totalSentences = 0;
  let currentAudio = null;
  const audioQueue = [];
  const positionQueue = [];
  let pollTimer = null;
  let docPollTimer = null;
  let playSeqId = 0;
  let audioEpochArmed = false;
  let seekDebounce = null;
  let editDocId = null;
  let editOrigTitle = '';
  let editPollTimer = null;

  // Rendered sentences of the open document, keyed "chunk:sentence".
  let sentenceEls = new Map();
  let markedEl = null;
  let follow = true;

  // Table of contents for the open document.
  let tocLinks = new Map(); // section index -> link element
  let tocCurrent = null;
  let railTab = 'library';

  // ---------- Preferences ----------

  function loadPrefs() {
    try {
      return JSON.parse(localStorage.getItem('octavius-reader-prefs')) || {};
    } catch (_err) {
      return {};
    }
  }
  function savePrefs(patch) {
    try {
      localStorage.setItem('octavius-reader-prefs', JSON.stringify(Object.assign(loadPrefs(), patch)));
    } catch (_err) {
    }
  }

  const prefs = loadPrefs();
  if (prefs.speed) speedSlider.value = prefs.speed;
  speedVal.textContent = parseFloat(speedSlider.value).toFixed(1) + 'x';

  // ---------- Playback level for the orb ----------

  let audioCtx = null;
  let analyser = null;
  let levelBuf = null;

  function wakeAudioContext() {
    // Called from a user gesture (play) so the context is allowed to run.
    try {
      if (!audioCtx) audioCtx = new AudioContext();
      if (audioCtx.state === 'suspended') audioCtx.resume().catch(() => {});
    } catch (_err) {
    }
  }

  function attachAnalyser(audio) {
    // Only route through the context when it is running; otherwise the
    // element plays directly and the orb simply doesn't follow the level.
    try {
      if (!audioCtx || audioCtx.state !== 'running') return;
      if (!analyser) {
        analyser = audioCtx.createAnalyser();
        analyser.fftSize = 1024;
        analyser.connect(audioCtx.destination);
        levelBuf = new Float32Array(analyser.fftSize);
      }
      audioCtx.createMediaElementSource(audio).connect(analyser);
    } catch (_err) {
    }
  }

  function playbackLevel() {
    if (!analyser || !currentAudio || currentAudio.paused) return 0;
    analyser.getFloatTimeDomainData(levelBuf);
    let sum = 0;
    for (let i = 0; i < levelBuf.length; i++) sum += levelBuf[i] * levelBuf[i];
    return Math.min(1, Math.sqrt(sum / levelBuf.length) * 5);
  }

  const orb = OctaviusOrb.create($('orb-canvas'), {
    core: false,
    getLevel(state) {
      return state === 'speaking' ? playbackLevel() : 0;
    },
  });

  // ---------- Transport UI ----------

  function playable() {
    return !!(currentDoc && currentDoc.status === 'ready' && totalSentences > 0);
  }

  function refreshTransport() {
    const canPlay = connected && playable();
    playPauseBtn.disabled = !canPlay;
    playPauseBtn.dataset.playing = String(isPlaying);
    playPauseBtn.setAttribute('aria-label', isPlaying ? 'Pause' : 'Play');
    playPauseBtn.title = isPlaying ? 'Pause (Space)' : 'Play (Space)';
    [skipBack, skipFwd, stepBack, stepFwd].forEach((b) => { b.disabled = !playable(); });
    progressBar.disabled = !playable();

    let state = 'disabled';
    let text = 'No document open';
    let tone = '';
    if (!connected) {
      state = 'offline';
      text = 'Disconnected. Reconnecting…';
      tone = 'error';
    } else if (currentDoc && currentDoc.status === 'processing') {
      state = 'thinking';
      text = 'Converting…';
    } else if (currentDoc && currentDoc.status === 'failed') {
      text = 'Conversion failed';
      tone = 'error';
    } else if (isPlaying && currentAudio && !currentAudio.paused) {
      state = 'speaking';
      text = 'Reading';
      tone = 'playing';
    } else if (isPlaying) {
      state = 'thinking';
      text = 'Starting…';
      tone = 'playing';
    } else if (playable()) {
      state = 'idle';
      if (finished) text = 'Finished';
      else if (stopped) text = 'Stopped';
      else if (currentChunk > 0 || currentSentence > 0) text = 'Paused';
      else text = 'Ready';
    }
    orb.setState(state);
    statusEl.textContent = text;
    statusEl.dataset.tone = tone;
  }

  function sectionHeading(chunkIdx) {
    const sections = (currentDoc && currentDoc.sections) || [];
    let heading = '';
    for (const sec of sections) {
      if (sec.index > chunkIdx) break;
      if (sec.heading) heading = sec.heading;
    }
    return heading;
  }

  function globalIndex(chunkIdx, sentIdx) {
    const sections = (currentDoc && currentDoc.sections) || [];
    let total = 0;
    for (const sec of sections) {
      if (sec.index < chunkIdx) total += sec.sentence_count;
      else if (sec.index === chunkIdx) return total + sentIdx;
    }
    return total;
  }

  function showProgress(globalPos) {
    progressBar.max = totalSentences;
    progressBar.value = globalPos;
    progressCur.textContent = totalSentences ? Math.min(globalPos + 1, totalSentences) : 0;
    progressTot.textContent = totalSentences;
    whereEl.textContent = currentDoc ? sectionHeading(currentChunk) : '';
  }

  // ---------- Read-along highlight ----------

  function markSentence(chunkIdx, sentIdx, scroll) {
    if (markedEl) markedEl.classList.remove('active', 'cursor');
    markedEl = sentenceEls.get(chunkIdx + ':' + sentIdx) || null;
    updateTocCurrent(chunkIdx);
    if (!markedEl) return;
    markedEl.classList.add(isPlaying ? 'active' : 'cursor');
    if (scroll && follow) {
      markedEl.scrollIntoView({ block: 'center', behavior: scroll === 'instant' ? 'auto' : 'smooth' });
    }
  }

  function userScrolled() {
    if (!isPlaying || !markedEl) return;
    follow = false;
    followBtn.hidden = false;
  }
  pageEl.addEventListener('wheel', userScrolled, { passive: true });
  pageEl.addEventListener('touchmove', userScrolled, { passive: true });
  followBtn.addEventListener('click', () => {
    follow = true;
    followBtn.hidden = true;
    if (markedEl) markedEl.scrollIntoView({ block: 'center', behavior: 'smooth' });
  });

  function setNotice(text, tone) {
    noticeEl.textContent = text || '';
    noticeEl.dataset.tone = tone || '';
    noticeEl.hidden = !text;
  }

  // ---------- Audio queue (paired with reader_position frames) ----------

  function enqueuePosition(pos) {
    positionQueue.push(pos);
  }

  function enqueueAudio(arrayBuffer) {
    audioQueue.push(arrayBuffer);
    if (audioQueue.length === 1 && !currentAudio) playNextAudio();
  }

  function applyPosition(pos) {
    if (!pos) return;
    currentChunk = pos.chunk_index;
    currentSentence = pos.sentence_index;
    totalSentences = pos.total_sentences;
    progressBar.max = totalSentences;
    progressBar.value = pos.sentence_global;
    progressCur.textContent = pos.sentence_global + 1;
    progressTot.textContent = totalSentences;
    whereEl.textContent = pos.heading || sectionHeading(pos.chunk_index);
    markSentence(pos.chunk_index, pos.sentence_index, true);
    // Keep the open document's row current; after reader_audio_done the queued
    // tail must not pull "Finished" back down.
    if (!finished) setDocProgress(currentDocId, pos.sentence_global, pos.total_sentences);
  }

  async function playNextAudio() {
    if (audioQueue.length === 0) {
      currentAudio = null;
      if (finished && !isPlaying) {
        if (markedEl) markedEl.classList.remove('active');
        followBtn.hidden = true;
      }
      refreshTransport();
      return;
    }
    const arrayBuffer = audioQueue.shift();
    const pos = positionQueue.shift();
    const speed = parseFloat(speedSlider.value);

    applyPosition(pos);

    const blob = new Blob([arrayBuffer], { type: 'audio/wav' });
    const url = URL.createObjectURL(blob);
    const audio = new Audio(url);
    audio.preservesPitch = true;
    audio.playbackRate = speed;
    currentAudio = audio;
    attachAnalyser(audio);

    audio.onplaying = refreshTransport;
    audio.onended = () => {
      URL.revokeObjectURL(url);
      if (currentAudio === audio) currentAudio = null;
      playNextAudio();
    };
    audio.onerror = () => {
      URL.revokeObjectURL(url);
      if (currentAudio === audio) currentAudio = null;
      playNextAudio();
    };
    audio.play().catch(() => playNextAudio());
  }

  function clearAudioQueue() {
    audioQueue.length = 0;
    positionQueue.length = 0;
    audioEpochArmed = false;
    if (currentAudio) {
      currentAudio.pause();
      currentAudio = null;
    }
  }

  // ---------- WebSocket ----------

  function connectWS() {
    const socketController = OctaviusApp.createWebSocket({
      binaryType: 'arraybuffer',
      heartbeat: true,
      onOpen(_evt, socket) {
        ws = socket;
        connected = true;
        connEl.dataset.state = 'open';
        connEl.textContent = 'Connected';
        refreshTransport();
      },
      onMessage(evt) {
        ws = socketController.getSocket();
        if (evt.data instanceof ArrayBuffer) {
          if (!isPlaying || !audioEpochArmed) return;
          audioEpochArmed = false;
          enqueueAudio(evt.data);
          return;
        }
        let msg;
        try {
          msg = JSON.parse(evt.data);
        } catch (_err) {
          return;
        }
        if (!msg || typeof msg !== 'object') return;
        if (msg.seq !== undefined && msg.seq !== playSeqId) {
          audioEpochArmed = false;
          return;
        }
        if (msg.type === 'reader_position') {
          audioEpochArmed = true;
          enqueuePosition(msg);
        }
        if (msg.type === 'reader_audio_done') {
          isPlaying = false;
          finished = true;
          if (totalSentences) setDocProgress(currentDocId, totalSentences, totalSentences);
          refreshTransport();
        }
        if (msg.type === 'status' && isPlaying && /not ready|not found/i.test(String(msg.text || ''))) {
          isPlaying = false;
          setNotice(String(msg.text), 'error');
          refreshTransport();
        }
      },
      onClose() {
        connected = false;
        connEl.dataset.state = 'closed';
        connEl.textContent = 'Offline';
        if (isPlaying) {
          isPlaying = false;
          clearAudioQueue();
        }
        refreshTransport();
      },
    });
    ws = socketController.connect();
  }

  function sendPlay(chunkIdx, sentIdx) {
    if (!ws || ws.readyState !== WebSocket.OPEN || !currentDocId) return;
    wakeAudioContext();

    if (isPlaying) ws.send(JSON.stringify({ type: 'reader_pause' }));
    clearAudioQueue();

    playSeqId++;
    const mySeq = playSeqId;
    finished = false;
    stopped = false;
    follow = true;
    followBtn.hidden = true;
    isPlaying = true;
    markSentence(chunkIdx !== undefined ? chunkIdx : currentChunk, sentIdx !== undefined ? sentIdx : currentSentence, true);
    refreshTransport();
    setTimeout(() => {
      if (mySeq !== playSeqId) return;
      ws.send(JSON.stringify({
        type: 'reader_play',
        doc_id: currentDocId,
        chunk_index: chunkIdx !== undefined ? chunkIdx : currentChunk,
        sentence_index: sentIdx !== undefined ? sentIdx : currentSentence,
        voice: voiceSelect.value,
        seq: mySeq,
      }));
    }, 100);
  }

  // The server synthesizes ahead of what is heard, so pause and stop tell it the
  // sentence actually playing; it saves that as the place instead of its own.
  function heardPlace() {
    return isPlaying && currentDocId
      ? { doc_id: currentDocId, chunk_index: currentChunk, sentence_index: currentSentence }
      : {};
  }

  function sendPause() {
    playSeqId++;
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'reader_pause', ...heardPlace() }));
    clearAudioQueue();
    isPlaying = false;
    followBtn.hidden = true;
    markSentence(currentChunk, currentSentence, false);
    refreshTransport();
  }

  // Leaving mid-play: without this the server keeps the further-ahead place it streamed to.
  window.addEventListener('pagehide', () => {
    if (isPlaying && ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'reader_pause', ...heardPlace() }));
    }
  });

  function sendStop() {
    // Stop playback but keep the place on the sentence being read: the next Play
    // starts that sentence again from its beginning.
    playSeqId++;
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'reader_stop', ...heardPlace() }));
    clearAudioQueue();
    isPlaying = false;
    stopped = true;
    followBtn.hidden = true;
    showProgress(globalIndex(currentChunk, currentSentence));
    markSentence(currentChunk, currentSentence, false);
    refreshTransport();
  }

  function togglePlay() {
    if (!playable()) return;
    if (isPlaying) sendPause();
    else if (finished) sendPlay(firstChunk(), 0);
    else sendPlay(currentChunk, currentSentence);
  }

  // ---------- Seeking ----------

  function firstChunk() {
    const sections = (currentDoc && currentDoc.sections) || [];
    return sections.length ? sections[0].index : 0;
  }

  function seekTo(chunkIdx, sentIdx) {
    currentChunk = chunkIdx;
    currentSentence = sentIdx;
    finished = false;
    stopped = false;
    showProgress(globalIndex(chunkIdx, sentIdx));
    follow = true;
    followBtn.hidden = true;
    markSentence(chunkIdx, sentIdx, 'smooth');
    if (isPlaying) sendPlay(chunkIdx, sentIdx);
    refreshTransport();
  }

  // Every non-blank sentence in reading order; the server skips blank ones.
  function readingOrder() {
    const out = [];
    for (const sec of (currentDoc && currentDoc.sections) || []) {
      const sentences = sec.sentences || [];
      for (let i = 0; i < sec.sentence_count; i++) {
        if (sentences.length && !String(sentences[i] || '').trim()) continue;
        out.push([sec.index, i]);
      }
    }
    return out;
  }

  function stepSentence(dir) {
    if (!playable()) return;
    const order = readingOrder();
    if (!order.length) return;
    let j;
    const i = order.findIndex(([c, s]) => c === currentChunk && s === currentSentence);
    if (i >= 0) {
      j = i + dir;
    } else {
      // Cursor sits on a blank sentence: move to the nearest real one in that direction.
      let k = order.findIndex(([c, s]) => c > currentChunk || (c === currentChunk && s > currentSentence));
      if (k < 0) k = order.length;
      j = dir > 0 ? k : k - 1;
    }
    j = Math.max(0, Math.min(order.length - 1, j));
    seekTo(order[j][0], order[j][1]);
  }

  function stepSection(dir) {
    if (!playable()) return;
    const sections = currentDoc.sections || [];
    if (!sections.length) return;
    const idx = Math.max(0, sections.findIndex((s) => s.index === currentChunk));
    if (dir < 0) {
      // Like a "previous track" button: back to the start of this section first.
      if (currentSentence > 0) { seekTo(sections[idx].index, 0); return; }
      seekTo(sections[Math.max(0, idx - 1)].index, 0);
    } else {
      seekTo(sections[Math.min(sections.length - 1, idx + 1)].index, 0);
    }
  }

  playPauseBtn.addEventListener('click', togglePlay);
  skipBack.addEventListener('click', () => stepSection(-1));
  skipFwd.addEventListener('click', () => stepSection(1));
  stepBack.addEventListener('click', () => stepSentence(-1));
  stepFwd.addEventListener('click', () => stepSentence(1));

  progressBar.addEventListener('input', () => {
    clearTimeout(seekDebounce);
    progressCur.textContent = Math.min(parseInt(progressBar.value, 10) + 1, totalSentences);
    seekDebounce = setTimeout(() => {
      const target = parseInt(progressBar.value, 10);
      const sections = (currentDoc && currentDoc.sections) || [];
      let accum = 0;
      for (const sec of sections) {
        if (accum + sec.sentence_count > target) {
          seekTo(sec.index, target - accum);
          return;
        }
        accum += sec.sentence_count;
      }
    }, 200);
  });

  sectionsEl.addEventListener('click', (event) => {
    const t = event.target;
    if (!(t instanceof Element)) return;
    if (window.getSelection && String(window.getSelection()).length > 0) return; // selecting text
    const s = t.closest('.s');
    if (s) { seekTo(Number(s.dataset.c), Number(s.dataset.s)); return; }
    const h = t.closest('.sec-head');
    if (h) seekTo(Number(h.dataset.c), 0);
  });

  speedSlider.addEventListener('input', () => {
    speedVal.textContent = parseFloat(speedSlider.value).toFixed(1) + 'x';
    if (currentAudio) currentAudio.playbackRate = parseFloat(speedSlider.value);
    savePrefs({ speed: parseFloat(speedSlider.value) });
  });

  OctaviusApp.loadVoices(voiceSelect, { currentVoice: prefs.voice }).catch(() => {});
  voiceSelect.addEventListener('change', () => {
    savePrefs({ voice: voiceSelect.value });
    if (isPlaying) sendPlay(currentChunk, currentSentence);
  });

  // ---------- Document view ----------

  // Builds the text + position state for a document. Shared by the initial open and by
  // the append-poll completion, so a document that grows or is replaced while open
  // re-renders the same way. `preservePosition: false` (after a replace) resets to the start.
  function renderDocument(doc, opts) {
    const preservePosition = !opts || opts.preservePosition !== false;
    currentDoc = doc;
    totalSentences = doc.total_sentences || 0;
    playerTitle.textContent = doc.title;
    playerEdit.hidden = !(doc.status === 'ready' || doc.status === 'failed');
    emptyEl.hidden = true;
    isPlaying = false;
    finished = false;
    stopped = false;
    follow = true;
    followBtn.hidden = true;
    syncDocFromDetail(doc);
    sentenceEls = new Map();
    markedEl = null;
    sectionsEl.innerHTML = '';

    if (doc.status !== 'ready') {
      buildToc(null);
      renderDocState(doc);
      currentChunk = 0;
      currentSentence = 0;
      showProgress(0);
      refreshTransport();
      return;
    }
    docStateEl.hidden = true;
    docStateEl.innerHTML = '';

    const title = document.createElement('h2');
    title.className = 'text-title';
    title.textContent = doc.title;
    sectionsEl.appendChild(title);

    const frag = document.createDocumentFragment();
    for (const sec of doc.sections || []) {
      const wrap = document.createElement('section');
      wrap.className = 'sec';
      if (sec.heading) {
        const h = document.createElement('button');
        h.type = 'button';
        h.className = 'sec-head';
        h.dataset.c = sec.index;
        h.textContent = sec.heading;
        h.title = 'Read from here';
        wrap.appendChild(h);
      }
      const body = document.createElement('p');
      body.className = 'sec-body';
      const sentences = sec.sentences || [];
      sentences.forEach((text, i) => {
        if (!String(text || '').trim()) return;
        const span = document.createElement('span');
        span.className = 's';
        span.dataset.c = sec.index;
        span.dataset.s = i;
        span.textContent = text;
        body.appendChild(span);
        body.appendChild(document.createTextNode(' '));
        sentenceEls.set(sec.index + ':' + i, span);
      });
      wrap.appendChild(body);
      frag.appendChild(wrap);
    }
    sectionsEl.appendChild(frag);

    if (preservePosition) {
      currentChunk = doc.last_chunk || 0;
      currentSentence = doc.last_sentence || 0;
    } else {
      currentChunk = firstChunk();
      currentSentence = 0;
    }
    // Read to the end last time: show it as finished, and Play starts over.
    const prog = numOrNull(doc.progress_sentences);
    if (totalSentences && prog !== null && prog >= totalSentences) finished = true;
    showProgress(globalIndex(currentChunk, currentSentence));
    buildToc(doc);

    if (doc.error) {
      setNotice(`The last update didn’t go through: ${doc.error}`, 'error');
    } else {
      setNotice('');
    }

    if (currentChunk > 0 || currentSentence > 0) {
      requestAnimationFrame(() => markSentence(currentChunk, currentSentence, 'instant'));
    } else {
      pageEl.scrollTop = 0;
      markSentence(currentChunk, currentSentence, false);
    }
    refreshTransport();
  }

  const SOURCE_LABELS = { pdf: 'PDF', url: 'Web page', inbox_item: 'From inbox', markdown: 'Text', text: 'Pasted text' };

  function renderDocState(doc) {
    setNotice('');
    docStateEl.hidden = false;
    if (doc.status === 'processing') {
      docStateEl.innerHTML = `
        <h2>Converting this document</h2>
        <p>It opens here as soon as it’s ready. Long PDFs can take a few minutes.</p>`;
      startDocPoll(doc.id);
    } else {
      docStateEl.innerHTML = `
        <h2>This document didn’t convert</h2>
        <p class="why">${esc(doc.error || 'No reason was recorded.')}</p>
        <p>Retry fetches the original source again. If the page was behind a sign-in, use Edit to paste the text instead.</p>
        <div class="actions">
          <button class="btn primary" data-act="retry">Retry</button>
          <button class="btn" data-act="edit">Edit</button>
        </div>`;
    }
  }

  docStateEl.addEventListener('click', (event) => {
    const btn = event.target instanceof Element ? event.target.closest('button[data-act]') : null;
    if (!btn || !currentDoc) return;
    if (btn.dataset.act === 'retry') retryDoc(currentDoc.id);
    if (btn.dataset.act === 'edit') openEditBar();
  });

  function stopDocPoll() {
    clearTimeout(docPollTimer);
    docPollTimer = null;
  }

  function startDocPoll(docId) {
    stopDocPoll();
    docPollTimer = setTimeout(async () => {
      if (currentDocId !== docId) return;
      try {
        const resp = await fetch(`/api/reader/documents/${docId}`);
        const data = await resp.json();
        if (currentDocId !== docId) return;
        const doc = data.document;
        if (doc && doc.status !== 'processing') {
          renderDocument(doc, { preservePosition: true });
          loadDocList();
          return;
        }
      } catch (_err) {
      }
      startDocPoll(docId);
    }, 3000);
  }

  async function openDocument(docId) {
    try {
      const resp = await fetch(`/api/reader/documents/${docId}`);
      const data = await resp.json();
      if (!data.document) throw new Error(data.error || 'not found');
      if (isPlaying) sendPause();
      clearAudioQueue();
      currentDocId = docId;
      stopAppendPoll();
      stopDocPoll();
      closeEditBar();
      renderDocument(data.document, { preservePosition: true });
      markCurrentInList();
      closeRail();
      try {
        const url = new URL(location.href);
        url.searchParams.set('doc_id', docId);
        url.searchParams.delete('inbox_id');
        history.replaceState(null, '', url);
      } catch (_err) {
      }
    } catch (e) {
      console.error('Failed to open document:', e);
    }
  }

  function closeDocument() {
    if (isPlaying) sendPause();
    clearAudioQueue();
    stopAppendPoll();
    stopDocPoll();
    closeEditBar();
    currentDocId = null;
    currentDoc = null;
    totalSentences = 0;
    sentenceEls = new Map();
    markedEl = null;
    sectionsEl.innerHTML = '';
    docStateEl.hidden = true;
    setNotice('');
    emptyEl.hidden = false;
    playerTitle.textContent = 'Reader';
    playerEdit.hidden = true;
    buildToc(null);
    showProgress(0);
    markCurrentInList();
    refreshTransport();
    try {
      const url = new URL(location.href);
      url.searchParams.delete('doc_id');
      history.replaceState(null, '', url);
    } catch (_err) {
    }
  }

  // ---------- Reading progress ----------

  function numOrNull(v) {
    return typeof v === 'number' && isFinite(v) ? v : null;
  }

  // Returns {pct, done} or null when there is nothing worth showing (unknown or not started).
  function progressOf(doc) {
    const total = numOrNull(doc.total_sentences);
    const done = numOrNull(doc.progress_sentences);
    if (!total || done === null || done <= 0) return null;
    if (done >= total) return { pct: 100, done: true };
    return { pct: Math.max(1, Math.min(99, Math.floor((100 * done) / total))), done: false };
  }

  function paintRowProgress(row, doc) {
    const pctEl = row.querySelector('.doc-pct');
    const bar = row.querySelector('.doc-bar');
    if (!pctEl || !bar) return;
    const p = progressOf(doc);
    pctEl.hidden = !p;
    bar.hidden = !p;
    if (!p) return;
    pctEl.textContent = p.done ? 'Finished' : `${p.pct}%`;
    pctEl.classList.toggle('done', p.done);
    bar.classList.toggle('done', p.done);
    bar.firstElementChild.style.width = p.pct + '%';
    bar.setAttribute('aria-label', p.done ? 'Finished' : `${p.pct}% read`);
  }

  function setDocProgress(id, done, total) {
    if (id == null) return;
    const doc = docs.find((d) => d.id === id);
    if (!doc) return;
    doc.progress_sentences = done;
    if (numOrNull(total)) doc.total_sentences = total;
    const row = docList.querySelector(`.doc[data-id="${id}"]`);
    if (row) paintRowProgress(row, doc);
  }

  // The detail endpoint carries the same two fields; fold them into the list entry.
  function syncDocFromDetail(doc) {
    const entry = docs.find((d) => d.id === doc.id);
    if (!entry) return;
    entry.total_sentences = numOrNull(doc.total_sentences);
    entry.progress_sentences = numOrNull(doc.progress_sentences);
    entry.status = doc.status;
    const row = docList.querySelector(`.doc[data-id="${doc.id}"]`);
    if (row) paintRowProgress(row, entry);
  }

  // ---------- Table of contents ----------

  const CHEVRON = '<svg viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4.5 2.5L8 6l-3.5 3.5"/></svg>';

  function tocAvailable() {
    return tocLinks.size >= 2;
  }

  function buildToc(doc) {
    tocTree.innerHTML = '';
    tocLinks = new Map();
    tocCurrent = null;
    const heads = ((doc && doc.sections) || []).filter((sec) => sec.heading);
    if (heads.length < 2) {
      railTabs.hidden = true;
      selectRailTab('library', true);
      return;
    }
    tocDoc.textContent = doc.title || '';

    // Nest by level (1-3); documents converted before levels existed are flat.
    const root = { level: 0, children: [], el: tocTree };
    const stack = [root];
    for (const sec of heads) {
      const lv = [1, 2, 3].includes(sec.level) ? sec.level : 1;
      while (stack.length > 1 && stack[stack.length - 1].level >= lv) stack.pop();
      const parent = stack[stack.length - 1];
      const node = { level: lv, sec, children: [], parent };
      parent.children.push(node);
      stack.push(node);
    }

    function render(nodes, ul) {
      for (const node of nodes) {
        const li = document.createElement('li');
        li.className = 'toc-item';
        li.setAttribute('role', 'treeitem');
        const row = document.createElement('div');
        row.className = 'toc-row';
        if (node.children.length) {
          li.classList.add('collapsed');
          li.setAttribute('aria-expanded', 'false');
          const tog = document.createElement('button');
          tog.type = 'button';
          tog.className = 'toc-toggle';
          tog.setAttribute('aria-label', `Show or hide subsections of ${node.sec.heading}`);
          tog.innerHTML = CHEVRON;
          tog.addEventListener('click', () => setExpanded(li, li.classList.contains('collapsed')));
          row.appendChild(tog);
        } else {
          const sp = document.createElement('span');
          sp.className = 'toc-spacer';
          row.appendChild(sp);
        }
        const link = document.createElement('button');
        link.type = 'button';
        link.className = 'toc-link';
        link.textContent = node.sec.heading;
        link.dataset.c = node.sec.index;
        link.addEventListener('click', () => {
          seekTo(node.sec.index, 0);
          if (window.matchMedia('(max-width: 860px)').matches) closeRail();
        });
        row.appendChild(link);
        li.appendChild(row);
        tocLinks.set(node.sec.index, link);
        if (node.children.length) {
          const sub = document.createElement('ul');
          sub.setAttribute('role', 'group');
          render(node.children, sub);
          li.appendChild(sub);
        }
        ul.appendChild(li);
      }
    }
    render(root.children, tocTree);
    railTabs.hidden = false;
    selectRailTab(railTab, true);
  }

  function setExpanded(li, open) {
    li.classList.toggle('collapsed', !open);
    li.setAttribute('aria-expanded', String(open));
  }

  // The heading section the reader is currently inside, if any.
  function headingFor(chunkIdx) {
    let found = null;
    for (const idx of tocLinks.keys()) {
      if (idx <= chunkIdx) found = idx;
      else break;
    }
    return found;
  }

  function updateTocCurrent(chunkIdx) {
    if (!tocLinks.size) return;
    const idx = headingFor(chunkIdx);
    const link = idx === null ? null : tocLinks.get(idx);
    if (link === tocCurrent) return;
    if (tocCurrent) tocCurrent.removeAttribute('aria-current');
    tocCurrent = link;
    if (!link) return;
    link.setAttribute('aria-current', 'true');
    // Open the branch that holds the current section.
    for (let li = link.closest('.toc-item'); li; li = li.parentElement && li.parentElement.closest('.toc-item')) {
      if (li.querySelector(':scope > ul') && li !== link.closest('.toc-item')) setExpanded(li, true);
    }
    if (!paneContents.hidden) link.scrollIntoView({ block: 'nearest' });
  }

  function selectRailTab(which, quiet) {
    if (which === 'contents' && !tocAvailable()) which = 'library';
    if (!quiet) railTab = which;
    const contents = which === 'contents';
    tabLibrary.setAttribute('aria-selected', String(!contents));
    tabContents.setAttribute('aria-selected', String(contents));
    paneLibrary.hidden = contents;
    paneContents.hidden = !contents;
    if (contents && tocCurrent) tocCurrent.scrollIntoView({ block: 'nearest' });
  }
  tabLibrary.addEventListener('click', () => selectRailTab('library'));
  tabContents.addEventListener('click', () => selectRailTab('contents'));

  // ---------- Library ----------

  function formatDate(iso) {
    if (!iso) return '';
    const d = new Date(String(iso).replace(' ', 'T'));
    if (isNaN(d)) return '';
    const sameYear = d.getFullYear() === new Date().getFullYear();
    return d.toLocaleDateString([], sameYear ? { month: 'short', day: 'numeric' } : { month: 'short', day: 'numeric', year: 'numeric' });
  }

  function renderList() {
    const q = query.trim().toLowerCase();
    const shown = docs.filter((d) => (filter === 'all' || d.status === filter) &&
      (!q || String(d.title || '').toLowerCase().includes(q)));
    if (docs.length === 0) {
      docList.innerHTML = '<div class="list-empty">No documents yet. Add a file path, a URL or some text above.</div>';
      return;
    }
    if (shown.length === 0) {
      docList.innerHTML = '<div class="list-empty">Nothing matches. Clear the search or pick another filter.</div>';
      return;
    }
    docList.innerHTML = '';
    for (const doc of shown) {
      const row = document.createElement('div');
      row.className = 'doc';
      row.setAttribute('role', 'listitem');
      row.dataset.id = doc.id;
      let stateTag = '';
      if (doc.status === 'processing') stateTag = '<span class="doc-state-tag processing">Converting</span><span class="sep"></span>';
      else if (doc.status === 'failed') stateTag = '<span class="doc-state-tag failed">Failed</span><span class="sep"></span>';
      const source = SOURCE_LABELS[doc.source_type] || doc.source_type || '';
      row.innerHTML = `
        <button type="button" class="doc-open">
          <span class="doc-title">${esc(doc.title)}</span>
          <span class="doc-meta">${stateTag}<span class="doc-pct" hidden></span><span class="sep"></span>${source ? `<span>${esc(source)}</span><span class="sep"></span>` : ''}<span>${esc(formatDate(doc.created_at))}</span></span>
          <span class="doc-bar" hidden><span></span></span>
        </button>
        <div class="doc-actions">
          ${doc.status === 'failed' ? `<button class="icon-btn" data-act="retry" title="Retry" aria-label="Retry ${esc(doc.title)}"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 12a8 8 0 0 1 13.7-5.6L20 8.7M20 4v4.7h-4.7M20 12a8 8 0 0 1-13.7 5.6L4 15.3M4 20v-4.7h4.7"/></svg></button>` : ''}
          <button class="icon-btn danger" data-act="delete" title="Delete" aria-label="Delete ${esc(doc.title)}"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M5 7h14M10 7V5h4v2M7 7l1 12h8l1-12"/></svg></button>
        </div>`;
      paintRowProgress(row, doc);
      docList.appendChild(row);
    }
    markCurrentInList();
  }

  function markCurrentInList() {
    docList.querySelectorAll('.doc').forEach((el) => {
      el.setAttribute('aria-current', String(Number(el.dataset.id) === currentDocId));
    });
  }

  docList.addEventListener('click', async (event) => {
    const t = event.target;
    if (!(t instanceof Element)) return;
    const row = t.closest('.doc');
    if (!row) return;
    const id = Number(row.dataset.id);
    const doc = docs.find((d) => d.id === id);
    const act = t.closest('button[data-act]');
    if (act && act.dataset.act === 'retry') {
      retryDoc(id);
      return;
    }
    if (act && act.dataset.act === 'delete') {
      if (!confirm(`Delete “${doc ? doc.title : 'this document'}”? This can’t be undone.`)) return;
      await fetch(`/api/reader/documents/${id}`, { method: 'DELETE' });
      if (currentDocId === id) closeDocument();
      loadDocList();
      return;
    }
    if (t.closest('.doc-open')) {
      if (currentDocId === id && currentDoc && currentDoc.status === 'ready') { closeRail(); return; }
      openDocument(id);
    }
  });

  async function retryDoc(id) {
    await fetch(`/api/reader/documents/${id}/retry`, { method: 'POST' }).catch(() => {});
    await loadDocList();
    if (currentDocId === id) openDocument(id);
  }

  async function loadDocList() {
    try {
      const resp = await fetch('/api/reader/documents');
      const data = await resp.json();
      docs = data.documents || [];
      renderList();
    } catch (_err) {
      docList.innerHTML = '<div class="list-empty">Couldn’t load documents. They’ll appear when the server answers.</div>';
    }

    clearTimeout(pollTimer);
    if (docs.some((d) => d.status === 'processing')) {
      pollTimer = setTimeout(loadDocList, 5000);
    }
  }

  searchInput.addEventListener('input', () => {
    query = searchInput.value;
    renderList();
  });
  searchInput.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { searchInput.value = ''; query = ''; renderList(); searchInput.blur(); }
  });

  statusFilter.addEventListener('click', (event) => {
    const chip = event.target instanceof Element ? event.target.closest('.chip') : null;
    if (!chip) return;
    filter = chip.dataset.filter;
    statusFilter.querySelectorAll('.chip').forEach((c) => c.setAttribute('aria-checked', String(c === chip)));
    renderList();
  });

  // ---------- Ingest ----------

  function setAddMsg(text, tone) {
    addMsg.textContent = text || '';
    addMsg.dataset.tone = tone || '';
  }

  ingestForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    const val = ingestInput.value.trim();
    if (!val) return;

    const isUrl = val.startsWith('http://') || val.startsWith('https://');
    const body = {
      source: isUrl ? 'url' : 'file',
      path: isUrl ? undefined : val,
      url: isUrl ? val : undefined,
      title: val.split('/').pop() || 'Document',
    };

    try {
      const resp = await fetch('/api/reader/documents', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        setAddMsg(err.error || `Couldn’t add that (HTTP ${resp.status}).`, 'error');
        return;
      }
      ingestInput.value = '';
      setAddMsg('Added. It shows as Converting until it’s ready.');
      loadDocList();
    } catch (e) {
      console.error('Ingest failed:', e);
      setAddMsg('Couldn’t reach the server.', 'error');
    }
  });

  function closePasteBar() {
    pasteBar.hidden = true;
    pasteToggle.setAttribute('aria-expanded', 'false');
    pasteInput.value = '';
    pasteTitle.value = '';
  }

  pasteToggle.addEventListener('click', () => {
    if (pasteBar.hidden) {
      pasteBar.hidden = false;
      pasteToggle.setAttribute('aria-expanded', 'true');
      pasteInput.focus();
    } else {
      closePasteBar();
    }
  });

  pasteCancel.addEventListener('click', closePasteBar);

  pasteSubmit.addEventListener('click', async () => {
    const text = pasteInput.value.trim();
    if (!text) return;

    // Title is optional; the server derives one from the first line.
    const body = { source: 'text', text };
    const title = pasteTitle.value.trim();
    if (title) body.title = title;

    pasteSubmit.disabled = true;
    try {
      const resp = await fetch('/api/reader/documents', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        console.error('Paste ingest failed:', err.error || resp.status);
        setAddMsg(err.error || `Couldn’t add the text (HTTP ${resp.status}).`, 'error');
        return;
      }
      closePasteBar();
      setAddMsg('Added. It shows as Converting until it’s ready.');
      loadDocList();
    } catch (e) {
      console.error('Paste ingest failed:', e);
      setAddMsg('Couldn’t reach the server.', 'error');
    } finally {
      pasteSubmit.disabled = false;
    }
  });

  // Ctrl/Cmd+Enter submits.
  pasteInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) pasteSubmit.click();
  });

  // ---------- Edit panel: rename / append / replace ----------

  const HINT_APPEND = 'The text is cleaned and math is converted to speech, then added to the end. Your place is kept.';
  const HINT_REPLACE = 'The current content is discarded and the document is rebuilt from this text alone. Your place resets to the start.';
  function syncEditHint() {
    editHint.textContent = editReplaceCheckbox.checked ? HINT_REPLACE : HINT_APPEND;
  }
  editReplaceCheckbox.addEventListener('change', syncEditHint);

  function showEditError(msg) {
    editError.textContent = msg || '';
  }

  function openEditBar() {
    if (!currentDocId || !currentDoc) return;
    editDocId = currentDocId;
    editOrigTitle = currentDoc.title || '';
    editTitleInput.value = editOrigTitle;
    editAppendInput.value = '';
    editReplaceCheckbox.checked = false;
    syncEditHint();
    showEditError('');
    editBar.hidden = false;
    playerEdit.setAttribute('aria-expanded', 'true');
    editTitleInput.focus();
  }

  function closeEditBar() {
    editBar.hidden = true;
    playerEdit.setAttribute('aria-expanded', 'false');
    editDocId = null;
    editTitleInput.value = '';
    editAppendInput.value = '';
    editReplaceCheckbox.checked = false;
    showEditError('');
  }

  playerEdit.addEventListener('click', () => {
    if (editBar.hidden) openEditBar();
    else closeEditBar();
  });
  editCancelBtn.addEventListener('click', closeEditBar);

  editAppendInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) editSaveBtn.click();
  });
  editTitleInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) editSaveBtn.click();
  });

  function stopAppendPoll() {
    clearTimeout(editPollTimer);
    editPollTimer = null;
  }

  function startAppendPoll(docId, replace) {
    stopAppendPoll();
    setNotice('Processing the new text…');
    let failures = 0;

    // Transient failures retry on the normal cadence; a run of them stops the poll
    // with a visible message rather than leaving "Processing…" up forever.
    function retry() {
      if (currentDocId !== docId) return;
      failures++;
      if (failures >= 5) {
        editPollTimer = null;
        setNotice('Lost track of the update. Reopen the document to check.', 'error');
        return;
      }
      editPollTimer = setTimeout(poll, 3000);
    }

    function poll() {
      if (currentDocId !== docId) return; // stale poll for a doc we've navigated away from
      fetch(`/api/reader/documents/${docId}`)
        .then((resp) => (resp.ok ? resp.json() : Promise.reject(new Error('HTTP ' + resp.status))))
        .then((data) => {
          if (currentDocId !== docId) return;
          const doc = data.document;
          if (!doc) { retry(); return; }
          failures = 0;
          if (doc.status === 'processing') {
            editPollTimer = setTimeout(poll, 3000);
            return;
          }
          editPollTimer = null;
          loadDocList();
          if (doc.status === 'failed') {
            renderDocument(doc, { preservePosition: true });
            return;
          }
          if (doc.error) {
            // A failed append on a previously-ready document comes back `ready` with
            // `error` set and its content untouched: report it as a failure.
            currentDoc = Object.assign(currentDoc || {}, { error: doc.error, title: doc.title });
            setNotice(`The new text wasn’t added: ${doc.error}`, 'error');
            return;
          }
          renderDocument(doc, { preservePosition: !replace });
        })
        .catch(retry);
    }
    poll();
  }

  async function saveEdit() {
    const docId = editDocId;
    if (docId == null) return;

    const rawTitle = editTitleInput.value.trim();
    const titleChanged = !!rawTitle && rawTitle !== editOrigTitle;
    const text = editAppendInput.value.trim();
    const replace = editReplaceCheckbox.checked;

    if (!titleChanged && !text) {
      closeEditBar();
      return;
    }

    if (text && replace) {
      if (!confirm('Replace the whole document? Your place resets to the start.')) return;
    }

    editSaveBtn.disabled = true;
    showEditError('');
    try {
      if (titleChanged) {
        const resp = await fetch(`/api/reader/documents/${docId}`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ title: rawTitle }),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) {
          showEditError(resp.status === 409
            ? 'Still processing. Try again in a moment.'
            : (data.error || 'Rename failed.'));
          return;
        }
        editOrigTitle = data.title;
        if (currentDocId === docId) {
          playerTitle.textContent = data.title;
          const h = sectionsEl.querySelector('.text-title');
          if (h) h.textContent = data.title;
          if (currentDoc) currentDoc.title = data.title;
        }
        loadDocList();
      }

      if (text) {
        if (isPlaying) sendPause();
        const resp = await fetch(`/api/reader/documents/${docId}/append`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text, replace }),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) {
          const why = resp.status === 409
            ? 'Still processing. Try again in a moment.'
            : (data.error || 'Append failed.');
          // The rename above already landed; say so rather than implying nothing changed.
          showEditError(titleChanged ? 'Title saved. ' + why : why);
          return;
        }
        closeEditBar();
        if (currentDocId === docId) startAppendPoll(docId, replace);
        loadDocList();
        return;
      }

      closeEditBar();
    } catch (e) {
      showEditError('Save failed.');
    } finally {
      editSaveBtn.disabled = false;
    }
  }

  editSaveBtn.addEventListener('click', saveEdit);

  // ---------- Drawer (narrow screens) ----------

  function openRail() {
    shell.classList.add('rail-open');
    scrim.hidden = false;
  }
  function closeRail() {
    shell.classList.remove('rail-open');
    scrim.hidden = true;
  }
  railOpen.addEventListener('click', openRail);
  railClose.addEventListener('click', closeRail);
  scrim.addEventListener('click', closeRail);

  // ---------- Keyboard ----------

  function isTypingTarget(el) {
    if (!el) return false;
    const tag = el.tagName;
    return tag === 'TEXTAREA' || tag === 'SELECT' || el.isContentEditable ||
      (tag === 'INPUT' && !['checkbox', 'radio', 'range', 'button'].includes(el.type));
  }

  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
      if (!editBar.hidden && editBar.contains(document.activeElement)) { closeEditBar(); return; }
      if (shell.classList.contains('rail-open')) { closeRail(); return; }
      if (isTypingTarget(document.activeElement)) return;
      if (isPlaying || audioQueue.length || currentAudio) sendStop();
      return;
    }
    if (isTypingTarget(document.activeElement)) return;
    if (event.metaKey || event.ctrlKey || event.altKey) return;
    if ((event.key === 't' || event.key === 'T') && !event.shiftKey) {
      if (!tocAvailable()) return;
      event.preventDefault();
      const narrow = window.matchMedia('(max-width: 860px)').matches;
      if (narrow && !shell.classList.contains('rail-open')) { selectRailTab('contents'); openRail(); return; }
      selectRailTab(railTab === 'contents' ? 'library' : 'contents');
      if (narrow && railTab === 'library') closeRail();
      return;
    }
    if (event.key === '/') {
      event.preventDefault();
      selectRailTab('library');
      if (window.matchMedia('(max-width: 860px)').matches) openRail();
      searchInput.focus();
      return;
    }
    if (event.code === 'Space' || event.key === ' ') {
      if (document.activeElement && document.activeElement.type === 'range') return;
      event.preventDefault();
      if (!event.repeat) togglePlay();
      return;
    }
    if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
      if (document.activeElement && document.activeElement.type === 'range') return;
      event.preventDefault();
      const dir = event.key === 'ArrowLeft' ? -1 : 1;
      if (event.shiftKey) stepSection(dir);
      else stepSentence(dir);
    }
  });

  // ---------- Boot ----------

  const params = new URLSearchParams(location.search);
  const inboxId = params.get('inbox_id');
  if (inboxId) {
    fetch('/api/reader/documents', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ source: 'inbox', saved_item_id: parseInt(inboxId, 10) }),
    }).then(() => loadDocList());
    // Don't re-ingest the same inbox item if the page is reloaded.
    try {
      const url = new URL(location.href);
      url.searchParams.delete('inbox_id');
      history.replaceState(null, '', url);
    } catch (_err) {
    }
  }

  const docIdParam = params.get('doc_id');
  if (docIdParam) openDocument(parseInt(docIdParam, 10));

  refreshTransport();
  connectWS();
  if (!inboxId) loadDocList();
})();
