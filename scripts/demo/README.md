# Demo video pipeline (6:00, 1920x1080, 60 fps)

What is real and what is a mock-up in the video:

| Part | Real? |
|---|---|
| Everything inside the Kairos window (login, chat, tabs, hands-free voice, spoken replies, camera, settings) | **Real**: the built web app talking to a live server and the live LLM (Groq `openai/gpt-oss-120b`, OpenRouter free vision) |
| Code export | **Real**: `export_artifact` writes the file; the server really invokes `code --reuse-window <path>` (a stand-in `code` script on `PATH` logs the call) |
| Linux desktop, dock, window chrome, VS Code window | **Mock-up** (`desktop.html`). The VS Code window shows the real exported file and the real output of running it |
| Phone edition | Real Compose UI (`android/.../DemoFrames.kt`, Robolectric) driven by the real server frames recorded from the web session and replayed; the network is faked. Not an on-device recording (no emulator/KVM on the recording machine) |
| Soundtrack | Reconstructed (`mux_audio.py`): the logged Piper audio placed where the client would play it, plus the fake-microphone clips |

## Run it

```bash
# 1. server with the demo settings (auth key, export dir, stand-in `code` launcher)
mkdir -p /tmp/kairos-rec/{bin,exports}
printf '#!/bin/sh\necho "$(date +%s.%N) code $*" >> /tmp/kairos-rec/code_opened.log\n' > /tmp/kairos-rec/bin/code && chmod +x /tmp/kairos-rec/bin/code
PATH=/tmp/kairos-rec/bin:$PATH AUTH_TOKEN=athena-kairos-2026 EXPORT_DIR=/tmp/kairos-rec/exports EXPORT_OPEN=1 \
  uvicorn agent.server:app --host 127.0.0.1 --port 8200

# 2. assets (microphone clips + poster for the fake camera) into /tmp/kairos-rec/assets: python make_assets.py
# 3. phone frames: record the web scenes once (python record.py --only flights,correction,code; copy /tmp/kairos-rec/wsraw.json
#    to android/demo-data/wsraw.json, same for `--only voice` -> wsraw_voice.json), then
docker run --rm -v "$PWD/android:/workspace" -v iagent-gradle:/root/.gradle -e DEMO_DATA=/workspace/demo-data iagent-android-build \
  ./gradlew cleanTestDebugUnitTest testDebugUnitTest --tests '*DemoFrames*' --offline --no-build-cache
python scripts/demo/build_phone_plan.py
# 4. the take (needs `pip install playwright numpy`, Chrome; takes 6 minutes, keep the machine idle), then assemble
python scripts/demo/record.py
python scripts/demo/assemble.py /tmp/kairos-rec/video.mp4 360
python scripts/demo/mux_audio.py /tmp/kairos-rec/video.mp4 /tmp/kairos-rec/kairos_demo.mp4 360
```

`record.py --only scene1,scene2` records parts for dry runs. Scenes are pinned to absolute times (see the `@scene(name, start)`
decorators); empty the export folder between takes, because exports never overwrite (`prime-1.py`).
