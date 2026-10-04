import json, subprocess, sys
from pathlib import Path
import os
REC = Path(os.getenv("KAIROS_REC", "/tmp/kairos-rec")); FR = REC / "frames"
out = sys.argv[1] if len(sys.argv) > 1 else str(REC / "demo.mp4")
dur = float(sys.argv[2]) if len(sys.argv) > 2 else None
stamps = json.load(open(REC / "stamps.json"))
ts0 = stamps[0][0]; total = (stamps[-1][0] - ts0)
dur = dur or total
N = int(round(dur * 60))
lines, j, last_name, run = [], 0, None, 0
def flush():
    global run, last_name
    if last_name is not None and run:
        lines.append(f"file '{FR / last_name}'"); lines.append(f"duration {run / 60:.6f}")
    run = 0
for i in range(N):
    t = ts0 + i / 60 + 1e-6
    while j + 1 < len(stamps) and stamps[j + 1][0] <= t: j += 1
    name = stamps[j][1]
    if name != last_name: flush(); last_name = name
    run += 1
flush()
lines.append(f"file '{FR / last_name}'")           # concat demuxer quirk: repeat the last file so its duration is honoured
(REC / "concat.txt").write_text("\n".join(lines))
uniq = len(set(l for l in lines if l.startswith("file")))
print(f"{N} output frames @60fps from {len(stamps)} captured ({uniq} distinct used)")
subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(REC / "concat.txt"),
                "-vf", "fps=60,scale=1920:1080:flags=lanczos:in_range=full:out_range=tv,format=yuv420p", "-r", "60", "-c:v", "libx264", "-preset", "medium", "-crf", "14", "-color_range", "tv", "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
                "-tune", "animation", "-movflags", "+faststart", "-t", f"{dur:.3f}", out], check=True)
print("wrote", out)
