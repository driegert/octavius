// Shared microphone capture for the streaming STT protocol: stt_start, Float32 LE PCM
// (16 kHz mono) as binary frames every 250 ms, then stt_stop. Shaped after
// startRecording/stopRecording/cancelRecording in index-app.js so that page can adopt it later.
(function() {
  // options: send(objOrBytes) -> bool (JSON-serialisable objects are the control frames,
  // Uint8Array is PCM), onLevel(0..1), onError(err).
  function createCapture(options) {
    const { send, onLevel, onError } = options || {};

    let stream = null;
    let audioCtx = null;
    let processor = null;
    let sendTimer = null;
    let chunks = [];
    let streaming = false;
    let starting = false;
    let stopWhileStarting = false;
    let cancelWhileStarting = false;

    function flush() {
      if (!chunks.length) return;
      let total = 0;
      for (const c of chunks) total += c.length;
      const combined = new Float32Array(total);
      let off = 0;
      for (const c of chunks) { combined.set(c, off); off += c.length; }
      chunks = [];
      send(new Uint8Array(combined.buffer));
    }

    function teardown() {
      streaming = false;
      if (sendTimer) { clearInterval(sendTimer); sendTimer = null; }
      if (stream) { stream.getTracks().forEach((t) => t.stop()); stream = null; }
      if (audioCtx) { audioCtx.close(); audioCtx = null; }
      processor = null;
      chunks = [];
      if (onLevel) onLevel(0);
    }

    // Resolves true once capture is running (stt_start sent), false if it could not start.
    async function start() {
      if (streaming || starting) return false;
      starting = true;
      stopWhileStarting = false;
      cancelWhileStarting = false;
      try {
        stream = await navigator.mediaDevices.getUserMedia({
          audio: { channelCount: 1, sampleRate: 16000, echoCancellation: true, noiseSuppression: true },
        });
        audioCtx = new AudioContext({ sampleRate: 16000 });
        const src = audioCtx.createMediaStreamSource(stream);
        processor = audioCtx.createScriptProcessor(4096, 1, 1);
        processor.onaudioprocess = (e) => {
          if (!streaming) return;
          const data = e.inputBuffer.getChannelData(0);
          chunks.push(new Float32Array(data));
          let sum = 0;
          for (let i = 0; i < data.length; i++) sum += data[i] * data[i];
          if (onLevel) onLevel(Math.min(1, Math.sqrt(sum / data.length) * 9));
        };
        src.connect(processor);
        processor.connect(audioCtx.destination);

        if (cancelWhileStarting) {
          // Cancelled while the browser asked for the mic: nothing was said, send nothing.
          starting = false;
          teardown();
          return false;
        }
        streaming = true;
        chunks = [];
        send({ type: 'stt_start' });
        sendTimer = setInterval(flush, 250);
        starting = false;
        if (stopWhileStarting) stop();
        return true;
      } catch (err) {
        starting = false;
        teardown();
        if (onError) onError(err);
        return false;
      }
    }

    // Sends the remaining audio and stt_stop. Returns whether a capture was ended.
    function stop() {
      if (starting) { stopWhileStarting = true; return false; }
      if (!streaming) return false;
      streaming = false;
      if (sendTimer) { clearInterval(sendTimer); sendTimer = null; }
      flush();
      send({ type: 'stt_stop' });
      teardown();
      return true;
    }

    // Tears down without stt_stop (no utterance to process), or after the server's stt_auto_stop.
    function cancel() {
      if (starting) cancelWhileStarting = true;
      if (streaming) teardown();
    }

    return { start, stop, cancel, isActive: () => streaming || starting };
  }

  window.OctaviusVoiceCapture = { createCapture };
})();
