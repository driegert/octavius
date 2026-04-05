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
    } = options || {};

    let socket = null;
    let closedExplicitly = false;

    function connect() {
      socket = new WebSocket(websocketUrl(path));
      if (binaryType) socket.binaryType = binaryType;

      socket.onopen = (event) => {
        if (onOpen) onOpen(event, socket);
      };

      socket.onmessage = (event) => {
        if (onMessage) onMessage(event, socket);
      };

      socket.onclose = (event) => {
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
        if (socket) socket.close();
      },
    };
  }

  async function loadVoices(selectEl, options) {
    const {
      currentVoice,
      fallbackVoice = 'de_male',
      onLoaded,
    } = options || {};

    const response = await fetch('/api/voices');
    const data = await response.json();

    selectEl.innerHTML = '';
    for (const voice of data.voices || []) {
      const opt = document.createElement('option');
      opt.value = voice;
      opt.textContent = voice.replace(/_/g, ' ');
      selectEl.appendChild(opt);
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
