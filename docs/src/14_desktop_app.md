# Desktop App (Windows .exe)

## 1. Purpose

The desktop app lets someone use Kairos without Python, Node.js, a terminal or a `.env` file: download an installer, run it, and enter their own Groq and/or OpenRouter API key in the app. It is the same server and web UI as everywhere else, packaged with PyInstaller and wrapped in a normal Windows installer.

## 2. What the user sees

1. They run `Kairos-Setup-<version>.exe` (or unzip the portable build) and start Kairos from the Start menu.
2. A small console window opens and the app opens in its own browser-engine window (Edge, which every Windows 10 and 11 machine has, or Chrome if present).
3. On first run the window shows "Connect your AI provider": two fields, one for a Groq key and one for an OpenRouter key. Either is enough; if both are set, Groq is used for replies, and OpenRouter is also used for camera questions.
4. When they press Save, the server checks each key with its provider. A rejected key is refused with a clear message; a provider that cannot be reached does not block saving (the key is saved with a warning). The agent switches to the new key immediately.
5. "Skip for now" continues in the offline mock mode. Keys can be changed or removed later under Settings, API keys.
6. Closing the console window quits Kairos.

## 3. Components

| Piece | File | Role |
|---|---|---|
| Key store | `agent/keystore.py` | Reads and writes `keys.json` in the per-user config folder, applies keys to the environment, masks them for display, validates their format and checks them online |
| Key endpoints | `agent/server.py` | `GET /settings/keys` (status, masked hints only) and `PUT /settings/keys` (validate, save, hot-swap) |
| Hot swap | `AgentCoordinator.reload_llm` in `agent/coordinator.py` | Rebuilds the LLM backend, the planner and the vision backend from the current environment |
| Key screen | `frontend/src/components/ApiKeysScreen.jsx`, wiring in `frontend/src/App.jsx` | Full-screen on first run, a dialog from Settings |
| Entry point | `agent/desktop.py`, `packaging/kairos_app.py` | First-run voice download, then the normal `kairos` launcher |
| Windows support | `agent/launcher.py` | Finds Edge or Chrome in Program Files, per-user config under `%APPDATA%`, UTF-8 console |
| Packaging | `packaging/kairos.spec`, `packaging/installer.iss`, `packaging/kairos.ico` | PyInstaller folder build and the Inno Setup installer |
| Build pipeline | `.github/workflows/build-desktop.yml` | Builds on `windows-latest`, smoke-tests the exe, makes the installer and portable zip, attaches them to a release |

## 4. How API keys are handled

- Stored in `%APPDATA%\Kairos\keys.json` on Windows, `~/Library/Application Support/Kairos` on macOS and `~/.config/kairos` elsewhere. The file is written atomically and made owner-only where the operating system supports it.
- At startup `keystore.load_into_environment()` copies stored keys into `GROQ_API_KEY` and `OPENROUTER_API_KEY`, so every existing module works unchanged. A variable that is already set (shell or `.env`) wins, so developers keep control.
- The server never returns a key: the status endpoint reports only whether a key exists and a masked hint such as `gsk_…cdef`. Keys are not logged.
- Pasted keys are cleaned (whitespace, quotes and a `Bearer ` prefix are removed) and format-checked offline first: Groq keys start with `gsk_` and OpenRouter keys with `sk-or-`.
- `PUT /settings/keys` refuses a browser request whose `Origin` is not the app itself, so a web page the user happens to visit cannot set their keys. When `AUTH_TOKEN` is configured the endpoints also need the bearer token.

## 5. What the package contains and leaves out

| Included | Left out on purpose |
|---|---|
| The server, the web UI build, the Whisper runtime, Piper, the keyword and lexical interrupt classifier | PyTorch and sentence-transformers (about 1 GB), so the MiniLM semantic classifier is not part of the desktop build and Tier 1 uses its keyword fallback |
| An icon and an installer | Large per-user models: the Whisper model downloads on first voice use (about 140 MB), the Piper voice on first start (about 60 MB) |

If the voice download fails (offline), Kairos runs without spoken replies and retries on the next start.

## 6. Building it

`.github/workflows/build-desktop.yml` runs on version tags (`v*`) and from the Actions tab (with an optional release tag to attach the files to). Steps: build the UI, install with `pip install -e ".[tts,build]"`, run PyInstaller, start the packaged `Kairos.exe` and check `/health`, `/settings/keys` and the served UI, compile the Inno Setup installer, zip the portable build, upload both and attach them to the release.

PyInstaller cannot cross-compile, so the Windows executable is only produced in CI. Running `pyinstaller packaging/kairos.spec` on Linux produces a Linux binary from the same spec; that is how the spec was verified during development (the frozen binary served the UI, reported health, validated a key with the real provider, downloaded the voice and completed a chat turn).

## 7. Known limits

- The installer is not code-signed, so Windows SmartScreen shows "unknown publisher" until it is signed or builds reputation.
- The console window stays visible while Kairos runs. There is no tray icon; closing the window quits.
- Interruption detection uses the keyword classifier in this build.
- The Windows-specific paths (Edge lookup, `%APPDATA%`, installer) are covered by unit tests that simulate Windows, and by the CI smoke test, but were not run on a real Windows machine during development.

## 8. Tests

`tests/test_keystore.py` (storage, masking, validation, endpoints, origin and token protection, live backend swap), `tests/test_desktop.py` (voice download, offline degradation, Windows browser and config paths) and `tests/test_deploy_files.py` (the workflow, spec and installer script stay valid).
