import { useCallback, useEffect, useRef, useState } from 'react';

const DUCK_GAIN = 0.15;

/**
 * Plays the agent's spoken replies and keeps them interruptible (full duplex).
 *
 *  - `audio_out` actions carry one sentence of PCM16 each; they are scheduled back to back on a WebAudio clock.
 *  - `speech_state: ducked`   -> the user started talking: drop the volume at once (the server then decides whether
 *                                it was a real barge-in or noise).
 *  - `speech_state: resumed`  -> false alarm: full volume again.
 *  - `speech_state: stopped`  -> the reply was cut off: stop every scheduled source immediately and forget the queue.
 *  Output goes through this page's own WebAudio graph, which is what lets the browser's echo canceller remove the
 *  agent's voice from the microphone signal.
 */
export default function useSpeechPlayer(wsRef, connected) {
  const [enabled, setEnabled] = useState(() => localStorage.getItem('speakReplies') === '1');
  const [speaking, setSpeaking] = useState(false);
  // The WebSocket handler that calls handleAction is created once, so it must not close over `enabled` state.
  const enabledRef = useRef(enabled);
  enabledRef.current = enabled;
  const ctxRef = useRef(null);
  const gainRef = useRef(null);
  const nextTimeRef = useRef(0);
  const sourcesRef = useRef(new Set());

  const ensureContext = useCallback(() => {
    if (!ctxRef.current) {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      const ctx = new Ctx();
      const gain = ctx.createGain();
      gain.connect(ctx.destination);
      ctxRef.current = ctx;
      gainRef.current = gain;
    }
    if (ctxRef.current.state === 'suspended') ctxRef.current.resume();
    return ctxRef.current;
  }, []);

  const flush = useCallback(() => {
    sourcesRef.current.forEach((src) => {
      src.onended = null;
      try { src.stop(); } catch (e) { /* already stopped */ }
    });
    sourcesRef.current.clear();
    nextTimeRef.current = 0;
    if (gainRef.current && ctxRef.current) gainRef.current.gain.setTargetAtTime(1, ctxRef.current.currentTime, 0.01);
  }, []);

  const send = useCallback((on) => {
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'tts', enabled: on }));
  }, [wsRef]);

  // (Re)announce the preference whenever the socket (re)connects: the server keeps no preference across sessions.
  useEffect(() => {
    if (connected && enabled) {
      ensureContext();
      send(true);
    }
  }, [connected, enabled, ensureContext, send]);

  const toggle = useCallback(() => {
    setEnabled((was) => {
      const now = !was;
      localStorage.setItem('speakReplies', now ? '1' : '0');
      if (now) ensureContext();   // must happen inside the click: browsers block audio until a user gesture
      else flush();
      send(now);
      return now;
    });
  }, [ensureContext, flush, send]);

  const handleAction = useCallback((action) => {
    if (action.action_type === 'audio_out') {
      if (!enabledRef.current) return;
      const ctx = ensureContext();
      const bin = atob(action.audio_b64);
      const n = bin.length >> 1;
      const samples = new Float32Array(n);
      for (let i = 0; i < n; i++) {
        let v = bin.charCodeAt(2 * i) | (bin.charCodeAt(2 * i + 1) << 8);
        if (v & 0x8000) v -= 0x10000;
        samples[i] = v / 32768;
      }
      const buffer = ctx.createBuffer(1, n, action.sample_rate);
      buffer.copyToChannel(samples, 0);
      const src = ctx.createBufferSource();
      src.buffer = buffer;
      src.connect(gainRef.current);
      const startAt = Math.max(ctx.currentTime + 0.02, nextTimeRef.current);
      src.start(startAt);
      nextTimeRef.current = startAt + buffer.duration;
      sourcesRef.current.add(src);
      src.onended = () => sourcesRef.current.delete(src);
      setSpeaking(true);
    } else if (action.action_type === 'speech_state') {
      const ctx = ctxRef.current;
      if (action.state === 'ducked' && gainRef.current) {
        gainRef.current.gain.setTargetAtTime(DUCK_GAIN, ctx.currentTime, 0.01);
      } else if (action.state === 'resumed' && gainRef.current) {
        gainRef.current.gain.setTargetAtTime(1, ctx.currentTime, 0.05);
      } else if (action.state === 'stopped') {
        flush();
        setSpeaking(false);
      } else if (action.state === 'finished') {
        setSpeaking(false);
      }
    }
  }, [ensureContext, flush]);

  useEffect(() => () => { flush(); if (ctxRef.current) ctxRef.current.close(); }, [flush]);

  return { enabled, speaking, toggle, handleAction, flush };
}
