(function() {
  const statusEl = document.getElementById('status');
  const talkBtn = document.getElementById('talk-btn');
  const userBlock = document.getElementById('user-block');
  const userText = document.getElementById('user-text');
  const assistBlock = document.getElementById('assist-block');
  const assistText = document.getElementById('assistant-text');
  const resetBtn = document.getElementById('reset-btn');
  const stopBtn = document.getElementById('stop-btn');
  const settingsBtn = document.getElementById('settings-btn');
  const settingsOver = document.getElementById('settings-overlay');
  const settingsClose = document.getElementById('settings-close');
  const voiceSelect = document.getElementById('voice-select');
  const speedSlider = document.getElementById('speed-slider');
  const speedValue = document.getElementById('speed-value');
  const trimCb = document.getElementById('trim-cb');
  const sttToggle = document.getElementById('stt-toggle');
  const ttsToggle = document.getElementById('tts-toggle');
  const textInputArea = document.getElementById('text-input-area');
  const textInput = document.getElementById('text-input');
  const textSend = document.getElementById('text-send');
  const toggleTalkCb = document.getElementById('toggle-talk-cb');
  const historyBtn = document.getElementById('history-btn');
  const historyBtnBot = document.getElementById('history-btn-bottom');
  const historyOver = document.getElementById('history-overlay');
  const historyClose = document.getElementById('history-close');
  const historyList = document.getElementById('history-list');
  const userExpand = document.getElementById('user-expand');
  const assistExpand = document.getElementById('assist-expand');
  const fullConvBtn = document.getElementById('full-conv-btn');
  const fullConvOverlay = document.getElementById('full-conv-overlay');
  const fullConvClose = document.getElementById('full-conv-close');
  const fullConvMessages = document.getElementById('full-conv-messages');

  let ws = null;
  let mediaRecorder = null;
  let audioChunks = [];
  let reconnectDelay = 1000;

  // Streaming STT state
  let sttAudioCtx = null;
  let sttStream = null;
  let sttProcessor = null;
  let sttChunks = [];
  let sttSendTimer = null;
  let sttStreaming = false;
  let currentVoice = 'de_male';
  let sttEnabled = true;
  let ttsEnabled = true;
  let toggleToTalk = false;
  let currentConversationId = null;
  let conversationHistory = [];

  try {
    const stored = localStorage.getItem('octavius-conv-id');
    if (stored) currentConversationId = parseInt(stored, 10);
  } catch (_err) {
  }

  if (typeof marked !== 'undefined') {
    marked.setOptions({ breaks: true, gfm: true });
  }

  const audioPlayer = OctaviusVoiceAudio.createStreamingAudioPlayer({
    getPlaybackRate() {
      return parseFloat(speedSlider.value);
    },
    shouldTrimSilence() {
      return trimCb.checked;
    },
    onPlaybackStart() {
      stopBtn.classList.add('visible');
    },
    onPlaybackIdle() {
      stopBtn.classList.remove('visible');
      setStatus('Ready');
      talkBtn.disabled = false;
      textSend.disabled = !textInput.value.trim();
    },
  });

  function renderMarkdown(text) {
    if (typeof marked !== 'undefined' && text) {
      return marked.parse(text);
    }
    return OctaviusApp.escapeHtml(text).replace(/\n/g, '<br>');
  }

  function checkOverflow(el) {
    const isOverflowing = el.scrollHeight > el.clientHeight + 2;
    el.classList.toggle('overflowing', isOverflowing);
    return isOverflowing;
  }

  function toggleExpand(contentEl, btnEl) {
    const expanded = contentEl.classList.toggle('expanded');
    btnEl.innerHTML = expanded ? '&#9650;' : '&#9660;';
  }

  document.getElementById('user-label').addEventListener('click', () => toggleExpand(userText, userExpand));
  document.getElementById('assist-label').addEventListener('click', () => toggleExpand(assistText, assistExpand));

  function setUserText(text) {
    if (!text) {
      userBlock.style.display = 'none';
      return;
    }
    userBlock.style.display = '';
    userText.textContent = text;
    userText.classList.remove('expanded');
    userExpand.innerHTML = '&#9660;';
    requestAnimationFrame(() => {
      const overflow = checkOverflow(userText);
      userExpand.style.display = overflow ? '' : 'none';
    });
  }

  function setAssistantText(text) {
    if (!text) {
      assistBlock.style.display = 'none';
      return;
    }
    assistBlock.style.display = '';
    assistText.innerHTML = renderMarkdown(text);
    assistText.classList.remove('expanded');
    assistExpand.innerHTML = '&#9660;';
    requestAnimationFrame(() => {
      const overflow = checkOverflow(assistText);
      assistExpand.style.display = overflow ? '' : 'none';
    });
  }

  function loadPrefs() {
    try {
      return JSON.parse(localStorage.getItem('octavius-prefs')) || {};
    } catch (_err) {
      return {};
    }
  }

  function savePrefs(prefs) {
    localStorage.setItem('octavius-prefs', JSON.stringify(prefs));
  }

  function getVoicePrefs(voice) {
    const prefs = loadPrefs();
    const voicePrefs = (prefs.voices || {})[voice];
    return voicePrefs || { speed: 1.4, trim: true };
  }

  function saveVoicePrefs(voice, speed, trim) {
    const prefs = loadPrefs();
    if (!prefs.voices) prefs.voices = {};
    prefs.voices[voice] = { speed, trim };
    prefs.voice = voice;
    savePrefs(prefs);
  }

  function applyVoicePrefs(voice) {
    const voicePrefs = getVoicePrefs(voice);
    speedSlider.value = voicePrefs.speed;
    speedValue.textContent = parseFloat(voicePrefs.speed).toFixed(1) + 'x';
    trimCb.checked = voicePrefs.trim;
    audioPlayer.updatePlaybackRate();
  }

  function persistCurrent() {
    saveVoicePrefs(currentVoice, parseFloat(speedSlider.value), trimCb.checked);
  }

  function setStatus(text, active) {
    statusEl.textContent = text;
    statusEl.classList.toggle('active', !!active);
  }

  function sendSettings() {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'settings', voice: currentVoice, tts: ttsEnabled }));
    }
  }

  function updateInputMode() {
    if (sttEnabled) {
      talkBtn.style.display = '';
      textInputArea.classList.remove('visible');
      talkBtn.innerHTML = toggleToTalk ? 'TAP TO<br>TALK' : 'HOLD TO<br>TALK';
    } else {
      talkBtn.style.display = 'none';
      textInputArea.classList.add('visible');
      textInput.focus();
    }

    sttToggle.classList.toggle('off', !sttEnabled);
    ttsToggle.classList.toggle('off', !ttsEnabled);
    toggleTalkCb.checked = toggleToTalk;

    const prefs = loadPrefs();
    prefs.sttEnabled = sttEnabled;
    prefs.ttsEnabled = ttsEnabled;
    prefs.toggleToTalk = toggleToTalk;
    savePrefs(prefs);
  }

  function submitText() {
    const text = textInput.value.trim();
    if (!text || !ws || ws.readyState !== WebSocket.OPEN) return;
    ws.send(JSON.stringify({ type: 'text_input', text }));
    textInput.value = '';
    textSend.disabled = true;
    setStatus('Sending...', true);
  }

  function openHistory() {
    historyOver.classList.add('open');
    loadHistory();
  }

  function formatHistoryDate(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    const now = new Date();
    const diff = now - d;
    if (diff < 86400000) {
      return 'Today ' + d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    }
    if (diff < 172800000) {
      return 'Yesterday ' + d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    }
    return d.toLocaleDateString([], { month: 'short', day: 'numeric' }) + ' ' +
      d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  }

  async function loadHistory() {
    historyList.innerHTML = '<div class="history-empty">Loading...</div>';
    try {
      const resp = await fetch('/api/conversations?limit=20');
      const data = await resp.json();
      const conversations = data.conversations || [];
      if (conversations.length === 0) {
        historyList.innerHTML = '<div class="history-empty">No conversations yet.</div>';
        return;
      }
      historyList.innerHTML = '';
      for (const conversation of conversations) {
        const item = document.createElement('div');
        item.className = 'history-item';
        let tagsHtml = '';
        if (conversation.tags && conversation.tags.length) {
          tagsHtml = '<div class="hi-tags">' +
            conversation.tags.map((tag) => `<span class="hi-tag">${tag}</span>`).join('') +
            '</div>';
        }
        item.innerHTML = `
          <div class="hi-date">${formatHistoryDate(conversation.started_at)} &middot; ${conversation.message_count} msgs</div>
          <div class="hi-summary">${conversation.summary || 'No summary'}</div>
          ${tagsHtml}
        `;
        item.addEventListener('click', () => loadConversation(conversation.id));
        historyList.appendChild(item);
      }
    } catch (_err) {
      historyList.innerHTML = '<div class="history-empty">Failed to load.</div>';
    }
  }

  function loadConversation(conversationId) {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'load_conversation', conversation_id: conversationId }));
    }
    historyOver.classList.remove('open');
  }

  function connect() {
    const socketController = OctaviusApp.createWebSocket({
      binaryType: 'arraybuffer',
      getReconnectDelayMs() {
        const delay = reconnectDelay;
        reconnectDelay = Math.min(reconnectDelay * 2, 15000);
        return delay;
      },
      onOpen(_evt, socket) {
        ws = socket;
        setStatus('Ready');
        talkBtn.disabled = false;
        textSend.disabled = !textInput.value.trim();
        reconnectDelay = 1000;
        sendSettings();
        if (currentConversationId) {
          ws.send(JSON.stringify({ type: 'restore_session', conversation_id: currentConversationId }));
        }
      },
      onMessage(evt) {
        ws = socketController.getSocket();
        if (evt.data instanceof ArrayBuffer) {
          setStatus('Speaking...', true);
          audioPlayer.enqueueAudio(evt.data);
          return;
        }
        if (typeof evt.data !== 'string') return;

        const msg = JSON.parse(evt.data);
        if (msg.type === 'status') {
          if (msg.text === 'audio_done') {
            audioPlayer.signalDone();
          } else {
            setStatus(msg.text, true);
            if (msg.text === 'Ready') {
              talkBtn.disabled = false;
              textSend.disabled = !textInput.value.trim();
            }
          }
        }
        if (msg.type === 'session_id') {
          currentConversationId = msg.conversation_id;
          try {
            localStorage.setItem('octavius-conv-id', msg.conversation_id);
          } catch (_err) {
          }
        }
        if (msg.type === 'transcript_partial') {
          setUserText(msg.text);
        }
        if (msg.type === 'transcript') {
          setUserText(msg.text);
          conversationHistory.push({ role: 'user', content: msg.text });
        }
        if (msg.type === 'response') {
          setAssistantText(msg.text);
          conversationHistory.push({ role: 'assistant', content: msg.text });
          if (!ttsEnabled) {
            setStatus('Ready');
            talkBtn.disabled = false;
            textSend.disabled = !textInput.value.trim();
          }
        }
        if (msg.type === 'conversation_loaded') {
          const messages = msg.messages || [];
          conversationHistory = messages.map((message) => ({ role: message.role, content: message.content }));
          let lastUser = '';
          let lastAssistant = '';
          for (const message of messages) {
            if (message.role === 'user') lastUser = message.content;
            if (message.role === 'assistant') lastAssistant = message.content;
          }
          setUserText(lastUser);
          setAssistantText(lastAssistant);
          setStatus(`Resumed conversation #${msg.conversation_id}`, true);
          setTimeout(() => setStatus('Ready'), 2000);
        }
      },
      onClose() {
        talkBtn.disabled = true;
        textSend.disabled = true;
        setStatus('Disconnected. Reconnecting...');
      },
    });
    ws = socketController.connect();
  }

  // --- Streaming STT helpers ---

  function sendPCMChunks() {
    if (!sttChunks.length) return;
    let total = 0;
    for (const c of sttChunks) total += c.length;
    const combined = new Float32Array(total);
    let off = 0;
    for (const c of sttChunks) { combined.set(c, off); off += c.length; }
    sttChunks = [];

    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(new Uint8Array(combined.buffer));
    }
  }

  async function startRecording() {
    if (!sttEnabled) return;
    try {
      sttStream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, sampleRate: 16000, echoCancellation: true, noiseSuppression: true },
      });
      sttAudioCtx = new AudioContext({ sampleRate: 16000 });
      const src = sttAudioCtx.createMediaStreamSource(sttStream);
      sttProcessor = sttAudioCtx.createScriptProcessor(4096, 1, 1);
      sttProcessor.onaudioprocess = (e) => {
        if (sttStreaming) sttChunks.push(new Float32Array(e.inputBuffer.getChannelData(0)));
      };
      src.connect(sttProcessor);
      sttProcessor.connect(sttAudioCtx.destination);

      sttStreaming = true;
      sttChunks = [];
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'stt_start' }));
      }
      sttSendTimer = setInterval(sendPCMChunks, 1500);

      talkBtn.classList.add('recording');
      setStatus('Recording...', true);
    } catch (_err) {
      setStatus('Microphone access denied.');
    }
  }

  function stopRecording() {
    if (!sttStreaming) return;
    sttStreaming = false;

    if (sttSendTimer) { clearInterval(sttSendTimer); sttSendTimer = null; }

    // Send any remaining audio
    sendPCMChunks();

    // Signal stop to server
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'stt_stop' }));
      talkBtn.disabled = true;
      setStatus('Transcribing...', true);
    }

    // Clean up audio resources
    if (sttStream) { sttStream.getTracks().forEach((t) => t.stop()); sttStream = null; }
    if (sttAudioCtx) { sttAudioCtx.close(); sttAudioCtx = null; }
    sttProcessor = null;

    talkBtn.classList.remove('recording');
  }

  function handleTalkDown(event) {
    event.preventDefault();
    if (toggleToTalk) {
      if (sttStreaming) {
        stopRecording();
      } else {
        startRecording();
      }
    } else {
      startRecording();
    }
  }

  function handleTalkUp() {
    if (!toggleToTalk) stopRecording();
  }

  function openFullConversation() {
    fullConvMessages.innerHTML = '';
    const messages = conversationHistory.slice(0, -1);

    if (messages.length === 0) {
      fullConvMessages.innerHTML = '<div class="conv-empty">No prior messages in this session.</div>';
      fullConvOverlay.classList.add('open');
      return;
    }

    for (const message of messages) {
      const div = document.createElement('div');
      div.className = 'conv-msg ' + message.role;
      if (message.role === 'assistant') {
        div.innerHTML = renderMarkdown(message.content);
      } else {
        div.textContent = message.content;
      }
      fullConvMessages.appendChild(div);
    }

    fullConvMessages.scrollTop = fullConvMessages.scrollHeight;
    fullConvOverlay.classList.add('open');
  }

  function updateInboxBadge() {
    fetch('/api/inbox?status=pending&limit=1&offset=0')
      .then((response) => response.json())
      .then((data) => {
        const badge = document.getElementById('inbox-badge');
        const count = data.items ? data.items.length : 0;
        if (count > 0) {
          badge.style.display = 'block';
          badge.textContent = '';
          badge.style.minWidth = '8px';
          badge.style.height = '8px';
          badge.style.borderRadius = '4px';
        } else {
          badge.style.display = 'none';
        }
      })
      .catch(() => {});
  }

  const prefs = loadPrefs();
  if (prefs.voice) currentVoice = prefs.voice;
  if (prefs.sttEnabled !== undefined) sttEnabled = prefs.sttEnabled;
  if (prefs.ttsEnabled !== undefined) ttsEnabled = prefs.ttsEnabled;
  if (prefs.toggleToTalk !== undefined) toggleToTalk = prefs.toggleToTalk;
  applyVoicePrefs(currentVoice);
  updateInputMode();

  sttToggle.addEventListener('click', () => {
    sttEnabled = !sttEnabled;
    updateInputMode();
  });

  ttsToggle.addEventListener('click', () => {
    ttsEnabled = !ttsEnabled;
    updateInputMode();
    sendSettings();
  });

  textSend.addEventListener('click', submitText);
  textInput.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      submitText();
    }
  });
  textInput.addEventListener('input', () => {
    textSend.disabled = !textInput.value.trim();
  });

  settingsBtn.addEventListener('click', () => settingsOver.classList.add('open'));
  settingsClose.addEventListener('click', () => settingsOver.classList.remove('open'));
  settingsOver.addEventListener('click', (event) => {
    if (event.target === settingsOver) settingsOver.classList.remove('open');
  });

  OctaviusApp.loadVoices(voiceSelect, {
    currentVoice,
    onLoaded(_data, resolvedVoice) {
      currentVoice = resolvedVoice;
      applyVoicePrefs(currentVoice);
    },
  }).catch(() => {
    const opt = document.createElement('option');
    opt.value = 'de_male';
    opt.textContent = 'de male';
    voiceSelect.appendChild(opt);
  });

  voiceSelect.addEventListener('change', () => {
    currentVoice = voiceSelect.value;
    applyVoicePrefs(currentVoice);
    sendSettings();
    persistCurrent();
  });

  speedSlider.addEventListener('input', () => {
    speedValue.textContent = parseFloat(speedSlider.value).toFixed(1) + 'x';
    persistCurrent();
    audioPlayer.updatePlaybackRate();
  });

  trimCb.addEventListener('change', persistCurrent);

  toggleTalkCb.addEventListener('change', () => {
    toggleToTalk = toggleTalkCb.checked;
    updateInputMode();
  });

  historyBtn.addEventListener('click', openHistory);
  historyBtnBot.addEventListener('click', openHistory);
  historyClose.addEventListener('click', () => historyOver.classList.remove('open'));
  historyOver.addEventListener('click', (event) => {
    if (event.target === historyOver) historyOver.classList.remove('open');
  });

  stopBtn.addEventListener('click', () => audioPlayer.stop());

  talkBtn.addEventListener('mousedown', handleTalkDown);
  talkBtn.addEventListener('mouseup', handleTalkUp);
  talkBtn.addEventListener('mouseleave', handleTalkUp);
  talkBtn.addEventListener('touchstart', handleTalkDown);
  talkBtn.addEventListener('touchend', handleTalkUp);
  talkBtn.addEventListener('touchcancel', handleTalkUp);

  resetBtn.addEventListener('click', () => {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'reset' }));
      currentConversationId = null;
      conversationHistory = [];
      try {
        localStorage.removeItem('octavius-conv-id');
      } catch (_err) {
      }
      setUserText('');
      setAssistantText('');
      audioPlayer.stop();
    }
  });

  fullConvBtn.addEventListener('click', (event) => {
    event.stopPropagation();
    openFullConversation();
  });
  fullConvClose.addEventListener('click', () => {
    fullConvOverlay.classList.remove('open');
  });
  fullConvOverlay.addEventListener('click', (event) => {
    if (event.target === fullConvOverlay) fullConvOverlay.classList.remove('open');
  });

  connect();
  updateInboxBadge();
  setInterval(updateInboxBadge, 60000);
})();
