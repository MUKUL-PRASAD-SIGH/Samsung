"""Turns the frames rendered by android DemoFrames (app/build/demo) into the timed plan the recorder plays on the phone mock-up.
   usage: python build_phone_plan.py [t_start=279.4] [t_end=337.8]"""
import json, shutil, sys, os
from pathlib import Path
REC = Path(os.getenv("KAIROS_REC", "/tmp/kairos-rec"))
src = Path(__file__).resolve().parents[2] / "android/app/build/demo"
t0 = float(sys.argv[1]) if len(sys.argv) > 1 else 279.4
t1 = float(sys.argv[2]) if len(sys.argv) > 2 else 337.8
steps = json.loads((src / "plan.json").read_text())
dst = REC / "www" / "phone"; shutil.rmtree(dst, ignore_errors=True); dst.mkdir(parents=True)
for s in steps: shutil.copy(src / s["img"], dst / s["img"])
scale = (t1 - t0) / sum(s["hold"] for s in steps)
t = t0
for s in steps:
    s["t"] = round(t, 2); t += s["hold"] * scale
bullets = ["Sign in with your access key", "Chat: acknowledges instantly, then answers", "Correct it mid-task", "State · Trace · Graph tabs", "Export code (Download / Copy path)", "Hands-free voice with barge-in"]
(REC / "phone_plan.json").write_text(json.dumps({"bullets": bullets, "steps": steps, "end": t1 + 1.2}, indent=1))
print(f"{len(steps)} frames, scale {scale:.2f}, {t0}s..{t1}s")
