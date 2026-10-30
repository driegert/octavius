(function() {
  const $ = (id) => document.getElementById(id);

  const shell = $('shell');
  const statusEl = $('status');
  const hintEl = $('talk-hint');
  const connEl = $('conn-state');
  const talkBtn = $('talk-btn');
  const stopBtn = $('stop-btn');
  const resetBtn = $('reset-btn');
  const transcriptEl = $('transcript');
  const columnEl = $('transcript-column');
  const emptyEl = $('empty-state');
  const emptyGreeting = $('empty-greeting');
  const emptyHint = $('empty-hint');
  const convTitle = $('conv-title');
  const voiceSelect = $('voice-select');
  const speedSlider = $('speed-slider');
  const speedValue = $('speed-value');
  const trimCb = $('trim-cb');
  const normalizeCb = $('normalize-cb');
  const proactiveSpeakCb = $('proactive-speak-cb');
  const sttToggle = $('stt-toggle');
  const ttsToggle = $('tts-toggle');
  const textForm = $('text-form');
  const textInput = $('text-input');
  const textSend = $('text-send');
  const talkModeGroup = $('talk-mode-group');
  const talkModeHelp = $('talk-mode-help');
  const historyList = $('history-list');
  const agentsSection = $('agents-section');
  const agentsList = $('agents-list');
  const tabHistory = $('tab-history');
  const tabSettings = $('tab-settings');
  const paneHistory = $('pane-history');
  const paneSettings = $('pane-settings');
  const railOpen = $('rail-open');
  const railClose = $('rail-close');
  const scrim = $('scrim');
  const inboxBadge = $('inbox-badge');

  let ws = null;
  let connected = false;
  let reconnectDelay = 1000;

  // Streaming STT state
  let sttAudioCtx = null;
  let sttStream = null;
  let sttProcessor = null;
  let sttChunks = [];
  let sttSendTimer = null;
  let sttStreaming = false;
  let micStarting = false;   // getUserMedia in flight
  let releaseWhileStarting = false; // hold released before the mic opened
  let micLevel = 0;

  // Turn state, driven by server `status` frames
  let phase = 'idle'; // idle | transcribing | thinking | speaking
  let dropAudio = false; // after barge-in, ignore stale audio until the next transcript

  let currentVoice = 'de_male';
  let sttEnabled = true;
  let ttsEnabled = true;
  let talkMode = 'hold';
  let continuousActive = false;
  let currentConversationId = null;
  let viewingConversationId = null;
  let conversationHistory = [];
  let proactiveSpeak = false;

  // Transcript elements that are still changing
  let liveUserEl = null;
  let streamingEl = null;
  let streamingText = '';
  let pendingConfirm = null;

  // Parked delegations keyed by handle (dormant feature, kept wired).
  const delegations = new Map();

  try {
    const stored = localStorage.getItem('octavius-conv-id');
    if (stored) currentConversationId = parseInt(stored, 10);
  } catch (_err) {
  }

  // ---------- Markdown ----------

  if (typeof marked !== 'undefined') {
    marked.setOptions({ breaks: true, gfm: true });
    try {
      // Show raw HTML in replies as text rather than injecting it.
      marked.use({
        renderer: {
          html(token) {
            const raw = typeof token === 'string' ? token : (token && (token.raw || token.text)) || '';
            return OctaviusApp.escapeHtml(raw);
          },
        },
        // Replies can quote web pages: only web and mail links survive.
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

  // ---------- Audio ----------

  const audioPlayer = OctaviusVoiceAudio.createStreamingAudioPlayer({
    getPlaybackRate() {
      return parseFloat(speedSlider.value);
    },
    shouldTrimSilence() {
      return trimCb.checked;
    },
    shouldNormalizeVolume() {
      return normalizeCb.checked;
    },
    onPlaybackStart() {
      stopBtn.hidden = false;
      phase = 'speaking';
      refreshUi();
    },
    onPlaybackIdle() {
      stopBtn.hidden = true;
      if (sttStreaming || micStarting) return; // barge-in already moved on
      phase = 'idle';
      if (continuousActive && sttEnabled && connected) {
        startRecording();
      } else {
        setStatus(connected ? 'Ready' : 'Disconnected. Reconnecting…');
      }
      refreshUi();
    },
  });

  const orb = OctaviusOrb.create($('orb-canvas'), {
    getLevel(state) {
      if (state === 'listening') return micLevel;
      if (state === 'speaking') return audioPlayer.getLevel();
      return 0;
    },
  });

  // ---------- Status + orb ----------

  function toneFor(text) {
    if (sttStreaming) return 'listening';
    if (/error|failed|denied|couldn't/i.test(text)) return 'error';
    if (phase === 'speaking') return 'speaking';
    if (phase !== 'idle') return 'busy';
    return '';
  }

  function setStatus(text) {
    statusEl.textContent = text;
    statusEl.dataset.tone = toneFor(text);
  }

  function orbState() {
    if (!connected) return 'offline';
    if (sttStreaming) return 'listening';
    if (phase === 'transcribing') return 'transcribing';
    if (phase === 'thinking') return 'thinking';
    if (phase === 'speaking') return 'speaking';
    return sttEnabled ? 'idle' : 'disabled';
  }

  function talkLabel() {
    if (!sttEnabled) return 'Microphone off';
    if (talkMode === 'continuous') {
      return continuousActive ? 'End hands-free conversation' : 'Start hands-free conversation';
    }
    if (talkMode === 'toggle') return sttStreaming ? 'Stop talking' : 'Tap to talk';
    return 'Hold to talk';
  }

  const coarse = window.matchMedia && window.matchMedia('(pointer: coarse)').matches;
  // Touch devices have no Space bar: name the orb instead.
  const KEY = coarse ? 'the orb' : 'Space';

  function hintText() {
    if (!connected) return '';
    if (!sttEnabled) return 'Microphone is off. Write below.';
    if (sttStreaming) {
      if (talkMode === 'hold') return 'Release to send';
      if (talkMode === 'toggle') return coarse ? 'Tap the orb again to send' : 'Press Space or the orb again to send';
      return `Pauses end your turn. Press ${KEY} to stop.`;
    }
    if (phase === 'speaking') return 'Talk to interrupt';
    if (phase !== 'idle') return '';
    if (talkMode === 'continuous') {
      return continuousActive ? `Hands-free. Press ${KEY} to end.` : `Press ${KEY} to start hands-free`;
    }
    if (talkMode === 'toggle') return `Press ${KEY} to talk`;
    return `Hold ${KEY} to talk`;
  }

  function refreshUi() {
    orb.setState(orbState());
    talkBtn.disabled = !connected || !sttEnabled;
    talkBtn.setAttribute('aria-label', talkLabel());
    talkBtn.title = talkLabel();
    hintEl.textContent = hintText();
    textSend.disabled = !connected || !textInput.value.trim();
    statusEl.dataset.tone = toneFor(statusEl.textContent);
  }

  function updateEmptyHint() {
    const h = new Date().getHours();
    emptyGreeting.textContent = h < 5 ? 'Up late, Dave.' : h < 12 ? 'Good morning, Dave.' : h < 18 ? 'Good afternoon, Dave.' : 'Good evening, Dave.';
    if (!sttEnabled) emptyHint.textContent = 'Write below to start.';
    else if (talkMode === 'continuous') emptyHint.textContent = `Press ${KEY} to start a hands-free conversation, or write below.`;
    else if (talkMode === 'toggle') emptyHint.textContent = `Press ${KEY} to talk, or write below.`;
    else emptyHint.textContent = `Hold ${KEY} to talk, or write below.`;
  }

  // ---------- Transcript ----------

  function nearBottom() {
    return transcriptEl.scrollHeight - transcriptEl.scrollTop - transcriptEl.clientHeight < 120;
  }

  function scrollToEnd(force) {
    if (force || nearBottom()) {
      requestAnimationFrame(() => { transcriptEl.scrollTop = transcriptEl.scrollHeight; });
    }
  }

  function syncEmpty() {
    emptyEl.hidden = columnEl.querySelector('.turn') !== null;
  }

  function addTurn(className, animate) {
    const el = document.createElement('div');
    el.className = 'turn ' + className + (animate ? ' turn-enter' : '');
    columnEl.appendChild(el);
    syncEmpty();
    return el;
  }

  function clearTranscript() {
    columnEl.querySelectorAll('.turn').forEach((el) => el.remove());
    liveUserEl = null;
    streamingEl = null;
    streamingText = '';
    pendingConfirm = null;
    syncEmpty();
  }

  function showPartial(text) {
    if (!text) return;
    if (!liveUserEl) {
      const stick = nearBottom();
      liveUserEl = addTurn('user live', true);
      scrollToEnd(stick);
    }
    liveUserEl.textContent = text;
    scrollToEnd();
  }

  function commitUser(text) {
    const stick = nearBottom();
    const el = liveUserEl || addTurn('user', true);
    el.classList.remove('live');
    el.textContent = text;
    liveUserEl = null;
    // Octavius is now working on a reply.
    streamingEl = addTurn('assistant pending', false);
    streamingText = '';
    scrollToEnd(stick || true);
  }

  function dropLiveUser() {
    if (liveUserEl) {
      liveUserEl.remove();
      liveUserEl = null;
      syncEmpty();
    }
  }

  function appendDelta(text) {
    if (!text) return;
    if (!streamingEl) streamingEl = addTurn('assistant', false);
    streamingEl.classList.remove('pending');
    streamingEl.classList.add('streaming');
    streamingText += text;
    streamingEl.innerHTML = renderMarkdown(streamingText);
    scrollToEnd();
  }

  function commitAssistant(text) {
    const el = streamingEl || addTurn('assistant', true);
    el.classList.remove('pending', 'streaming');
    el.innerHTML = renderMarkdown(text);
    streamingEl = null;
    streamingText = '';
    scrollToEnd();
  }

  function settlePending() {
    // Turn ended without a reply (error, empty, cancelled): drop the caret.
    if (streamingEl) {
      if (!streamingText) {
        streamingEl.remove();
      } else {
        streamingEl.classList.remove('pending', 'streaming');
      }
      streamingEl = null;
      streamingText = '';
      syncEmpty();
    }
  }

  function addNote(text, isError) {
    const el = addTurn('note' + (isError ? ' error' : ''), true);
    el.textContent = text;
    scrollToEnd();
  }

  function renderConversation(messages) {
    clearTranscript();
    for (const message of messages) {
      if (message.role === 'user') {
        addTurn('user', false).textContent = message.content;
      } else if (message.role === 'assistant') {
        addTurn('assistant', false).innerHTML = renderMarkdown(message.content);
      }
    }
    syncEmpty();
    scrollToEnd(true);
  }

  function showConfirm(msg) {
    resolveConfirm();
    const el = addTurn('confirm', true);
    const title = document.createElement('p');
    title.className = 'confirm-title';
    title.textContent = msg.title || 'Confirm';
    el.appendChild(title);
    if (msg.message) {
      const body = document.createElement('p');
      body.className = 'confirm-message';
      body.textContent = msg.message;
      el.appendChild(body);
    }
    const actions = document.createElement('div');
    actions.className = 'confirm-actions';
    const yes = document.createElement('button');
    yes.className = 'btn primary';
    yes.textContent = 'Yes, go ahead';
    yes.addEventListener('click', () => sendText('Yes'));
    const no = document.createElement('button');
    no.className = 'btn';
    no.textContent = 'No';
    no.addEventListener('click', () => sendText('No'));
    const or = document.createElement('span');
    or.className = 'confirm-or';
    or.textContent = 'or answer out loud';
    actions.append(yes, no, or);
    el.appendChild(actions);
    pendingConfirm = el;
    scrollToEnd(true);
  }

  function resolveConfirm() {
    if (pendingConfirm) {
      pendingConfirm.classList.add('answered');
      pendingConfirm = null;
    }
  }

  // ---------- Preferences ----------

  function loadPrefs() {
    try {
      return JSON.parse(localStorage.getItem('octavius-prefs')) || {};
    } catch (_err) {
      return {};
    }
  }

  function savePrefs(prefs) {
    try {
      localStorage.setItem('octavius-prefs', JSON.stringify(prefs));
    } catch (_err) {
    }
  }

  function getVoicePrefs(voice) {
    const prefs = loadPrefs();
    const voicePrefs = (prefs.voices || {})[voice];
    return voicePrefs || { speed: 1.4, trim: true, normalize: true };
  }

  function saveVoicePrefs(voice, speed, trim, normalize) {
    const prefs = loadPrefs();
    if (!prefs.voices) prefs.voices = {};
    prefs.voices[voice] = { speed, trim, normalize };
    prefs.voice = voice;
    savePrefs(prefs);
  }

  function applyVoicePrefs(voice) {
    const voicePrefs = getVoicePrefs(voice);
    speedSlider.value = voicePrefs.speed;
    speedValue.textContent = parseFloat(voicePrefs.speed).toFixed(1) + 'x';
    trimCb.checked = voicePrefs.trim;
    normalizeCb.checked = voicePrefs.normalize !== false;
    audioPlayer.updatePlaybackRate();
  }

  function persistCurrent() {
    saveVoicePrefs(currentVoice, parseFloat(speedSlider.value), trimCb.checked, normalizeCb.checked);
  }

  const TALK_MODE_HELP = {
    hold: 'Hold the orb or Space while you speak. Letting go sends it.',
    toggle: 'Press once to start speaking and again to send.',
    continuous: 'Start once and talk back and forth. A pause ends your turn, and the mic reopens after each reply.',
  };

  function updateInputMode() {
    sttToggle.setAttribute('aria-pressed', String(sttEnabled));
    ttsToggle.setAttribute('aria-pressed', String(ttsEnabled));
    sttToggle.title = sttEnabled ? 'Microphone on' : 'Microphone off';
    ttsToggle.title = ttsEnabled ? 'Replies are spoken aloud' : 'Replies are text only';
    const radio = talkModeGroup.querySelector(`input[value="${talkMode}"]`);
    if (radio) radio.checked = true;
    talkModeHelp.textContent = TALK_MODE_HELP[talkMode] || '';
    updateEmptyHint();
    refreshUi();

    const prefs = loadPrefs();
    prefs.sttEnabled = sttEnabled;
    prefs.ttsEnabled = ttsEnabled;
    prefs.talkMode = talkMode;
    savePrefs(prefs);
  }

  // ---------- WebSocket ----------

  function send(obj) {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(obj));
      return true;
    }
    return false;
  }

  function sendSettings() {
    send({
      type: 'settings',
      voice: currentVoice,
      tts: ttsEnabled,
      proactive_speak: proactiveSpeak,
    });
  }

  function sendText(text) {
    if (!text || !send({ type: 'text_input', text })) return false;
    setStatus('Sending…');
    return true;
  }

  function submitText() {
    const text = textInput.value.trim();
    if (!text) return;
    if (sendText(text)) {
      textInput.value = '';
      autosize();
      refreshUi();
    }
  }

  function handleStatus(text) {
    if (text === 'audio_done') {
      audioPlayer.signalDone();
      settlePending();
      return;
    }
    if (text === 'Transcribing...') phase = 'transcribing';
    else if (text === 'Thinking...') phase = 'thinking';
    else if (text === 'Speaking...') phase = 'speaking';
    else if (text === 'Ready') phase = 'idle';
    else if (/^Couldn't hear anything/.test(text)) {
      phase = 'idle';
      dropLiveUser();
      // No audio_done follows an empty transcription, so re-arm here.
      if (continuousActive && sttEnabled) setTimeout(() => { if (continuousActive && !sttStreaming) startRecording(); }, 400);
    } else if (/^(Agent error|Transcription failed)/.test(text)) {
      addNote(text, true);
    } else if (phase === 'thinking' || phase === 'transcribing') {
      // Tool progress ("Searching the web…") while a turn runs.
    }

    if (!sttStreaming) setStatus(text);
    refreshUi();
  }

  function connect() {
    const socketController = OctaviusApp.createWebSocket({
      binaryType: 'arraybuffer',
      heartbeat: true,
      getReconnectDelayMs() {
        const delay = reconnectDelay;
        reconnectDelay = Math.min(reconnectDelay * 2, 15000);
        return delay;
      },
      onOpen(_evt, socket) {
        ws = socket;
        connected = true;
        connEl.dataset.state = 'open';
        connEl.textContent = 'Connected';
        phase = 'idle';
        setStatus('Ready');
        reconnectDelay = 1000;
        sendSettings();
        if (currentConversationId) {
          send({ type: 'restore_session', conversation_id: currentConversationId });
        }
        // Clear local list and resync with server snapshot.
        delegations.clear();
        renderAgents();
        send({ type: 'delegation_list' });
        refreshUi();
      },
      onMessage(evt) {
        ws = socketController.getSocket();
        if (evt.data instanceof ArrayBuffer) {
          if (dropAudio) return;
          audioPlayer.enqueueAudio(evt.data);
          return;
        }
        if (typeof evt.data !== 'string') return;

        let msg;
        try {
          msg = JSON.parse(evt.data);
        } catch (_err) {
          return;
        }
        if (!msg || typeof msg !== 'object') return;

        switch (msg.type) {
          case 'status':
            handleStatus(String(msg.text || ''));
            break;
          case 'session_id':
            currentConversationId = msg.conversation_id;
            try {
              localStorage.setItem('octavius-conv-id', msg.conversation_id);
            } catch (_err) {
            }
            break;
          case 'stt_auto_stop':
            // Server detected end of speech: clean up recording on our side.
            teardownMic();
            phase = 'transcribing';
            setStatus('Transcribing…');
            refreshUi();
            break;
          case 'transcript_partial':
            showPartial(msg.text);
            break;
          case 'transcript':
            dropAudio = false;
            resolveConfirm();
            commitUser(msg.text);
            conversationHistory.push({ role: 'user', content: msg.text });
            break;
          case 'response_delta':
            appendDelta(msg.text);
            break;
          case 'response':
            commitAssistant(msg.text);
            conversationHistory.push({ role: 'assistant', content: msg.text });
            if (conversationHistory.length <= 2) scheduleHistoryRefresh(1500);
            break;
          case 'conversation_loaded': {
            const messages = msg.messages || [];
            conversationHistory = messages.map((m) => ({ role: m.role, content: m.content }));
            viewingConversationId = msg.conversation_id;
            renderConversation(messages);
            const known = historyCache.find((c) => c.id === msg.conversation_id);
            convTitle.textContent = (known && known.summary) || `Conversation ${msg.conversation_id}`;
            markActiveHistory();
            setStatus('Picked up where you left off');
            setTimeout(() => { if (phase === 'idle' && !sttStreaming) setStatus('Ready'); }, 2000);
            break;
          }
          case 'confirm_request':
            showConfirm(msg);
            break;
          case 'delegation_update':
            applyDelegationUpdate(msg);
            break;
          case 'delegation_removed':
            applyDelegationRemoved(msg);
            break;
          default:
            // Unknown frame types are ignored.
            break;
        }
      },
      onClose() {
        connected = false;
        connEl.dataset.state = 'closed';
        connEl.textContent = 'Offline';
        if (continuousActive) {
          continuousActive = false;
        }
        if (sttStreaming || micStarting) teardownMic();
        settlePending();
        dropLiveUser();
        phase = 'idle';
        setStatus('Disconnected. Reconnecting…');
        refreshUi();
      },
    });
    ws = socketController.connect();
  }

  // ---------- Microphone ----------

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

  function teardownMic() {
    sttStreaming = false;
    if (sttSendTimer) { clearInterval(sttSendTimer); sttSendTimer = null; }
    if (sttStream) { sttStream.getTracks().forEach((t) => t.stop()); sttStream = null; }
    if (sttAudioCtx) { sttAudioCtx.close(); sttAudioCtx = null; }
    sttProcessor = null;
    sttChunks = [];
    micLevel = 0;
  }

  async function startRecording() {
    if (!sttEnabled || !connected || sttStreaming || micStarting) return;
    micStarting = true;
    releaseWhileStarting = false;
    try {
      sttStream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, sampleRate: 16000, echoCancellation: true, noiseSuppression: true },
      });
      sttAudioCtx = new AudioContext({ sampleRate: 16000 });
      const src = sttAudioCtx.createMediaStreamSource(sttStream);
      sttProcessor = sttAudioCtx.createScriptProcessor(4096, 1, 1);
      sttProcessor.onaudioprocess = (e) => {
        if (!sttStreaming) return;
        const data = e.inputBuffer.getChannelData(0);
        sttChunks.push(new Float32Array(data));
        let sum = 0;
        for (let i = 0; i < data.length; i++) sum += data[i] * data[i];
        micLevel = Math.min(1, Math.sqrt(sum / data.length) * 9);
      };
      src.connect(sttProcessor);
      sttProcessor.connect(sttAudioCtx.destination);

      sttStreaming = true;
      sttChunks = [];
      send({ type: 'stt_start' });
      sttSendTimer = setInterval(sendPCMChunks, 250);

      micStarting = false;
      setStatus(continuousActive ? 'Listening…' : 'Recording…');
      refreshUi();
      if (releaseWhileStarting && talkMode === 'hold') stopRecording();
    } catch (_err) {
      micStarting = false;
      teardownMic();
      if (continuousActive) continuousActive = false;
      setStatus('Microphone access denied. Allow it in the browser to talk.');
      refreshUi();
    }
  }

  function stopRecording() {
    if (micStarting) { releaseWhileStarting = true; return; }
    if (!sttStreaming) return;
    sttStreaming = false;
    if (sttSendTimer) { clearInterval(sttSendTimer); sttSendTimer = null; }

    // Send remaining audio, then signal stop to server.
    sendPCMChunks();
    if (send({ type: 'stt_stop' })) {
      phase = 'transcribing';
      setStatus('Transcribing…');
    }
    teardownMic();
    refreshUi();
  }

  function cancelRecording() {
    // Tear down audio without sending stt_stop (no utterance to process).
    if (micStarting) releaseWhileStarting = true;
    if (sttStreaming) teardownMic();
    dropLiveUser();
  }

  function bargeIn() {
    // Opening the mic mid-reply interrupts it: the server cancels the turn
    // on stt_start. Stop local playback and ignore any audio still in flight.
    if (audioPlayer.isActive() || phase === 'speaking' || phase === 'thinking') {
      dropAudio = true;
      audioPlayer.stop();
      settlePending();
    }
  }

  function startContinuousConversation() {
    continuousActive = true;
    bargeIn();
    startRecording();
    refreshUi();
  }

  function endContinuousConversation() {
    continuousActive = false;
    cancelRecording();
    if (phase === 'idle') setStatus('Ready');
    refreshUi();
  }

  function talkPress() {
    if (!connected || !sttEnabled) return;
    if (talkMode === 'continuous') {
      if (continuousActive) endContinuousConversation();
      else startContinuousConversation();
    } else if (talkMode === 'toggle') {
      if (sttStreaming || micStarting) stopRecording();
      else { bargeIn(); startRecording(); }
    } else {
      bargeIn();
      startRecording();
    }
  }

  function talkRelease() {
    if (talkMode === 'hold') stopRecording();
  }

  talkBtn.addEventListener('pointerdown', (event) => {
    if (event.button !== undefined && event.button !== 0) return;
    event.preventDefault();
    try { talkBtn.setPointerCapture(event.pointerId); } catch (_err) {}
    talkPress();
  });
  talkBtn.addEventListener('pointerup', talkRelease);
  talkBtn.addEventListener('pointercancel', talkRelease);
  talkBtn.addEventListener('lostpointercapture', talkRelease);
  talkBtn.addEventListener('contextmenu', (event) => event.preventDefault());
  // Keyboard activation of the focused orb (Enter); Space is handled globally.
  talkBtn.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.repeat) {
      event.preventDefault();
      talkPress();
    }
  });
  talkBtn.addEventListener('keyup', (event) => {
    if (event.key === 'Enter') talkRelease();
  });

  function isTypingTarget(el) {
    if (!el) return false;
    const tag = el.tagName;
    return tag === 'TEXTAREA' || tag === 'SELECT' || el.isContentEditable ||
      (tag === 'INPUT' && !['checkbox', 'radio', 'range', 'button'].includes(el.type));
  }

  let spaceHeld = false;
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
      if (shell.classList.contains('rail-open')) { closeRail(); return; }
      if (audioPlayer.isActive()) { audioPlayer.stop(); return; }
      if (continuousActive) { endContinuousConversation(); return; }
      if (sttStreaming) { cancelRecording(); setStatus('Ready'); refreshUi(); }
      return;
    }
    if (isTypingTarget(document.activeElement)) return;
    if (event.metaKey || event.ctrlKey || event.altKey) return;
    if (event.key === '/') {
      event.preventDefault();
      textInput.focus();
      return;
    }
    if (event.code === 'Space' || event.key === ' ') {
      const tgt = document.activeElement;
      if (tgt && tgt !== talkBtn && tgt !== document.body && tgt.tagName !== 'CANVAS' && (tgt.tagName === 'BUTTON' || tgt.tagName === 'A' || tgt.tagName === 'INPUT')) {
        return; // let Space activate the focused control
      }
      event.preventDefault();
      if (event.repeat || spaceHeld) return;
      spaceHeld = true;
      talkPress();
    }
  });
  document.addEventListener('keyup', (event) => {
    if ((event.code === 'Space' || event.key === ' ') && spaceHeld) {
      spaceHeld = false;
      talkRelease();
    }
  });
  window.addEventListener('blur', () => {
    if (spaceHeld) { spaceHeld = false; talkRelease(); }
  });

  // ---------- History rail ----------

  let historyCache = [];
  let historyTimer = null;

  function scheduleHistoryRefresh(ms) {
    clearTimeout(historyTimer);
    historyTimer = setTimeout(loadHistory, ms);
  }

  function dayGroup(iso) {
    if (!iso) return 'Earlier';
    const d = new Date(iso);
    const start = new Date();
    start.setHours(0, 0, 0, 0);
    const diffDays = Math.floor((start - new Date(d.getFullYear(), d.getMonth(), d.getDate())) / 86400000);
    if (diffDays <= 0) return 'Today';
    if (diffDays === 1) return 'Yesterday';
    if (diffDays < 7) return 'This week';
    return d.toLocaleDateString([], { month: 'long', year: 'numeric' });
  }

  function formatWhen(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    const group = dayGroup(iso);
    const time = d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
    if (group === 'Today' || group === 'Yesterday') return time;
    if (group === 'This week') return d.toLocaleDateString([], { weekday: 'short' }) + ' ' + time;
    return d.toLocaleDateString([], { month: 'short', day: 'numeric' });
  }

  async function loadHistory() {
    if (!historyCache.length) historyList.innerHTML = '<div class="history-empty">Loading conversations…</div>';
    try {
      const resp = await fetch('/api/conversations?limit=40');
      const data = await resp.json();
      historyCache = data.conversations || [];
      renderHistory();
    } catch (_err) {
      historyList.innerHTML = '<div class="history-empty">Couldn’t load conversations. They’ll appear when the server answers.</div>';
    }
  }

  function renderHistory() {
    if (historyCache.length === 0) {
      historyList.innerHTML = '<div class="history-empty">Past conversations will collect here.</div>';
      return;
    }
    historyList.innerHTML = '';
    let lastGroup = null;
    for (const conversation of historyCache) {
      const group = dayGroup(conversation.started_at);
      if (group !== lastGroup) {
        const h = document.createElement('div');
        h.className = 'hi-group';
        h.textContent = group;
        historyList.appendChild(h);
        lastGroup = group;
      }
      const item = document.createElement('button');
      item.type = 'button';
      item.className = 'history-item';
      item.dataset.id = conversation.id;
      const summary = conversation.summary || '';
      const count = conversation.message_count || 0;
      item.innerHTML = `
        <div class="hi-summary${summary ? '' : ' untitled'}">${OctaviusApp.escapeHtml(summary || 'Not summarised yet')}</div>
        <div class="hi-meta"><span>${OctaviusApp.escapeHtml(formatWhen(conversation.started_at))}</span><span>${count} ${count === 1 ? 'message' : 'messages'}</span></div>
      `;
      if (conversation.tags && conversation.tags.length) item.title = conversation.tags.join(', ');
      item.addEventListener('click', () => loadConversation(conversation.id));
      historyList.appendChild(item);
    }
    markActiveHistory();
  }

  function markActiveHistory() {
    historyList.querySelectorAll('.history-item').forEach((el) => {
      el.setAttribute('aria-current', String(Number(el.dataset.id) === viewingConversationId));
    });
  }

  function loadConversation(conversationId) {
    if (continuousActive) endContinuousConversation();
    send({ type: 'load_conversation', conversation_id: conversationId });
    closeRail();
  }

  // ---------- Background agents (dormant; rendered only when present) ----------

  const DOMAIN_LABELS = { email: 'Email', research: 'Research', tasks: 'Tasks' };

  function statusLabel(status) {
    if (status === 'ready') return 'Ready';
    if (status === 'running') return 'Working';
    if (status === 'failed') return 'Failed';
    return status || '';
  }

  function renderAgents() {
    const esc = OctaviusApp.escapeHtml;
    agentsSection.hidden = delegations.size === 0;
    if (delegations.size === 0) {
      agentsList.innerHTML = '';
      return;
    }
    const order = { ready: 0, running: 1, failed: 2 };
    const items = Array.from(delegations.values()).sort((a, b) => (order[a.status] ?? 3) - (order[b.status] ?? 3));
    agentsList.innerHTML = '';
    for (const rec of items) {
      const node = document.createElement('div');
      node.className = `agent-item ${rec.status}`;
      const domainLabel = DOMAIN_LABELS[rec.domain] || rec.domain;
      let body = '';
      let actions = '';
      if (rec.status === 'ready') {
        body = `<div class="ai-preview">${esc(rec.preview || '(no preview)')}</div>`;
        actions = `
          <button class="btn primary" data-act="merge" data-handle="${esc(rec.handle)}">Bring into this chat</button>
          <button class="btn" data-act="new" data-handle="${esc(rec.handle)}">Open as new</button>
          <button class="btn danger" data-act="dismiss" data-handle="${esc(rec.handle)}">Dismiss</button>`;
      } else if (rec.status === 'failed') {
        body = `<div class="ai-preview">${esc(rec.error || 'Unknown error.')}</div>`;
        actions = `<button class="btn danger" data-act="dismiss" data-handle="${esc(rec.handle)}">Dismiss</button>`;
      } else {
        actions = `<button class="btn danger" data-act="dismiss" data-handle="${esc(rec.handle)}">Cancel</button>`;
      }
      node.innerHTML = `
        <div class="ai-head"><span>${esc(domainLabel)}</span><span class="ai-status">${esc(statusLabel(rec.status))}</span></div>
        <div class="ai-task">${esc(rec.submitted_task || '')}</div>
        ${body}
        <div class="ai-actions">${actions}</div>`;
      agentsList.appendChild(node);
    }
  }

  function applyDelegationUpdate(msg) {
    delegations.set(msg.handle, {
      handle: msg.handle,
      domain: msg.domain,
      submitted_task: msg.submitted_task,
      status: msg.status,
      preview: msg.preview,
      error: msg.error,
    });
    renderAgents();
  }

  function applyDelegationRemoved(msg) {
    delegations.delete(msg.handle);
    renderAgents();
  }

  agentsList.addEventListener('click', (event) => {
    const target = event.target;
    if (!(target instanceof Element)) return;
    const btn = target.closest('button[data-act]');
    if (!btn) return;
    const action = btn.getAttribute('data-act');
    const handle = btn.getAttribute('data-handle');
    if (!action || !handle) return;
    if (action === 'dismiss') send({ type: 'delegation_dismiss', handle });
    else if (action === 'merge' || action === 'new') send({ type: 'delegation_pull', handle, mode: action });
  });

  // ---------- Rail tabs + drawer ----------

  function selectTab(which) {
    const settings = which === 'settings';
    tabHistory.setAttribute('aria-selected', String(!settings));
    tabSettings.setAttribute('aria-selected', String(settings));
    paneHistory.hidden = settings;
    paneSettings.hidden = !settings;
    try { localStorage.setItem('octavius-rail-tab', which); } catch (_err) {}
  }
  tabHistory.addEventListener('click', () => { selectTab('history'); loadHistory(); });
  tabSettings.addEventListener('click', () => selectTab('settings'));
  try { if (localStorage.getItem('octavius-rail-tab') === 'settings') selectTab('settings'); } catch (_err) {}

  function openRail() {
    shell.classList.add('rail-open');
    scrim.hidden = false;
    loadHistory();
  }
  function closeRail() {
    shell.classList.remove('rail-open');
    scrim.hidden = true;
  }
  railOpen.addEventListener('click', openRail);
  railClose.addEventListener('click', closeRail);
  scrim.addEventListener('click', closeRail);

  // ---------- Inbox badge ----------

  function updateInboxBadge() {
    fetch('/api/inbox?status=pending&limit=1&offset=0')
      .then((response) => response.json())
      .then((data) => {
        inboxBadge.hidden = !(data.items && data.items.length > 0);
      })
      .catch(() => {});
  }

  // ---------- Composer ----------

  function autosize() {
    textInput.style.height = 'auto';
    textInput.style.height = Math.min(textInput.scrollHeight, 160) + 'px';
  }

  textForm.addEventListener('submit', (event) => {
    event.preventDefault();
    submitText();
  });
  textInput.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      submitText();
    }
  });
  textInput.addEventListener('input', () => {
    autosize();
    refreshUi();
  });

  // ---------- Controls ----------

  sttToggle.addEventListener('click', () => {
    sttEnabled = !sttEnabled;
    if (!sttEnabled) {
      if (continuousActive) endContinuousConversation();
      else cancelRecording();
    }
    updateInputMode();
  });

  ttsToggle.addEventListener('click', () => {
    ttsEnabled = !ttsEnabled;
    if (!ttsEnabled) audioPlayer.stop();
    updateInputMode();
    sendSettings();
  });

  talkModeGroup.addEventListener('change', (event) => {
    const value = event.target && event.target.value;
    if (!value) return;
    if (continuousActive && value !== 'continuous') endContinuousConversation();
    if (sttStreaming && value !== talkMode) cancelRecording();
    talkMode = value;
    updateInputMode();
  });

  OctaviusApp.loadVoices(voiceSelect, {
    currentVoice,
    onLoaded(_data, resolvedVoice) {
      currentVoice = resolvedVoice;
      applyVoicePrefs(currentVoice);
      sendSettings();
    },
  }).catch(() => {
    const opt = document.createElement('option');
    opt.value = 'bm_lewis';
    opt.textContent = 'bm lewis';
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
  normalizeCb.addEventListener('change', persistCurrent);

  proactiveSpeakCb.addEventListener('change', () => {
    proactiveSpeak = proactiveSpeakCb.checked;
    sendSettings();
    const prefs = loadPrefs();
    prefs.proactiveSpeak = proactiveSpeak;
    savePrefs(prefs);
  });

  stopBtn.addEventListener('click', () => audioPlayer.stop());

  resetBtn.addEventListener('click', () => {
    if (!send({ type: 'reset' })) return;
    if (continuousActive) endContinuousConversation();
    cancelRecording();
    currentConversationId = null;
    viewingConversationId = null;
    conversationHistory = [];
    try {
      localStorage.removeItem('octavius-conv-id');
    } catch (_err) {
    }
    audioPlayer.stop();
    clearTranscript();
    convTitle.textContent = 'New conversation';
    markActiveHistory();
    closeRail();
    // The finished conversation gets summarised server-side; pick it up shortly.
    scheduleHistoryRefresh(5000);
  });

  // ---------- Boot ----------

  const prefs = loadPrefs();
  if (prefs.voice) currentVoice = prefs.voice;
  if (prefs.sttEnabled !== undefined) sttEnabled = prefs.sttEnabled;
  if (prefs.ttsEnabled !== undefined) ttsEnabled = prefs.ttsEnabled;
  if (prefs.talkMode) {
    talkMode = prefs.talkMode;
  } else if (prefs.toggleToTalk) {
    talkMode = 'toggle';
  }
  if (typeof prefs.proactiveSpeak === 'boolean') {
    proactiveSpeak = prefs.proactiveSpeak;
    proactiveSpeakCb.checked = proactiveSpeak;
  }
  applyVoicePrefs(currentVoice);
  updateInputMode();
  setStatus('Connecting…');

  connect();
  loadHistory();
  updateInboxBadge();
  setInterval(updateInboxBadge, 60000);
})();
