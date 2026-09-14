(function() {
  const listView = document.getElementById('list-view');
  const docList = document.getElementById('doc-list');
  const ingestInput = document.getElementById('ingest-input');
  const ingestBtn = document.getElementById('ingest-btn');
  const pasteToggle = document.getElementById('paste-toggle');
  const pasteBar = document.getElementById('paste-bar');
  const pasteInput = document.getElementById('paste-input');
  const pasteTitle = document.getElementById('paste-title');
  const pasteSubmit = document.getElementById('paste-submit');
  const pasteCancel = document.getElementById('paste-cancel');
  const playerDiv = document.getElementById('player');
  const playerBack = document.getElementById('player-back');
  const playerTitle = document.getElementById('player-title');
  const sectionsEl = document.getElementById('sections');
  const progressBar = document.getElementById('progress-bar');
  const progressCur = document.getElementById('progress-current');
  const progressTot = document.getElementById('progress-total');
  const playPauseBtn = document.getElementById('play-pause-btn');
  const skipBack = document.getElementById('skip-back');
  const skipFwd = document.getElementById('skip-fwd');
  const speedSlider = document.getElementById('reader-speed');
  const speedVal = document.getElementById('reader-speed-val');
  const voiceSelect = document.getElementById('reader-voice');
  const readAlong = document.getElementById('read-along');
  const playerEdit = document.getElementById('player-edit');
  const editBar = document.getElementById('edit-bar');
  const editBarCaption = document.getElementById('edit-bar-caption');
  const editTitleInput = document.getElementById('edit-title');
  const editAppendInput = document.getElementById('edit-append');
  const editReplaceCheckbox = document.getElementById('edit-replace');
  const editError = document.getElementById('edit-error');
  const editHint = document.getElementById('edit-hint');
  const editCancelBtn = document.getElementById('edit-cancel');
  const editSaveBtn = document.getElementById('edit-save');
  const editMountList = document.getElementById('edit-bar-mount-list');
  const editMountPlayer = document.getElementById('edit-bar-mount-player');

  let ws = null;
  let currentDocId = null;
  let currentDoc = null;
  let isPlaying = false;
  let currentChunk = 0;
  let currentSentence = 0;
  let totalSentences = 0;
  let currentAudio = null;
  const audioQueue = [];
  const positionQueue = [];
  let pollTimer = null;
  let playSeqId = 0;
  let audioEpochArmed = false;
  let seekDebounce = null;
  let editDocId = null;
  let editOrigTitle = '';
  let editContext = null; // 'list' | 'player'
  let editPollTimer = null;

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
    progressCur.textContent = pos.sentence_global;
    progressTot.textContent = totalSentences;

    document.querySelectorAll('.section-item.active').forEach(el => el.classList.remove('active'));
    const secEl = document.querySelector(`.section-item[data-chunk="${pos.chunk_index}"]`);
    if (secEl) {
      secEl.classList.add('active');
      secEl.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    }

    if (pos.sentence_text) {
      readAlong.innerHTML = '<span class="current-sentence">' + OctaviusApp.escapeHtml(pos.sentence_text) + '</span>';
    }
  }

  async function playNextAudio() {
    if (audioQueue.length === 0) {
      currentAudio = null;
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

  function connectWS() {
    const socketController = OctaviusApp.createWebSocket({
      binaryType: 'arraybuffer',
      heartbeat: true,
      onOpen(_evt, socket) {
        ws = socket;
      },
      onMessage(evt) {
        ws = socketController.getSocket();
        if (evt.data instanceof ArrayBuffer) {
          if (!isPlaying || !audioEpochArmed) return;
          audioEpochArmed = false;
          enqueueAudio(evt.data);
          return;
        }
        const msg = JSON.parse(evt.data);
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
          playPauseBtn.innerHTML = '&#9654;';
        }
      },
    });
    ws = socketController.connect();
  }

  OctaviusApp.loadVoices(voiceSelect).catch(() => {});

  speedSlider.addEventListener('input', () => {
    speedVal.textContent = parseFloat(speedSlider.value).toFixed(1) + 'x';
    if (currentAudio) currentAudio.playbackRate = parseFloat(speedSlider.value);
  });

  function showEditError(msg) {
    editError.textContent = msg || '';
  }

  function openEditBar(docId, title, context, mountEl) {
    editDocId = docId;
    editOrigTitle = title || '';
    editContext = context;
    editTitleInput.value = title || '';
    editAppendInput.value = '';
    editReplaceCheckbox.checked = false;
    syncEditHint();
    showEditError('');
    editBarCaption.textContent = title ? `Editing: ${title}` : '';
    mountEl.appendChild(editBar);
    editBar.classList.add('active');
    editTitleInput.focus();
  }

  function closeEditBar() {
    editBar.classList.remove('active');
    editDocId = null;
    editContext = null;
    editTitleInput.value = '';
    editAppendInput.value = '';
    editReplaceCheckbox.checked = false;
    showEditError('');
  }

  editCancelBtn.addEventListener('click', closeEditBar);

  const HINT_APPEND = 'The text is cleaned and math is converted to speech, then added to the end. Playback position is kept.';
  const HINT_REPLACE = 'The current content is discarded and the document is rebuilt from this text alone. Playback position resets.';
  function syncEditHint() {
    editHint.textContent = editReplaceCheckbox.checked ? HINT_REPLACE : HINT_APPEND;
  }
  editReplaceCheckbox.addEventListener('change', syncEditHint);

  // Ctrl/Cmd+Enter submits, matching the paste panel's textarea behavior.
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
    readAlong.innerHTML = '<span style="color:#666">Processing appended text\u2026</span>';
    let failures = 0;

    function showPollError(msg) {
      readAlong.innerHTML = '<span style="color:#bf6a6a">' + OctaviusApp.escapeHtml(msg) + '</span>';
    }

    // Transient failures (network blip, a non-JSON error page) retry on the normal
    // cadence; a run of them stops the poll with a visible message rather than
    // leaving "Processing…" up forever.
    function retry() {
      if (currentDocId !== docId) return;
      failures++;
      if (failures >= 5) {
        editPollTimer = null;
        showPollError('Lost track of the update \u2014 reopen the document to check.');
        return;
      }
      editPollTimer = setTimeout(poll, 3000);
    }

    function poll() {
      if (currentDocId !== docId) return; // stale poll for a doc we've navigated away from
      fetch(`/api/reader/documents/${docId}`)
        .then(resp => (resp.ok ? resp.json() : Promise.reject(new Error('HTTP ' + resp.status))))
        .then(data => {
          if (currentDocId !== docId) return; // guard again after the await
          const doc = data.document;
          if (!doc) { retry(); return; }
          failures = 0;
          if (doc.status === 'processing') {
            editPollTimer = setTimeout(poll, 3000);
            return;
          }
          editPollTimer = null;
          if (doc.status === 'failed' || doc.error) {
            // A failed append on a previously-ready document comes back `ready` with
            // `error` set and its content untouched, so there is nothing to re-render.
            currentDoc = doc;
            showPollError(doc.error || 'Append failed.');
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
    const ctx = editContext;
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
      if (!confirm('Replace the whole document? Playback position resets.')) return;
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
            ? 'Still processing \u2014 try again in a moment.'
            : (data.error || 'Rename failed.'));
          return;
        }
        editOrigTitle = data.title;
        if (ctx === 'player' && currentDocId === docId) {
          playerTitle.textContent = data.title;
          if (currentDoc) currentDoc.title = data.title;
        }
      }

      if (text) {
        if (ctx === 'player' && isPlaying) sendPause();
        const resp = await fetch(`/api/reader/documents/${docId}/append`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text, replace }),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) {
          const why = resp.status === 409
            ? 'Still processing \u2014 try again in a moment.'
            : (data.error || 'Append failed.');
          // The rename above already landed; say so rather than implying nothing changed.
          showEditError(titleChanged ? 'Title saved. ' + why : why);
          return;
        }
        closeEditBar();
        if (ctx === 'player' && currentDocId === docId) {
          startAppendPoll(docId, replace);
        } else {
          loadDocList();
        }
        return;
      }

      closeEditBar();
      if (ctx === 'list') loadDocList();
    } catch (e) {
      showEditError('Save failed.');
    } finally {
      editSaveBtn.disabled = false;
    }
  }

  editSaveBtn.addEventListener('click', saveEdit);

  playerEdit.addEventListener('click', () => {
    if (!currentDocId || !currentDoc) return;
    openEditBar(currentDocId, currentDoc.title, 'player', editMountPlayer);
  });

  async function loadDocList() {
    let docs = [];
    try {
      const resp = await fetch('/api/reader/documents');
      const data = await resp.json();
      docs = data.documents || [];
      if (docs.length === 0) {
        docList.innerHTML = '<div class="empty-state">No documents yet. Add one above.</div>';
        return;
      }
      docList.innerHTML = '';
      for (const doc of docs) {
        const card = document.createElement('div');
        card.className = 'doc-card';
        const retryButton = doc.status === 'failed'
          ? '<button class="dc-retry" title="Retry">retry</button>'
          : '';
        const editButton = (doc.status === 'ready' || doc.status === 'failed')
          ? '<button class="dc-edit" title="Edit">edit</button>'
          : '';
        card.innerHTML = `
          <span class="dc-title">${OctaviusApp.escapeHtml(doc.title)}</span>
          <span class="dc-status ${doc.status}">${doc.status}</span>
          ${retryButton}
          ${editButton}
          <button class="dc-delete" title="Delete">&times;</button>
        `;
        card.addEventListener('click', () => {
          if (doc.status === 'ready') openDocument(doc.id);
          else if (doc.status === 'processing') loadDocList();
        });
        const retryEl = card.querySelector('.dc-retry');
        if (retryEl) {
          retryEl.addEventListener('click', async (e) => {
            e.stopPropagation();
            await fetch(`/api/reader/documents/${doc.id}/retry`, { method: 'POST' });
            loadDocList();
          });
        }
        const editEl = card.querySelector('.dc-edit');
        if (editEl) {
          editEl.addEventListener('click', (e) => {
            e.stopPropagation();
            openEditBar(doc.id, doc.title, 'list', editMountList);
          });
        }
        card.querySelector('.dc-delete').addEventListener('click', async (e) => {
          e.stopPropagation();
          await fetch(`/api/reader/documents/${doc.id}`, { method: 'DELETE' });
          loadDocList();
        });
        docList.appendChild(card);
      }
    } catch {
      docList.innerHTML = '<div class="empty-state">Failed to load.</div>';
    }

    clearTimeout(pollTimer);
    if (docs.some(d => d.status === 'processing')) {
      pollTimer = setTimeout(loadDocList, 5000);
    }
  }

  ingestBtn.addEventListener('click', async () => {
    const val = ingestInput.value.trim();
    if (!val) return;

    const isUrl = val.startsWith('http://') || val.startsWith('https://');
    const body = {
      source: isUrl ? 'url' : 'file',
      path: isUrl ? undefined : val,
      url: isUrl ? val : undefined,
      title: val.split('/').pop() || 'Document',
    };

    if (isUrl) body.source = 'url';

    try {
      await fetch('/api/reader/documents', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      ingestInput.value = '';
      loadDocList();
    } catch (e) {
      console.error('Ingest failed:', e);
    }
  });

  ingestInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') ingestBtn.click();
  });

  function closePasteBar() {
    pasteBar.classList.remove('active');
    pasteInput.value = '';
    pasteTitle.value = '';
  }

  pasteToggle.addEventListener('click', () => {
    const opening = !pasteBar.classList.contains('active');
    pasteBar.classList.toggle('active', opening);
    if (opening) pasteInput.focus();
    else closePasteBar();
  });

  pasteCancel.addEventListener('click', closePasteBar);

  pasteSubmit.addEventListener('click', async () => {
    const text = pasteInput.value.trim();
    if (!text) return;

    // Title is optional — the server derives one from the first line.
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
        return;
      }
      closePasteBar();
      loadDocList();
    } catch (e) {
      console.error('Paste ingest failed:', e);
    } finally {
      pasteSubmit.disabled = false;
    }
  });

  // Ctrl/Cmd+Enter submits, matching the single-line input's Enter behavior.
  pasteInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) pasteSubmit.click();
  });

  // Builds the section list + progress/position state for a document and shows it in the
  // player. Shared by the initial open and by the append-poll completion, so a document that
  // grows or is replaced while the player is showing it re-renders the same way it would on
  // a fresh open. `preservePosition: false` (used after a `replace`) resets to the start.
  function renderDocument(doc, opts) {
    const preservePosition = !opts || opts.preservePosition !== false;
    currentDoc = doc;
    totalSentences = doc.total_sentences || 0;
    playerTitle.textContent = doc.title;

    sectionsEl.innerHTML = '';
    const sections = doc.sections || [];
    let sentenceOffset = 0;
    for (const sec of sections) {
      if (!sec.heading) {
        sentenceOffset += sec.sentence_count;
        continue;
      }
      const item = document.createElement('div');
      item.className = 'section-item';
      item.dataset.chunk = sec.index;
      item.dataset.sentenceOffset = sentenceOffset;
      item.textContent = sec.heading;
      item.addEventListener('click', () => seekTo(sec.index, 0));
      sectionsEl.appendChild(item);
      sentenceOffset += sec.sentence_count;
    }

    progressBar.max = totalSentences;
    if (preservePosition) {
      currentChunk = doc.last_chunk || 0;
      currentSentence = doc.last_sentence || 0;
    } else {
      currentChunk = 0;
      currentSentence = 0;
    }

    let savedGlobal = 0;
    for (const sec of sections) {
      if (sec.index < currentChunk) savedGlobal += sec.sentence_count;
      else if (sec.index === currentChunk) {
        savedGlobal += currentSentence;
        break;
      }
    }

    progressBar.value = savedGlobal;
    progressCur.textContent = savedGlobal;
    progressTot.textContent = totalSentences;
    readAlong.innerHTML = currentChunk > 0 || currentSentence > 0
      ? '<span style="color:#666">Resuming from saved position...</span>'
      : '';
    isPlaying = false;
    playPauseBtn.innerHTML = '&#9654;';

    document.querySelectorAll('.section-item.active').forEach(el => el.classList.remove('active'));
    if (currentChunk > 0) {
      const secEl = document.querySelector(`.section-item[data-chunk="${currentChunk}"]`);
      if (secEl) secEl.classList.add('active');
    }
  }

  async function openDocument(docId) {
    try {
      const resp = await fetch(`/api/reader/documents/${docId}`);
      const data = await resp.json();
      currentDocId = docId;
      stopAppendPoll();
      closeEditBar();

      listView.style.display = 'none';
      playerDiv.classList.add('active');

      renderDocument(data.document, { preservePosition: true });
    } catch (e) {
      console.error('Failed to open document:', e);
    }
  }

  playerBack.addEventListener('click', () => {
    if (isPlaying) sendPause();
    clearAudioQueue();
    stopAppendPoll();
    closeEditBar();
    playerDiv.classList.remove('active');
    listView.style.display = '';
    currentDocId = null;
    currentDoc = null;
    loadDocList();
  });

  function sendPlay(chunkIdx, sentIdx) {
    if (!ws || ws.readyState !== WebSocket.OPEN || !currentDocId) return;

    if (isPlaying) ws.send(JSON.stringify({ type: 'reader_pause' }));
    clearAudioQueue();

    playSeqId++;
    const mySeq = playSeqId;
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
      isPlaying = true;
      playPauseBtn.innerHTML = '&#10074;&#10074;';
    }, 100);
  }

  function sendPause() {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    playSeqId++;
    ws.send(JSON.stringify({ type: 'reader_pause' }));
    clearAudioQueue();
    isPlaying = false;
    playPauseBtn.innerHTML = '&#9654;';
  }

  function seekTo(chunkIdx, sentIdx) {
    currentChunk = chunkIdx;
    currentSentence = sentIdx;

    document.querySelectorAll('.section-item.active').forEach(el => el.classList.remove('active'));
    const secEl = document.querySelector(`.section-item[data-chunk="${chunkIdx}"]`);
    if (secEl) secEl.classList.add('active');

    if (isPlaying) sendPlay(chunkIdx, sentIdx);
  }

  playPauseBtn.addEventListener('click', () => {
    if (isPlaying) sendPause();
    else sendPlay(currentChunk, currentSentence);
  });

  skipBack.addEventListener('click', () => {
    const newChunk = Math.max(0, currentChunk - 1);
    seekTo(newChunk, 0);
  });

  skipFwd.addEventListener('click', () => {
    const sections = currentDoc?.sections || [];
    const maxChunk = sections.length > 0 ? sections[sections.length - 1].index : 0;
    const newChunk = Math.min(maxChunk, currentChunk + 1);
    seekTo(newChunk, 0);
  });

  progressBar.addEventListener('input', () => {
    clearTimeout(seekDebounce);
    seekDebounce = setTimeout(() => {
      const target = parseInt(progressBar.value, 10);
      const sections = currentDoc?.sections || [];
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

  const params = new URLSearchParams(location.search);
  const inboxId = params.get('inbox_id');
  if (inboxId) {
    fetch('/api/reader/documents', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ source: 'inbox', saved_item_id: parseInt(inboxId, 10) }),
    }).then(() => loadDocList());
  }

  const docIdParam = params.get('doc_id');
  if (docIdParam) openDocument(parseInt(docIdParam, 10));

  connectWS();
  if (!inboxId && !docIdParam) loadDocList();
  else if (!docIdParam) loadDocList();
})();
