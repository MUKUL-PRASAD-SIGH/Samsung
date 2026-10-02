"""Event schemas for the interruptible real-time agent system.

Covers:
- Text chunks from user
- Audio chunks
- Video frames
- Explicit interrupt signals
- Tool execution results
"""

from __future__ import annotations

import time
from agent import clock
import uuid
from enum import Enum
from typing import Any, Dict, Optional
from pydantic import BaseModel, Field


class EventType(str, Enum):
    USER_TEXT = "user_text"
    AUDIO_CHUNK = "audio_chunk"
    VIDEO_FRAME = "video_frame"
    INTERRUPT_SIGNAL = "interrupt_signal"
    TOOL_RESULT = "tool_result"


class BaseEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str
    event_type: EventType
    timestamp: float = Field(default_factory=clock.now)
    payload: Dict[str, Any] = Field(default_factory=dict)


class UserTextEvent(BaseEvent):
    event_type: EventType = EventType.USER_TEXT
    text: str
    is_partial: bool = False  # e.g., interim streaming ASR chunk
    # True when a voice barge-in already interrupted in-flight work for this utterance (from a partial
    # transcript), so the coordinator must not bump the epoch a second time for the final text.
    barge_in_handled: bool = False
    # A complete utterance (voice endpoint) needs no burst-coalescing window: planning starts immediately.
    immediate: bool = False


class AudioChunkEvent(BaseEvent):
    event_type: EventType = EventType.AUDIO_CHUNK
    audio_bytes: Optional[bytes] = None
    format: str = "pcm_16khz"  # "pcm_16khz", "wav", "webm"
    duration_ms: float = 0.0
    sample_rate: int = 16000
    is_final: bool = False
    # Continuous voice streaming: raw 16 kHz mono PCM16 frames, with "start"/"stop" control events.
    streaming: bool = False
    stream_control: Optional[str] = None  # "start" | "stop"


class VideoFrameEvent(BaseEvent):
    event_type: EventType = EventType.VIDEO_FRAME
    frame_data: Optional[bytes] = None
    width: int = 0
    height: int = 0
    frame_id: Optional[str] = None
    mime: str = "image/jpeg"
    source: str = "camera"  # "camera" | "screen" | "harness"


class InterruptSignalEvent(BaseEvent):
    event_type: EventType = EventType.INTERRUPT_SIGNAL
    reason: str = "user_barge_in"
    confidence: float = 1.0


class ToolResultEvent(BaseEvent):
    event_type: EventType = EventType.TOOL_RESULT
    call_id: str
    tool_name: str
    epoch: int
    result: Any
    error: Optional[str] = None
