// The voice indicator: an eight-lobed form drawn as four stacked outlines
// ("tapers"), each slightly out of phase with the others. It breathes when
// idle, follows the mic level when listening (tide), turns when thinking, and
// follows the playback level when speaking (violet).
(function() {
  const PALETTE = {
    muted: [128, 136, 165],
    violet: [185, 166, 255],
    tide: [130, 224, 207],
  };

  const STATES = {
    offline:      { amp: 0.0,   spin: 0.0,  gain: 0.0, color: 'muted',  alpha: 0.45, pulse: 0 },
    disabled:     { amp: 0.015, spin: 0.03, gain: 0.0, color: 'muted',  alpha: 0.6,  pulse: 0 },
    idle:         { amp: 0.05,  spin: 0.12, gain: 0.0, color: 'violet', alpha: 0.75, pulse: 0 },
    listening:    { amp: 0.04,  spin: 0.22, gain: 0.85, color: 'tide',  alpha: 1.0,  pulse: 0 },
    transcribing: { amp: 0.06,  spin: 0.70, gain: 0.0, color: 'tide',   alpha: 0.9,  pulse: 0.02 },
    thinking:     { amp: 0.075, spin: 1.05, gain: 0.0, color: 'violet', alpha: 0.95, pulse: 0.035 },
    speaking:     { amp: 0.04,  spin: 0.30, gain: 1.1, color: 'violet', alpha: 1.0,  pulse: 0 },
  };

  const LOBES = 8;
  const TAPERS = 4;
  const STEPS = 192;

  function create(canvas, options) {
    const { getLevel, core = true } = options || {};
    const ctx = canvas.getContext('2d');
    const reduceMotion = window.matchMedia
      ? window.matchMedia('(prefers-reduced-motion: reduce)')
      : { matches: false };

    let stateName = 'offline';
    const cur = { amp: 0, spin: 0, gain: 0, alpha: 0.45, pulse: 0, rgb: PALETTE.muted.slice() };
    let level = 0;
    let phase = 0;
    let last = performance.now();
    let width = 0;
    let height = 0;
    let rafId = null;

    function resize() {
      const rect = canvas.getBoundingClientRect();
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      width = rect.width;
      height = rect.height;
      canvas.width = Math.round(width * dpr);
      canvas.height = Math.round(height * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }

    function approach(from, to, k) {
      return from + (to - from) * k;
    }

    function frame(now) {
      rafId = requestAnimationFrame(frame);
      const dt = Math.min(0.05, (now - last) / 1000);
      last = now;
      if (!width) return;

      const target = STATES[stateName] || STATES.idle;
      const k = 1 - Math.pow(0.002, dt); // ~ frame-rate independent easing
      cur.amp = approach(cur.amp, target.amp, k);
      cur.spin = approach(cur.spin, target.spin, k);
      cur.gain = approach(cur.gain, target.gain, k);
      cur.alpha = approach(cur.alpha, target.alpha, k);
      cur.pulse = approach(cur.pulse, target.pulse, k);
      const rgbTarget = PALETTE[target.color];
      for (let i = 0; i < 3; i++) cur.rgb[i] = approach(cur.rgb[i], rgbTarget[i], k);

      const raw = getLevel ? Math.max(0, Math.min(1, getLevel(stateName) || 0)) : 0;
      // Fast attack, slower release, like a VU meter.
      level = raw > level ? approach(level, raw, 0.45) : approach(level, raw, 0.08);

      const still = reduceMotion.matches;
      const t = now / 1000;
      if (!still) phase += cur.spin * dt;
      const breathe = still ? 0 : 0.012 * Math.sin(t * 0.9);
      const pulse = still ? 0 : cur.pulse * Math.sin(t * 3.2);
      const amp = cur.amp + level * cur.gain * 0.32 + pulse;

      draw(amp, breathe, t, still);
    }

    function draw(amp, breathe, t, still) {
      const cx = width / 2;
      const cy = height / 2;
      const base = Math.min(width, height) * 0.27 * (1 + breathe);
      const [r, g, b] = cur.rgb.map((v) => Math.round(v));
      ctx.clearRect(0, 0, width, height);

      // Soft body
      const glow = ctx.createRadialGradient(cx, cy, base * 0.1, cx, cy, base * (1.35 + amp));
      glow.addColorStop(0, `rgba(${r},${g},${b},${0.16 * cur.alpha})`);
      glow.addColorStop(0.7, `rgba(${r},${g},${b},${0.05 * cur.alpha})`);
      glow.addColorStop(1, `rgba(${r},${g},${b},0)`);
      ctx.fillStyle = glow;
      ctx.beginPath();
      ctx.arc(cx, cy, base * (1.4 + amp), 0, Math.PI * 2);
      ctx.fill();

      const drift = still ? 0 : t * 0.35;
      for (let k = 0; k < TAPERS; k++) {
        const R = base * (1 - k * 0.075);
        const p = phase * (1 + k * 0.18) + k * 0.8;
        const a = amp * (1 - k * 0.12);
        ctx.beginPath();
        for (let i = 0; i <= STEPS; i++) {
          const th = (i / STEPS) * Math.PI * 2;
          // Eight arms, each gently modulated so no two are alike.
          const arm = Math.cos(LOBES * th + p);
          const sway = 0.65 + 0.35 * Math.sin(3 * th - drift + k * 1.3);
          const rr = R * (1 + a * arm * sway);
          const x = cx + rr * Math.cos(th);
          const y = cy + rr * Math.sin(th);
          if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
        }
        ctx.closePath();
        ctx.lineWidth = k === 0 ? 1.6 : 1.1;
        ctx.strokeStyle = `rgba(${r},${g},${b},${(0.95 - k * 0.2) * cur.alpha})`;
        ctx.stroke();
      }

      if (!core) return;
      // Core
      ctx.beginPath();
      ctx.arc(cx, cy, base * 0.16 * (1 + amp * 1.5), 0, Math.PI * 2);
      ctx.fillStyle = `rgba(${r},${g},${b},${0.85 * cur.alpha})`;
      ctx.fill();
    }

    resize();
    if (window.ResizeObserver) {
      new ResizeObserver(resize).observe(canvas);
    } else {
      window.addEventListener('resize', resize);
    }
    rafId = requestAnimationFrame(frame);

    return {
      setState(name) {
        if (STATES[name]) stateName = name;
      },
      getState() {
        return stateName;
      },
      destroy() {
        if (rafId) cancelAnimationFrame(rafId);
      },
    };
  }

  window.OctaviusOrb = { create };
})();
