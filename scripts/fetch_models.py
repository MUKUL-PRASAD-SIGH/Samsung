"""Download every model the server uses into the Hugging Face cache / TTS_VOICE_PATH, so the first request is not
the one that pays for it (and so an image can be baked for offline use).

    python scripts/fetch_models.py            # whisper base.en, MiniLM, Piper voice
    WHISPER_MODEL=small.en python scripts/fetch_models.py
    python scripts/fetch_models.py --skip tts

Silero VAD ships inside the faster-whisper wheel and needs no download.
"""

from __future__ import annotations

import argparse
import os
import sys
import urllib.request
from pathlib import Path

PIPER_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/"
PIPER_FILES = ("en_US-lessac-medium.onnx", "en_US-lessac-medium.onnx.json")


def fetch_whisper() -> None:
    from faster_whisper.utils import download_model

    name = os.getenv("WHISPER_MODEL", "base.en")
    path = download_model(name)
    print(f"whisper {name}: {path}")


def fetch_minilm() -> None:
    from sentence_transformers import SentenceTransformer

    SentenceTransformer("all-MiniLM-L6-v2", device="cpu")
    print("MiniLM all-MiniLM-L6-v2: cached")


def fetch_piper() -> None:
    voice = Path(os.getenv("TTS_VOICE_PATH") or Path(__file__).resolve().parents[1] / "models" / "piper" / PIPER_FILES[0])
    voice.parent.mkdir(parents=True, exist_ok=True)
    for name in PIPER_FILES:
        target = voice.parent / name
        if target.exists() and target.stat().st_size > 1000:
            print(f"piper {name}: already present")
            continue
        urllib.request.urlretrieve(PIPER_BASE + name, target)
        print(f"piper {name}: {target.stat().st_size} bytes")


STEPS = {"whisper": fetch_whisper, "minilm": fetch_minilm, "tts": fetch_piper}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--skip", nargs="*", default=[], choices=sorted(STEPS))
    args = ap.parse_args()
    failed = []
    for name, fn in STEPS.items():
        if name in args.skip:
            continue
        try:
            fn()
        except Exception as e:  # noqa: BLE001 - report every failure, then exit non-zero
            failed.append(name)
            print(f"{name}: FAILED ({type(e).__name__}: {e})", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
