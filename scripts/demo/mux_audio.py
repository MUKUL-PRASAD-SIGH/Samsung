"""Rebuilds the soundtrack (the headless browser has no audio device, so nothing was heard while recording) from what the page logged:
  * the agent's voice: every `audio_out` chunk (Piper PCM) placed where the client would have played it: queued back to back, cut off
    on `speech_state: stopped`, attenuated while `ducked`
  * the user's voice: the fake-microphone file, which starts when the page opened the microphone
and muxes it with the video.   usage: python mux_audio.py video.mp4 out.mp4 [duration=360]"""
import base64, json, os, subprocess, sys, wave
import numpy as np
from pathlib import Path
REC = Path(os.getenv("KAIROS_REC", "/tmp/kairos-rec")); RATE = 48000
video, out = sys.argv[1], sys.argv[2]; dur = float(sys.argv[3]) if len(sys.argv) > 3 else 360.0
ts0 = json.load(open(REC / "meta.json"))["ts0"]
d = json.load(open(REC / "audio.json"))
buf = np.zeros(int(dur * RATE) + RATE, dtype=np.float32)
def rel(ms): return ms / 1000 - ts0

def place(x, t, gain=1.0):
    i = int(round(t * RATE))
    if i < 0: x = x[-i:]; i = 0
    n = min(len(x), len(buf) - i)
    if n > 0: buf[i:i + n] += x[:n] * gain

# --- user (fake microphone)
if d.get("micStart"):
    with wave.open(str(REC / "mic.wav")) as w:
        mic = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768 * 0.9
    place(mic, rel(d["micStart"]))

# --- agent (Piper): replay the client's queue
def resample(x, src):
    if src == RATE: return x
    n = int(len(x) * RATE / src); return np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.float32)
stops = sorted(rel(e["t"]) for e in d["ws"] if e["type"] == "speech_state" and e.get("state") == "stopped")
duck = sorted((rel(e["t"]), e.get("state")) for e in d["ws"] if e["type"] == "speech_state" and e.get("state") in ("ducked", "resumed", "started", "stopped", "finished"))
def gain_at(t):
    g = 1.0
    for ts, st in duck:
        if ts > t: break
        g = 0.3 if st == "ducked" else 1.0
    return g
play_end = -1.0; flushed_until = -1.0; placed = 0
for c in sorted(d["audio"], key=lambda c: c["t"]):
    arrive = rel(c["t"])
    pcm = np.frombuffer(base64.b64decode(c["b64"]), dtype=np.int16).astype(np.float32) / 32768
    x = resample(pcm, c["rate"]); length = len(x) / RATE
    start = max(arrive, play_end)
    cut = next((s for s in stops if arrive - 0.05 <= s and s < start + length), None)
    if any(arrive - 0.05 < s <= start and s >= flushed_until for s in stops) and start > arrive:   # queued behind something that got flushed
        play_end = start = arrive
    if cut is not None and cut <= start:     # flushed before this chunk got its turn: dropped
        flushed_until = cut; play_end = min(play_end, cut); continue
    seg = x if cut is None else x[: int(max(0.0, cut - start) * RATE)]
    if cut is not None: seg = seg * np.linspace(1, 0, len(seg)) if len(seg) else seg
    g = np.array([gain_at(start + k / RATE) for k in range(0, len(seg), 480)], dtype=np.float32)
    g = np.repeat(g, 480)[: len(seg)]
    place(seg * g * 0.95, start)
    play_end = start + length if cut is None else cut; placed += 1
    if cut is not None: flushed_until = cut
print(f"placed {placed}/{len(d['audio'])} agent chunks")
peak = float(np.abs(buf).max()); buf = buf / max(1.0, peak / 0.95)
wav = REC / "soundtrack.wav"
with wave.open(str(wav), "wb") as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE); w.writeframes((buf[: int(dur * RATE)] * 32767).astype(np.int16).tobytes())
subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", video, "-i", str(wav), "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-t", f"{dur:.3f}", "-movflags", "+faststart", out], check=True)
print("wrote", out)
