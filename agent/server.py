"""FastAPI and WebSocket Server with Live Demo UI (§4, §5).

Provides:
- Real-time full-duplex WebSocket communication (/ws/{session_id})
- Built-in 3-panel Demo UI (Conversation, State Snapshot, Trace Timeline)
- Evaluator harness JSON endpoint
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from typing import Dict, Set
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

import base64
import os
from agent.coordinator import AgentCoordinator
from agent.schemas.events import UserTextEvent, InterruptSignalEvent, AudioChunkEvent, VideoFrameEvent
from agent.schemas.actions import BaseAction
from agent.trace_logger import TraceLogger

logger = logging.getLogger("agent.server")

# Global coordinator instance
_trace_log_path = os.getenv("TRACE_LOG_PATH")
coordinator = AgentCoordinator(
    trace_logger=TraceLogger(log_file=_trace_log_path) if _trace_log_path else None
)


# session_id -> the inbox of every live connection for that session. The coordinator has ONE outbound action
# queue shared by all sessions; a single dispatcher fans each action out to its own session's connections.
# (Previously every connection ran its own reader on the shared queue and DISCARDED actions for other
# sessions, so any second connection -- another tab, a stale socket, dev-mode double connect -- randomly
# stole and dropped actions such as the acknowledgement filler.)
_subscribers: Dict[str, Set[asyncio.Queue]] = {}
INBOX_MAX = 2000


async def _dispatch_actions_loop() -> None:
    while True:
        try:
            action = await coordinator.get_next_action()
        except asyncio.CancelledError:
            break
        for inbox in list(_subscribers.get(action.session_id, ())):
            try:
                inbox.put_nowait(action)
            except asyncio.QueueFull:
                logger.warning("Slow client for session %s: dropping action %s", action.session_id, action.action_type)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _subscribers.clear()
    dispatcher = asyncio.create_task(_dispatch_actions_loop())
    await coordinator.start()
    # Load + warm the Whisper model in the background so the first voice message doesn't
    # pay the cold-start cost, without delaying server startup (/health reports progress).
    asr_warmup_task = asyncio.create_task(asyncio.to_thread(coordinator.asr_processor.warmup))
    yield
    asr_warmup_task.cancel()
    dispatcher.cancel()
    await coordinator.stop()


app = FastAPI(title="Interruptible Real-Time Agent", version="1.0.0", lifespan=lifespan)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "sessions": len(coordinator.sessions),
        "asr": coordinator.asr_processor.info(),
    }


@app.websocket("/ws/{session_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str):
    await websocket.accept()
    session = coordinator.get_or_create_session(session_id)

    inbox: asyncio.Queue = asyncio.Queue(maxsize=INBOX_MAX)
    _subscribers.setdefault(session_id, set()).add(inbox)

    # Task to forward THIS session's actions to the WebSocket
    async def forward_actions():
        while True:
            try:
                action = await inbox.get()
                await websocket.send_text(action.model_dump_json())
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.info("Stopping forwarder for session %s: %s", session_id, e)
                break  # the socket is gone; nothing more can be delivered to it

    forwarder_task = asyncio.create_task(forward_actions())

    # True between {"type": "voice_stream", "action": "start"} and "stop": binary frames are then
    # continuous raw 16 kHz mono PCM16 (hands-free voice), not one complete WebM recording.
    voice_streaming = False

    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break
            if "bytes" in message and message["bytes"]:
                if voice_streaming:
                    await coordinator.post_event(
                        AudioChunkEvent(
                            session_id=session_id,
                            audio_bytes=message["bytes"],
                            format="pcm_16khz",
                            streaming=True,
                        )
                    )
                else:
                    # Push-to-talk: one complete WebM recording
                    await coordinator.post_event(
                        AudioChunkEvent(
                            session_id=session_id,
                            audio_bytes=message["bytes"],
                            format="webm",
                            is_final=True,
                        )
                    )
            elif "text" in message and message["text"]:
                data = json.loads(message["text"])
                event_type = data.get("type", "user_text")

                if event_type == "interrupt":
                    await coordinator.post_event(
                        InterruptSignalEvent(
                            session_id=session_id,
                            reason=data.get("reason", "ui_barge_in"),
                        )
                    )
                elif event_type == "voice_stream":
                    action = data.get("action")
                    if action in ("start", "stop"):
                        voice_streaming = action == "start"
                        await coordinator.post_event(
                            AudioChunkEvent(
                                session_id=session_id,
                                streaming=True,
                                stream_control=action,
                                format="pcm_16khz",
                            )
                        )
                elif event_type == "video_frame":
                    # {"type":"video_frame","mime":"image/jpeg","data":"<base64>","source":"camera|screen"}
                    b64 = data.get("data") or ""
                    if len(b64) <= 3_000_000:  # ~2 MB decoded; the coordinator re-validates size and mime
                        try:
                            frame = base64.b64decode(b64, validate=True)
                        except Exception:
                            frame = b""
                        if frame:
                            await coordinator.post_event(
                                VideoFrameEvent(
                                    session_id=session_id,
                                    frame_data=frame,
                                    mime=data.get("mime", "image/jpeg"),
                                    source=data.get("source", "camera"),
                                    width=int(data.get("width", 0) or 0),
                                    height=int(data.get("height", 0) or 0),
                                )
                            )
                elif event_type == "audio_chunk":
                    b64_audio = data.get("audio_base64", "")
                    raw_audio = base64.b64decode(b64_audio) if b64_audio else None
                    await coordinator.post_event(
                        AudioChunkEvent(
                            session_id=session_id,
                            audio_bytes=raw_audio,
                            format=data.get("format", "webm"),
                            is_final=data.get("is_final", True),
                        )
                    )
                else:
                    await coordinator.post_event(
                        UserTextEvent(
                            session_id=session_id,
                            text=data.get("text", ""),
                        )
                    )
    except WebSocketDisconnect:
        logger.info("WebSocket disconnected for session %s", session_id)
    finally:
        if voice_streaming:
            # Client vanished mid-stream: close out any utterance in progress and free the runtime.
            await coordinator.post_event(
                AudioChunkEvent(session_id=session_id, streaming=True, stream_control="stop", format="pcm_16khz")
            )
        forwarder_task.cancel()
        subscribers = _subscribers.get(session_id)
        if subscribers is not None:
            subscribers.discard(inbox)
            if not subscribers:
                del _subscribers[session_id]


DEMO_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Interruptible Real-Time Agent Demo</title>
    <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-slate-900 text-slate-100 h-screen flex flex-col font-sans">
    <header class="bg-slate-800 border-b border-slate-700 px-6 py-4 flex items-center justify-between">
        <div>
            <h1 class="text-xl font-bold text-sky-400">Interruptible Real-Time Agent</h1>
            <p class="text-xs text-slate-400">Full-Duplex Interruption &amp; Re-Planning Engine · Samsung Hackathon Theme 05</p>
        </div>
        <div class="flex items-center gap-4">
            <span id="epochBadge" class="bg-emerald-600/30 text-emerald-400 border border-emerald-500/40 px-3 py-1 rounded-full text-xs font-mono font-semibold">Epoch: 1</span>
            <span id="connStatus" class="bg-amber-600/30 text-amber-400 border border-amber-500/40 px-3 py-1 rounded-full text-xs font-mono">Connecting...</span>
        </div>
    </header>

    <main class="flex-1 grid grid-cols-12 gap-4 p-4 overflow-hidden">
        <!-- Panel 1: Conversation -->
        <section class="col-span-4 bg-slate-800/80 rounded-xl border border-slate-700 flex flex-col p-4">
            <h2 class="text-sm font-semibold text-slate-300 mb-3 flex items-center gap-2">
                <span class="w-2 h-2 rounded-full bg-sky-400"></span> Conversation View
            </h2>
            <div id="chatLog" class="flex-1 overflow-y-auto space-y-3 pr-2 text-sm"></div>
            <div class="mt-4 flex flex-col gap-2">
                <div class="flex gap-2">
                    <input id="userInput" type="text" placeholder="Speak / type a request..." 
                           class="flex-1 bg-slate-950 border border-slate-700 rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-sky-500">
                    <button id="micBtn" class="bg-indigo-600 hover:bg-indigo-500 px-3 py-2 rounded-lg text-sm font-medium transition flex items-center gap-1.5">
                        <span>🎙️</span> <span id="micLabel">Speak</span>
                    </button>
                    <button id="sendBtn" class="bg-sky-600 hover:bg-sky-500 px-4 py-2 rounded-lg text-sm font-medium transition">Send</button>
                </div>
                <button id="interruptBtn" class="w-full bg-rose-600/20 hover:bg-rose-600/30 border border-rose-500/50 text-rose-300 font-semibold py-2 rounded-lg text-sm transition flex items-center justify-center gap-2">
                    <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10 9v6m4-6v6m7-3a9 9 0 11-18 0 9 9 0 0118 0z"></path></svg>
                    Barge In / Interrupt Now
                </button>
            </div>
        </section>

        <!-- Panel 2: Live State Snapshot -->
        <section class="col-span-4 bg-slate-800/80 rounded-xl border border-slate-700 flex flex-col p-4">
            <h2 class="text-sm font-semibold text-slate-300 mb-3 flex items-center gap-2">
                <span class="w-2 h-2 rounded-full bg-emerald-400"></span> Live State Snapshot (§2.3)
            </h2>
            <pre id="snapshotBox" class="flex-1 bg-slate-950/90 rounded-lg p-3 text-xs font-mono text-emerald-300 overflow-auto border border-slate-800">{ "epoch": 1, "slots": {}, "in_flight_calls": [] }</pre>
        </section>

        <!-- Panel 3: Trace Timeline -->
        <section class="col-span-4 bg-slate-800/80 rounded-xl border border-slate-700 flex flex-col p-4">
            <h2 class="text-sm font-semibold text-slate-300 mb-3 flex items-center gap-2">
                <span class="w-2 h-2 rounded-full bg-amber-400"></span> Trace Timeline &amp; Recovery
            </h2>
            <div id="traceLog" class="flex-1 overflow-y-auto space-y-2 pr-2 text-xs font-mono"></div>
        </section>
    </main>

    <script>
        const sessionId = "demo_" + Math.random().toString(36).substring(7);
        const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
        const wsUrl = `${protocol}//${window.location.host}/ws/${sessionId}`;
        let ws;

        function connect() {
            ws = new WebSocket(wsUrl);
            ws.onopen = () => {
                document.getElementById("connStatus").textContent = "Connected";
                document.getElementById("connStatus").className = "bg-emerald-600/30 text-emerald-400 border border-emerald-500/40 px-3 py-1 rounded-full text-xs font-mono";
            };
            ws.onclose = () => {
                document.getElementById("connStatus").textContent = "Disconnected";
                document.getElementById("connStatus").className = "bg-rose-600/30 text-rose-400 border border-rose-500/40 px-3 py-1 rounded-full text-xs font-mono";
                setTimeout(connect, 2000);
            };
            ws.onmessage = (e) => {
                const action = JSON.parse(e.data);
                handleAction(action);
            };
        }

        function appendChat(role, text, type) {
            const box = document.getElementById("chatLog");
            const div = document.createElement("div");
            div.className = role === "user" ? "text-right" : "text-left";
            const bubble = document.createElement("span");
            bubble.className = role === "user" ? "inline-block bg-sky-600 text-white rounded-lg px-3 py-2 max-w-[85%]" :
                              type === "filler" ? "inline-block bg-slate-700 text-slate-300 italic rounded-lg px-3 py-2 max-w-[85%]" :
                              "inline-block bg-slate-700 text-white rounded-lg px-3 py-2 max-w-[85%]";
            bubble.textContent = text;
            div.appendChild(bubble);
            box.appendChild(div);
            box.scrollTop = box.scrollHeight;
        }

        function appendTrace(type, msg, color) {
            const box = document.getElementById("traceLog");
            const div = document.createElement("div");
            div.className = `p-2 rounded bg-slate-950 border border-slate-800 ${color}`;
            div.innerHTML = `<span class="font-bold">[${type.toUpperCase()}]</span> ${msg}`;
            box.appendChild(div);
            box.scrollTop = box.scrollHeight;
        }

        function handleAction(action) {
            if (action.epoch) {
                document.getElementById("epochBadge").textContent = `Epoch: ${action.epoch}`;
            }

            if (action.action_type === "state_snapshot") {
                document.getElementById("snapshotBox").textContent = JSON.stringify(action, null, 2);
                appendTrace("snapshot", `Updated slots: ${Object.keys(action.slots).length}, in-flight: ${action.in_flight_calls.length}`, "text-emerald-400");
            } else if (action.action_type === "filler") {
                appendChat("agent", action.text, "filler");
                appendTrace("filler", action.text, "text-sky-300");
            } else if (action.action_type === "spoken_response") {
                appendChat("agent", action.text, "response");
                appendTrace("response", action.text, "text-sky-400");
            } else if (action.action_type === "tool_call") {
                appendTrace("tool_call", `${action.tool_name} (call_id=${action.call_id})`, "text-amber-400");
            } else if (action.action_type === "tool_cancel") {
                appendTrace("cancel", `ABORTED call ${action.call_id} (${action.tool_name}) reason=${action.reason}`, "text-rose-400 font-bold");
            } else if (action.action_type === "clarification") {
                appendChat("agent", action.question, "clarification");
                appendTrace("clarify", action.question, "text-yellow-400");
            }
        }

        document.getElementById("sendBtn").onclick = () => {
            const input = document.getElementById("userInput");
            const text = input.value.trim();
            if (!text) return;
            appendChat("user", text);
            ws.send(JSON.stringify({ type: "user_text", text: text }));
            input.value = "";
        };

        document.getElementById("userInput").onkeydown = (e) => {
            if (e.key === "Enter") document.getElementById("sendBtn").click();
        };

        let mediaRecorder;
        let audioChunks = [];
        const micBtn = document.getElementById("micBtn");
        const micLabel = document.getElementById("micLabel");

        micBtn.onclick = async () => {
            if (mediaRecorder && mediaRecorder.state === "recording") {
                mediaRecorder.stop();
                micLabel.textContent = "Speak";
                micBtn.className = "bg-indigo-600 hover:bg-indigo-500 px-3 py-2 rounded-lg text-sm font-medium transition flex items-center gap-1.5";
                return;
            }
            try {
                const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
                mediaRecorder = new MediaRecorder(stream);
                audioChunks = [];
                mediaRecorder.ondataavailable = (e) => {
                    if (e.data.size > 0) audioChunks.push(e.data);
                };
                mediaRecorder.onstop = async () => {
                    appendChat("user", "🎤 [Audio streaming...]");
                    const audioBlob = new Blob(audioChunks, { type: "audio/webm" });
                    const arrayBuffer = await audioBlob.arrayBuffer();
                    ws.send(arrayBuffer);
                    stream.getTracks().forEach(t => t.stop());
                };
                mediaRecorder.start();
                micLabel.textContent = "Stop";
                micBtn.className = "bg-rose-600 hover:bg-rose-500 px-3 py-2 rounded-lg text-sm font-medium transition flex items-center gap-1.5 animate-pulse";
            } catch (err) {
                alert("Microphone error: " + err.message);
            }
        };

        connect();
    </script>
</body>
</html>
"""


import os
from fastapi.staticfiles import StaticFiles

# Serve compiled React frontend if present, otherwise fallback to inline template
frontend_dist = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend", "dist")
if os.path.exists(frontend_dist):
    app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="frontend")
else:
    @app.get("/", response_class=HTMLResponse)
    async def demo_page():
        return HTMLResponse(content=DEMO_HTML)
