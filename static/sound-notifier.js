(function (global) {
  "use strict";

  const DEFAULTS = {
    sessionStartedAtMs: Date.now(),
    gainBoost: 4.0,
    maxGain: 0.42,
    masterGain: 1.25,
    repeatCount: 3,
    repeatGapMs: 2000,
    dedupMaxKeys: 2000,
    pastEventGraceMs: 1500,
  };

  function createNotifier(options = {}) {
    const config = { ...DEFAULTS, ...options };
    const state = {
      audioContext: null,
      audioBus: null,
      audioUnlocked: false,
      timers: [],
      dedupSet: new Set(),
      dedupQueue: [],
      unlockHandler: null,
    };

    function ensureAudioContext() {
      if (state.audioContext) return state.audioContext;
      const AudioCtx = global.AudioContext || global.webkitAudioContext;
      if (!AudioCtx) return null;
      state.audioContext = new AudioCtx();
      return state.audioContext;
    }

    function ensureAudioBus() {
      if (state.audioBus) return state.audioBus;
      const ctx = ensureAudioContext();
      if (!ctx) return null;

      const compressor = ctx.createDynamicsCompressor();
      compressor.threshold.setValueAtTime(-18, ctx.currentTime);
      compressor.knee.setValueAtTime(14, ctx.currentTime);
      compressor.ratio.setValueAtTime(8, ctx.currentTime);
      compressor.attack.setValueAtTime(0.003, ctx.currentTime);
      compressor.release.setValueAtTime(0.25, ctx.currentTime);

      const master = ctx.createGain();
      master.gain.setValueAtTime(config.masterGain, ctx.currentTime);

      compressor.connect(master);
      master.connect(ctx.destination);
      state.audioBus = { input: compressor };
      return state.audioBus;
    }

    async function unlockAudio() {
      const ctx = ensureAudioContext();
      if (!ctx) return;
      try {
        await ctx.resume();
        state.audioUnlocked = ctx.state === "running";
      } catch (_error) {
        // Browser blocked audio without explicit user gesture.
      }
    }

    function setupAudioUnlock() {
      if (state.unlockHandler) return;
      const unlock = () => {
        unlockAudio().catch(() => {});
        global.removeEventListener("pointerdown", unlock, true);
        global.removeEventListener("keydown", unlock, true);
        state.unlockHandler = null;
      };
      state.unlockHandler = unlock;
      global.addEventListener("pointerdown", unlock, true);
      global.addEventListener("keydown", unlock, true);
    }

    function clearTimers() {
      for (const timerId of state.timers) {
        clearTimeout(timerId);
      }
      state.timers = [];
    }

    function removeTimer(timerId) {
      state.timers = state.timers.filter((activeId) => activeId !== timerId);
    }

    function playTone(frequencyHz, durationMs, kind = "sine", gain = 0.035, offsetSec = 0) {
      if (!state.audioUnlocked) return;
      const ctx = ensureAudioContext();
      if (!ctx || ctx.state !== "running") return;
      const bus = ensureAudioBus();
      if (!bus) return;

      const normalizedGain = Math.min(
        Math.max(gain * config.gainBoost, 0.0001),
        config.maxGain
      );

      const osc = ctx.createOscillator();
      const amp = ctx.createGain();
      osc.type = kind;
      osc.frequency.setValueAtTime(frequencyHz, ctx.currentTime + offsetSec);
      amp.gain.setValueAtTime(0.0001, ctx.currentTime + offsetSec);
      amp.gain.exponentialRampToValueAtTime(
        normalizedGain,
        ctx.currentTime + offsetSec + 0.01
      );
      amp.gain.exponentialRampToValueAtTime(
        0.0001,
        ctx.currentTime + offsetSec + durationMs / 1000
      );
      osc.connect(amp);
      amp.connect(bus.input);
      osc.start(ctx.currentTime + offsetSec);
      osc.stop(ctx.currentTime + offsetSec + durationMs / 1000 + 0.02);
    }

    function playNotificationPattern(eventType) {
      if (eventType !== "task.completed" && eventType !== "task.failed") return;
      if (eventType === "task.completed") {
        playTone(880, 90, "sine", 0.03, 0);
        playTone(1175, 120, "sine", 0.03, 0.11);
        return;
      }
      playTone(520, 120, "sawtooth", 0.06, 0);
      playTone(390, 160, "sawtooth", 0.06, 0.13);
      playTone(260, 220, "triangle", 0.065, 0.3);
      playTone(520, 90, "sawtooth", 0.055, 0.62);
      playTone(340, 120, "triangle", 0.055, 0.72);
    }

    function playNotificationSequence(eventType) {
      clearTimers();
      for (let index = 0; index < config.repeatCount; index += 1) {
        const timerId = setTimeout(() => {
          playNotificationPattern(eventType);
          removeTimer(timerId);
        }, index * config.repeatGapMs);
        state.timers.push(timerId);
      }
    }

    function notificationEventKey(eventPayload, lastEventId = "") {
      const idCandidate = String(lastEventId || eventPayload?.event_id || "").trim();
      if (idCandidate) return `id:${idCandidate}`;

      const eventType = String(eventPayload?.type || "").trim();
      const runId = String(eventPayload?.run_id ?? "").trim();
      const timestamp = String(eventPayload?.ts || "").trim();
      if (!eventType && !timestamp) return "";
      return `fallback:${eventType}|${runId}|${timestamp}`;
    }

    function rememberDedupKey(key) {
      if (!key || state.dedupSet.has(key)) return;
      state.dedupSet.add(key);
      state.dedupQueue.push(key);
      if (state.dedupQueue.length > config.dedupMaxKeys) {
        const stale = state.dedupQueue.shift();
        if (stale) state.dedupSet.delete(stale);
      }
    }

    function handleEvent(eventPayload, lastEventId = "") {
      const eventType = String(eventPayload?.type || "");
      if (eventType !== "task.completed" && eventType !== "task.failed") return;

      const eventTsMs = Date.parse(String(eventPayload?.ts || ""));
      if (!Number.isNaN(eventTsMs) && eventTsMs < config.sessionStartedAtMs - config.pastEventGraceMs) {
        return;
      }

      const dedupKey = notificationEventKey(eventPayload, lastEventId);
      if (dedupKey && state.dedupSet.has(dedupKey)) return;
      rememberDedupKey(dedupKey);

      playNotificationSequence(eventType);
    }

    function teardown() {
      clearTimers();
      if (state.unlockHandler) {
        global.removeEventListener("pointerdown", state.unlockHandler, true);
        global.removeEventListener("keydown", state.unlockHandler, true);
        state.unlockHandler = null;
      }
    }

    setupAudioUnlock();

    return {
      handleEvent,
      teardown,
    };
  }

  global.ManzaraSound = {
    createNotifier,
  };
})(window);
