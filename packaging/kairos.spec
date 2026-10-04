# PyInstaller spec for the Kairos desktop app (one folder: Kairos.exe + its libraries).
#
#   cd frontend && npm ci && npm run build && cd ..
#   pip install -e ".[tts]" pyinstaller
#   pyinstaller packaging/kairos.spec --noconfirm        ->  dist/Kairos/Kairos.exe
#
# Deliberately NOT bundled: PyTorch / sentence-transformers (the ~90 MB MiniLM interrupt classifier would add ~1 GB). Without
# them Tier 1 falls back to its keyword + lexical classifier (see agent/fast_path/intent_classifier.py). Models that are
# large or per-user (Whisper, the Piper voice) are downloaded on first use instead of shipped.
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).parent

datas = [
    (str(ROOT / "frontend" / "dist"), "frontend/dist"),
    (str(ROOT / "agent" / "fast_path" / "intent_weights.json"), "agent/fast_path"),
]
for pkg in ("piper", "faster_whisper", "jsonschema_specifications", "jsonschema", "certifi"):
    try:
        datas += collect_data_files(pkg)
    except Exception:  # an optional package that is not installed
        pass

hiddenimports = (
    collect_submodules("agent")
    + collect_submodules("uvicorn")
    + collect_submodules("websockets")
    + ["multipart", "av", "onnxruntime", "ctranslate2", "numpy"]
)

excludes = [
    "torch", "torchvision", "torchaudio", "sentence_transformers", "transformers", "tensorflow", "scipy.tests",
    "matplotlib", "tkinter", "IPython", "pytest", "ruff", "PIL.ImageTk",
]

a = Analysis(
    [str(ROOT / "packaging" / "kairos_app.py")],
    pathex=[str(ROOT)],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="Kairos",
    console=True,                      # a small console window: closing it quits Kairos (the UI opens in its own app window)
    icon=str(ROOT / "packaging" / "kairos.ico"),
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="Kairos", upx=False)
