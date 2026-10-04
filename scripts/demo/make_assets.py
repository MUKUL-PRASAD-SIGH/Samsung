import subprocess, wave, numpy as np, sys
sys.path.insert(0, "/home/varshith/Samsung")
RATE = 48000
def load(path):
    raw = subprocess.run(["ffmpeg","-loglevel","error","-i",path,"-ar",str(RATE),"-ac","1","-f","s16le","-"],capture_output=True,check=True).stdout
    return np.frombuffer(raw, dtype=np.int16)
A = load("tests/fixtures/audio/book_flight.wav"); B = load("tests/fixtures/audio/correction_goa.wav")
# user utterances synthesized with Piper (the same local voice engine the agent uses to speak)
from agent.multimodal.tts import PiperTTSBackend
tts = PiperTTSBackend()
def synth(text):
    a = tts.synthesize(text)
    raw = subprocess.run(["ffmpeg","-loglevel","error","-f","s16le","-ar",str(a.sample_rate),"-ac","1","-i","-","-ar",str(RATE),"-ac","1","-f","s16le","-"],input=a.pcm,capture_output=True,check=True).stdout
    return np.frombuffer(raw, dtype=np.int16)
C = synth("No wait, show me hotels in Pune instead.")
total = 100 * RATE
buf = np.zeros(total, dtype=np.int16)
def put(sig, t): buf[int(t*RATE):int(t*RATE)+len(sig)] = sig
# schedule (seconds after the microphone is switched on)
put(A, 3.0); put(B, 3.0 + len(A)/RATE + 2.4)
import json; json.dump({"A_end": 3.0+len(A)/RATE, "B_start": 3.0+len(A)/RATE+2.4, "B_len": len(B)/RATE, "C_len": len(C)/RATE}, open("/tmp/kairos-rec/assets/mic_meta.json","w"))
np.save("/tmp/kairos-rec/assets/C.npy", C)
def write(path, arr):
    with wave.open(path, "wb") as w: w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE); w.writeframes(arr.tobytes())
write("/tmp/kairos-rec/assets/mic_base.wav", buf)
print("A", len(A)/RATE, "B", len(B)/RATE, "C", len(C)/RATE)
