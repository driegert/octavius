(function() {
  function createStreamingAudioPlayer(options) {
    const {
      getPlaybackRate,
      shouldTrimSilence,
      onPlaybackStart,
      onPlaybackIdle,
    } = options || {};

    let audioCtx = null;
    const audioQueue = [];
    let audioDone = false;
    let isPlaying = false;
    let currentAudio = null;

    function getAudioCtx() {
      if (!audioCtx) audioCtx = new AudioContext();
      return audioCtx;
    }

    function trimSilence(audioBuffer) {
      const sampleRate = audioBuffer.sampleRate;
      const numChannels = audioBuffer.numberOfChannels;
      const data = audioBuffer.getChannelData(0);
      const len = data.length;

      const silenceThreshold = 0.008;
      const windowSize = Math.floor(sampleRate * 0.025);
      const minSilenceDuration = Math.floor(sampleRate * 0.4);
      const keepSilence = Math.floor(sampleRate * 0.25);

      function rms(start, end) {
        let sum = 0;
        const n = Math.min(end, len) - start;
        if (n <= 0) return 0;
        for (let i = start; i < start + n; i++) {
          sum += data[i] * data[i];
        }
        return Math.sqrt(sum / n);
      }

      const silentRegions = [];
      let inSilence = false;
      let silenceStart = 0;

      for (let i = 0; i < len; i += windowSize) {
        const energy = rms(i, i + windowSize);
        if (energy < silenceThreshold) {
          if (!inSilence) {
            inSilence = true;
            silenceStart = i;
          }
        } else if (inSilence) {
          if (i - silenceStart > minSilenceDuration) {
            silentRegions.push({ start: silenceStart, end: i });
          }
          inSilence = false;
        }
      }

      if (inSilence && (len - silenceStart) > minSilenceDuration) {
        silentRegions.push({ start: silenceStart, end: len });
      }

      if (silentRegions.length === 0) return audioBuffer;

      const segments = [];
      let pos = 0;
      for (const region of silentRegions) {
        if (region.start > pos) segments.push({ start: pos, end: region.start });
        segments.push({ start: region.start, end: Math.min(region.start + keepSilence, region.end) });
        pos = region.end;
      }
      if (pos < len) segments.push({ start: pos, end: len });

      let newLen = 0;
      for (const seg of segments) newLen += (seg.end - seg.start);
      if (newLen >= len * 0.95) return audioBuffer;

      const ctx = getAudioCtx();
      const newBuffer = ctx.createBuffer(numChannels, newLen, sampleRate);

      for (let ch = 0; ch < numChannels; ch++) {
        const src = audioBuffer.getChannelData(ch);
        const dst = newBuffer.getChannelData(ch);
        let writePos = 0;
        for (const seg of segments) {
          dst.set(src.subarray(seg.start, seg.end), writePos);
          writePos += (seg.end - seg.start);
        }
      }

      return newBuffer;
    }

    function audioBufferToWav(buffer) {
      const numChannels = buffer.numberOfChannels;
      const sampleRate = buffer.sampleRate;
      const length = buffer.length;
      const bytesPerSample = 2;
      const dataSize = length * numChannels * bytesPerSample;
      const headerSize = 44;
      const out = new ArrayBuffer(headerSize + dataSize);
      const view = new DataView(out);

      function writeStr(offset, str) {
        for (let i = 0; i < str.length; i++) view.setUint8(offset + i, str.charCodeAt(i));
      }

      writeStr(0, 'RIFF');
      view.setUint32(4, 36 + dataSize, true);
      writeStr(8, 'WAVE');
      writeStr(12, 'fmt ');
      view.setUint32(16, 16, true);
      view.setUint16(20, 1, true);
      view.setUint16(22, numChannels, true);
      view.setUint32(24, sampleRate, true);
      view.setUint32(28, sampleRate * numChannels * bytesPerSample, true);
      view.setUint16(32, numChannels * bytesPerSample, true);
      view.setUint16(34, 16, true);
      writeStr(36, 'data');
      view.setUint32(40, dataSize, true);

      let offset = 44;
      const channels = [];
      for (let ch = 0; ch < numChannels; ch++) channels.push(buffer.getChannelData(ch));

      for (let i = 0; i < length; i++) {
        for (let ch = 0; ch < numChannels; ch++) {
          const sample = Math.max(-1, Math.min(1, channels[ch][i]));
          view.setInt16(offset, sample * 0x7FFF, true);
          offset += 2;
        }
      }

      return out;
    }

    function setIdle() {
      isPlaying = false;
      currentAudio = null;
      if (onPlaybackIdle) onPlaybackIdle();
    }

    async function playNext() {
      if (audioQueue.length === 0) {
        if (audioDone) {
          audioDone = false;
          setIdle();
        } else {
          isPlaying = false;
        }
        return;
      }

      isPlaying = true;
      let arrayBuffer = audioQueue.shift();

      if (shouldTrimSilence && shouldTrimSilence()) {
        try {
          const ctx = getAudioCtx();
          if (ctx.state === 'suspended') await ctx.resume();
          const buffer = trimSilence(await ctx.decodeAudioData(arrayBuffer));
          arrayBuffer = audioBufferToWav(buffer);
        } catch (_err) {
        }
      }

      const blob = new Blob([arrayBuffer], { type: 'audio/wav' });
      const url = URL.createObjectURL(blob);
      const audio = new Audio(url);
      audio.preservesPitch = true;
      audio.playbackRate = getPlaybackRate ? getPlaybackRate() : 1.0;
      currentAudio = audio;

      audio.onended = () => {
        URL.revokeObjectURL(url);
        if (currentAudio === audio) currentAudio = null;
        playNext();
      };

      audio.onerror = () => {
        URL.revokeObjectURL(url);
        if (currentAudio === audio) currentAudio = null;
        playNext();
      };

      audio.play().catch(() => playNext());
    }

    return {
      enqueueAudio(arrayBuffer) {
        audioQueue.push(arrayBuffer);
        if (onPlaybackStart) onPlaybackStart();
        if (!isPlaying) playNext();
      },
      signalDone() {
        audioDone = true;
        if (!isPlaying && audioQueue.length === 0) {
          audioDone = false;
          setIdle();
        }
      },
      stop() {
        audioQueue.length = 0;
        audioDone = false;
        if (currentAudio) {
          currentAudio.pause();
          currentAudio = null;
        }
        setIdle();
      },
      updatePlaybackRate() {
        if (currentAudio) {
          currentAudio.playbackRate = getPlaybackRate ? getPlaybackRate() : 1.0;
        }
      },
    };
  }

  window.OctaviusVoiceAudio = {
    createStreamingAudioPlayer,
  };
})();
