"""Entry point of the packaged desktop app (`Kairos.exe`).

It is the `kairos` launcher plus the first-run chores a downloaded app must do for itself, because nobody has run `pip`, edited
`.env` or fetched models:

  * the spoken-reply voice (Piper, ~60 MB) is downloaded once into the per-user data folder; if the download fails Kairos
    simply runs without spoken replies;
  * API keys are not read from `.env`: the web UI asks for them on first run (see `agent/keystore.py`);
  * nothing is written next to the program, so it can live in `C:\\Program Files`.

Run from source with `python -m agent.desktop` to try the same flow.
"""

from __future__ import annotations

import multiprocessing
import os
import sys
import urllib.request
from pathlib import Path
from typing import List, Optional

from agent import keystore

VOICE_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/"
VOICE_FILES = ("en_US-lessac-medium.onnx", "en_US-lessac-medium.onnx.json")
MIN_VOICE_BYTES = 1000


def data_dir() -> Path:
    d = keystore.config_dir() / "models" / "piper"
    d.mkdir(parents=True, exist_ok=True)
    return d


def ensure_voice(directory: Optional[Path] = None, opener=urllib.request.urlopen, say=print) -> Optional[Path]:
    """Make sure the Piper voice exists, downloading it on first run. Returns the model path, or None if it is unavailable
    (offline): the caller continues without spoken replies. Files are written to a temp name and renamed, so an interrupted
    download is never mistaken for a complete one."""
    directory = directory or data_dir()
    model = directory / VOICE_FILES[0]
    for name in VOICE_FILES:
        target = directory / name
        if target.exists() and target.stat().st_size > MIN_VOICE_BYTES:
            continue
        say(f"  First run: downloading the speaking voice ({name}) ...")
        tmp = target.with_suffix(target.suffix + ".part")
        try:
            with opener(VOICE_BASE + name, timeout=60) as r, open(tmp, "wb") as f:
                while chunk := r.read(1 << 20):
                    f.write(chunk)
            if tmp.stat().st_size <= MIN_VOICE_BYTES:
                raise OSError("download too small")
            os.replace(tmp, target)
        except (OSError, ValueError) as e:
            say(f"  Could not download the voice ({e}). Kairos will run without spoken replies; it retries next start.")
            tmp.unlink(missing_ok=True)
            return None
    return model


def prepare_environment() -> None:
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    os.environ.setdefault("HF_HUB_VERBOSITY", "error")
    if "TTS_VOICE_PATH" not in os.environ and os.getenv("TTS_BACKEND", "auto") != "off":
        voice = ensure_voice()
        if voice:
            os.environ["TTS_VOICE_PATH"] = str(voice)


def main(argv: Optional[List[str]] = None) -> int:
    multiprocessing.freeze_support()         # PyInstaller on Windows re-executes the exe for child processes
    from agent import launcher

    launcher._utf8_console()
    prepare_environment()
    print("  Close this window (or press Ctrl+C) to quit Kairos.\n")
    return launcher.main(argv)


if __name__ == "__main__":
    sys.exit(main())
