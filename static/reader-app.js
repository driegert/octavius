(function() {
  const listView = document.getElementById('list-view');
  const docList = document.getElementById('doc-list');
  const ingestInput = document.getElementById('ingest-input');
  const ingestBtn = document.getElementById('ingest-btn');
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
        card.innerHTML = `
          <span class="dc-title">${OctaviusApp.escapeHtml(doc.title)}</span>
          <span class="dc-status ${doc.status}">${doc.status}</span>
          ${retryButton}
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

  async function openDocument(docId) {
    try {
      const resp = await fetch(`/api/reader/documents/${docId}`);
      const data = await resp.json();
      currentDoc = data.document;
      currentDocId = docId;
      totalSentences = currentDoc.total_sentences || 0;

      listView.style.display = 'none';
      playerDiv.classList.add('active');
      playerTitle.textContent = currentDoc.title;

      sectionsEl.innerHTML = '';
      const sections = currentDoc.sections || [];
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
      currentChunk = currentDoc.last_chunk || 0;
      currentSentence = currentDoc.last_sentence || 0;

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

      if (currentChunk > 0) {
        const secEl = document.querySelector(`.section-item[data-chunk="${currentChunk}"]`);
        if (secEl) secEl.classList.add('active');
      }
    } catch (e) {
      console.error('Failed to open document:', e);
    }
  }

  playerBack.addEventListener('click', () => {
    if (isPlaying) sendPause();
    clearAudioQueue();
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
