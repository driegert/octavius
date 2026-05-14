(function() {
  function escapeHtml(value) {
    if (value == null) return '';
    const div = document.createElement('div');
    div.textContent = String(value);
    return div.innerHTML;
  }

  function websocketUrl(path) {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    return `${proto}//${location.host}${path}`;
  }

  function createWebSocket(options) {
    const {
      path = '/ws',
      binaryType,
      onOpen,
      onMessage,
      onClose,
      onError,
      reconnectDelayMs = 2000,
      getReconnectDelayMs,
      reconnect = true,
      heartbeat = false,
      heartbeatIntervalMs = 15000,
      heartbeatTimeoutMs = 10000,
    } = options || {};

    let socket = null;
    let closedExplicitly = false;
    let heartbeatTimer = null;
    let pongDeadline = null;

    function stopHeartbeat() {
      if (heartbeatTimer) {
        clearInterval(heartbeatTimer);
        heartbeatTimer = null;
      }
      pongDeadline = null;
    }

    function startHeartbeat(sock) {
      if (!heartbeat) return;
      stopHeartbeat();
      heartbeatTimer = setInterval(() => {
        // If we previously sent a ping and the deadline passed without a
        // pong, the underlying TCP is likely dead (e.g. mobile network
        // handoff). Force the socket closed so onclose fires and the
        // reconnect path runs.
        if (pongDeadline !== null && Date.now() > pongDeadline) {
          stopHeartbeat();
          try { sock.close(); } catch (_err) { /* ignore */ }
          return;
        }
        if (sock.readyState !== WebSocket.OPEN) {
          stopHeartbeat();
          return;
        }
        try {
          sock.send(JSON.stringify({ type: 'ping' }));
          pongDeadline = Date.now() + heartbeatTimeoutMs;
        } catch (_err) {
          stopHeartbeat();
          try { sock.close(); } catch (_err2) { /* ignore */ }
        }
      }, heartbeatIntervalMs);
    }

    function connect() {
      socket = new WebSocket(websocketUrl(path));
      if (binaryType) socket.binaryType = binaryType;

      socket.onopen = (event) => {
        startHeartbeat(socket);
        if (onOpen) onOpen(event, socket);
      };

      socket.onmessage = (event) => {
        if (heartbeat) {
          // Any inbound message means the link is alive.
          pongDeadline = null;
          if (typeof event.data === 'string') {
            try {
              const parsed = JSON.parse(event.data);
              if (parsed && parsed.type === 'pong') return;
            } catch (_err) { /* fall through to app handler */ }
          }
        }
        if (onMessage) onMessage(event, socket);
      };

      socket.onclose = (event) => {
        stopHeartbeat();
        if (onClose) onClose(event, socket);
        if (!closedExplicitly && reconnect) {
          const delay = getReconnectDelayMs ? getReconnectDelayMs() : reconnectDelayMs;
          setTimeout(connect, delay);
        }
      };

      socket.onerror = (event) => {
        if (onError) onError(event, socket);
        socket.close();
      };

      return socket;
    }

    return {
      connect,
      getSocket() {
        return socket;
      },
      close() {
        closedExplicitly = true;
        stopHeartbeat();
        if (socket) socket.close();
      },
    };
  }

  async function loadVoices(selectEl, options) {
    const {
      currentVoice,
      fallbackVoice = 'bm_lewis',
      onLoaded,
    } = options || {};

    const response = await fetch('/api/voices');
    const data = await response.json();

    function makeOption(voice) {
      const opt = document.createElement('option');
      opt.value = voice;
      opt.textContent = voice.replace(/_/g, ' ');
      return opt;
    }

    selectEl.innerHTML = '';
    const byEngine = data.voices_by_engine || null;
    if (byEngine) {
      const engineLabels = { kokoro: 'Kokoro (fast, reliable)', voxtral: 'Voxtral (expressive)' };
      for (const engine of ['kokoro', 'voxtral']) {
        const voices = byEngine[engine] || [];
        if (!voices.length) continue;
        const group = document.createElement('optgroup');
        group.label = engineLabels[engine] || engine;
        for (const voice of voices) group.appendChild(makeOption(voice));
        selectEl.appendChild(group);
      }
    } else {
      for (const voice of data.voices || []) selectEl.appendChild(makeOption(voice));
    }

    let resolvedVoice = data.default || fallbackVoice;
    if (currentVoice && (data.voices || []).includes(currentVoice)) {
      resolvedVoice = currentVoice;
    }
    selectEl.value = resolvedVoice;

    if (onLoaded) onLoaded(data, resolvedVoice);
    return { data, resolvedVoice };
  }

  window.OctaviusApp = {
    createWebSocket,
    escapeHtml,
    loadVoices,
    websocketUrl,
  };
})();
