import React, { useState, useEffect, useRef, useCallback } from 'react';
import { 
  Menu, 
  Plus, 
  Search, 
  Image as ImageIcon, 
  Code2, 
  Mic, 
  MicOff, 
  Camera,
  ScreenShare,
  ArrowUp, 
  Bot, 
  Sparkles, 
  Eye, 
  Copy, 
  Check, 
  Zap, 
  Settings,
  PanelRightClose,
  PanelRightOpen,
  PanelLeftClose,
  PanelLeftOpen,
  X,
  Layers,
  Terminal,
  Clock,
  ShieldCheck,
  RefreshCw,
  SlidersHorizontal,
  ChevronRight,
  Volume2,
  VolumeX,
  LogOut,
  FileCode2,
  Square,
  Trash2
} from 'lucide-react';
import MarkdownReply from './components/MarkdownReply';
import TraceTimeline from './components/TraceTimeline';
import useSpeechPlayer from './hooks/useSpeechPlayer';
import { Logo, Wordmark } from './components/Brand';
import LoginScreen from './components/LoginScreen';
import SuggestionChips from './components/SuggestionChips';
import ExportsList from './components/ExportsList';
import { ToastStack, useToasts } from './components/Toasts';
import { loadHistory, newSessionId, remove as removeChat, save as saveHistory, upsert as upsertChat } from './lib/history';

// Cute Animated SVG Robot Character for the Active Swarm
function MiniBotAvatar({ status, isWatching }) {
  let eyeColor = '#38bdf8'; // blue
  let glowColor = 'rgba(56, 189, 248, 0.4)';

  if (isWatching) {
    eyeColor = '#facc15'; // yellow alert
    glowColor = 'rgba(250, 204, 21, 0.5)';
  } else if (status === 'cancelled') {
    eyeColor = '#f43f5e'; // red
    glowColor = 'rgba(244, 63, 94, 0.5)';
  } else if (status === 'completed') {
    eyeColor = '#34d399'; // green
    glowColor = 'rgba(52, 211, 153, 0.5)';
  }

  return (
    <div className="relative w-10 h-10 flex items-center justify-center shrink-0">
      <div 
        className="absolute inset-0 rounded-2xl blur-[6px] transition-all duration-300"
        style={{ backgroundColor: glowColor }}
      />
      <svg viewBox="0 0 48 48" className="w-9 h-9 relative z-10 drop-shadow-md">
        <line x1="24" y1="10" x2="24" y2="4" stroke="#94a3b8" strokeWidth="2.5" strokeLinecap="round" />
        <circle 
          cx="24" 
          cy="3" 
          r="3" 
          fill={eyeColor} 
          className={status === 'working' || isWatching ? 'animate-ping' : ''} 
        />
        <circle cx="24" cy="3" r="2.5" fill={eyeColor} />
        <rect x="8" y="10" width="32" height="26" rx="8" fill="#1e293b" stroke="#334155" strokeWidth="2" />
        <rect x="12" y="15" width="24" height="15" rx="5" fill="#090d16" />

        {status === 'cancelled' ? (
          <g stroke="#f43f5e" strokeWidth="2" strokeLinecap="round">
            <line x1="16" y1="20" x2="20" y2="24" />
            <line x1="20" y1="20" x2="16" y2="24" />
            <line x1="28" y1="20" x2="32" y2="24" />
            <line x1="32" y1="20" x2="28" y2="24" />
          </g>
        ) : isWatching ? (
          <g fill="#facc15">
            <circle cx="18" cy="22" r="3.8" />
            <circle cx="30" cy="22" r="3.8" />
            <circle cx="19" cy="23" r="1.5" fill="#0f172a" />
            <circle cx="31" cy="23" r="1.5" fill="#0f172a" />
          </g>
        ) : status === 'completed' ? (
          <g stroke="#34d399" strokeWidth="2.2" strokeLinecap="round" fill="none">
            <path d="M16 23 Q 18 20 20 23" />
            <path d="M28 23 Q 30 20 32 23" />
          </g>
        ) : (
          <g fill="#38bdf8">
            <rect x="16" y="20" width="5" height="5" rx="2" className="animate-pulse" />
            <rect x="27" y="20" width="5" height="5" rx="2" className="animate-pulse" />
          </g>
        )}
        <rect x="19" y="32" width="10" height="1.5" rx="0.75" fill="#64748b" />
      </svg>
    </div>
  );
}


// Captures mic audio, resamples to 16 kHz mono and posts 100 ms Int16 PCM frames to the main thread.
const PCM_WORKLET_SOURCE = `
class PCMCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / 16000;   // input samples per output sample (1 when the context runs at 16 kHz)
    this.pos = 1;                       // read position within [prev, ...block]
    this.prev = 0;
    this.frame = new Int16Array(1600);
    this.filled = 0;
  }
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch) return true;
    const buf = new Float32Array(ch.length + 1);
    buf[0] = this.prev;
    buf.set(ch, 1);
    let p = this.pos;
    while (p < buf.length - 1) {
      const i = Math.floor(p);
      const f = p - i;
      const v = Math.max(-1, Math.min(1, buf[i] * (1 - f) + buf[i + 1] * f));
      this.frame[this.filled++] = v < 0 ? v * 32768 : v * 32767;
      if (this.filled === this.frame.length) {
        this.port.postMessage(this.frame.buffer, [this.frame.buffer]);
        this.frame = new Int16Array(1600);
        this.filled = 0;
      }
      p += this.ratio;
    }
    this.pos = p - (buf.length - 1);
    this.prev = ch[ch.length - 1];
    return true;
  }
}
registerProcessor('pcm-capture', PCMCapture);
`;

// Access token for deployments that set AUTH_TOKEN: open the UI once as https://host/?token=... and it is remembered.
function getAuthToken() {
  const fromUrl = new URLSearchParams(window.location.search).get('token');
  if (fromUrl) localStorage.setItem('authToken', fromUrl);
  return fromUrl || localStorage.getItem('authToken') || '';
}

export default function App() {
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [rightSidebarOpen, setRightSidebarOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [searchQuery, setSearchQuery] = useState('');
  // A conversation is a server session. The id is remembered so a reload (or reopening the app) continues it.
  const [sessionId, setSessionId] = useState(() => localStorage.getItem('kairos.session') || newSessionId());
  useEffect(() => { localStorage.setItem('kairos.session', sessionId); }, [sessionId]);
  const [history, setHistory] = useState(() => loadHistory());
  const { toasts, push: toast, dismiss: dismissToast } = useToasts();
  const [exportsList, setExportsList] = useState([]);   // files the agent exported (export_artifact)

  // Sign-in: only when the server has an access key configured. 'checking' | 'login' | 'ok'
  const [auth, setAuth] = useState('checking');
  const [authToken, setAuthToken] = useState(() => getAuthToken());
  const [authNotice, setAuthNotice] = useState('');
  useEffect(() => {
    (async () => {
      try {
        const health = await (await fetch('/health')).json();
        if (!health.auth_required) return setAuth('ok');
        if (authToken) {
          const r = await fetch('/auth/check', { headers: { Authorization: `Bearer ${authToken}` } });
          if (r.ok) return setAuth('ok');
          localStorage.removeItem('authToken');
          setAuthToken('');
          setAuthNotice('Your saved key is no longer valid. Please sign in again.');
        }
        setAuth('login');
      } catch {
        setAuth('ok');   // server unreachable: show the app, which has its own reconnect banner
      }
    })();
  }, []);
  const signIn = async (key) => {
    let r;
    try {
      r = await fetch('/auth/check', { headers: { Authorization: `Bearer ${key}` } });
    } catch {
      return 'Cannot reach the server. Is it running?';
    }
    if (!r.ok) return 'That key was not accepted.';
    localStorage.setItem('authToken', key);
    setAuthToken(key);
    setAuthNotice('');
    setAuth('ok');
    return true;
  };
  const signOut = () => {
    localStorage.removeItem('authToken');
    setAuthToken('');
    setAuth('login');
  };
  const [connected, setConnected] = useState(false);
  const [inputText, setInputText] = useState('');
  const [isRecording, setIsRecording] = useState(false); // hands-free voice streaming is on
  const [isSpeaking, setIsSpeaking] = useState(false);     // server VAD currently hears the user
  const [livePartial, setLivePartial] = useState('');      // latest partial transcript of that speech
  const [sharing, setSharing] = useState('off');           // 'off' | 'camera' | 'screen': frames streamed to the agent
  const [lastFrameAt, setLastFrameAt] = useState(0);
  const [activeRightTab, setActiveRightTab] = useState('agents'); // 'agents' | 'snapshot' | 'trace' | 'graph'
  const [copiedCode, setCopiedCode] = useState(false);
  const [epochPulsing, setEpochPulsing] = useState(false);

  // Home screen by default (empty messages)
  const [messages, setMessages] = useState([]);
  const [spawnedBots, setSpawnedBots] = useState([]);
  const [artifact, setArtifact] = useState(null);
  // True while a real spawn_agent worker is running but hasn't produced its
  // artifact yet -- drives a loading state instead of showing stale/unrelated content.
  const [artifactLoading, setArtifactLoading] = useState(false);

  // Cognitive Graph state (turn/entity/artifact nodes + edges streamed via graph_update actions)
  const [graphNodes, setGraphNodes] = useState([]);
  const [graphEdges, setGraphEdges] = useState([]);
  const [selectedGraphNode, setSelectedGraphNode] = useState(null);

  // Settings State
  const [selectedModel, setSelectedModel] = useState('openai/gpt-oss-120b');
  const [asrEngine, setAsrEngine] = useState('faster-whisper');
  useEffect(() => {
    if (auth !== 'ok') return;
    const token = getAuthToken();
    fetch('/health', token ? { headers: { Authorization: `Bearer ${token}` } } : undefined)
      .then((r) => r.json())
      .then((h) => {
        setTtsAvailable(!!(h.tts && h.tts.available));
        if (h.asr) {
          const state = h.asr.load_failed ? 'unavailable' : h.asr.loaded ? 'ready' : 'loading…';
          setAsrEngine(`faster-whisper (${h.asr.model}, ${h.asr.device}/${h.asr.compute_type}) — ${state}`);
        }
      })
      .catch(() => {});
  }, [connected, auth]);

  // Current session snapshot
  const [snapshot, setSnapshot] = useState({
    epoch: 1,
    intent: 'ready',
    slots: {},
    in_flight_calls: [],
    last_updated: new Date().toISOString(),
  });

  const [traceLogs, setTraceLogs] = useState([
    { id: '1', type: 'system', text: 'Connected full-duplex session', time: '12:00:00' },
  ]);

  const wsRef = useRef(null);
  const [ttsAvailable, setTtsAvailable] = useState(false);   // server has a TTS backend (piper-tts + voice file)
  const speech = useSpeechPlayer(wsRef, connected);
  const voiceRef = useRef(null); // { stream, ctx, node, sink } while streaming
  const shareRef = useRef(null); // { stream, video, canvas, kind, timer } while sharing camera/screen
  const previewVideoRef = useRef(null);
  const chatScrollRef = useRef(null);
  // What the user is currently producing but hasn't submitted: typed draft or live speech.
  const draftText = isSpeaking ? livePartial : inputText;
  const isTyping = draftText.trim().length > 0 || isSpeaking;

  // Auto-scroll chat
  useEffect(() => {
    if (chatScrollRef.current) {
      chatScrollRef.current.scrollTop = chatScrollRef.current.scrollHeight;
    }
  }, [messages, spawnedBots]);

  // Connect WebSocket to FastAPI backend (after sign-in), and keep it connected: drops are retried with backoff.
  useEffect(() => {
    if (auth !== 'ok') return undefined;
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const token = getAuthToken();
    const wsUrl = `${protocol}//${window.location.host}/ws/${sessionId}${token ? `?token=${encodeURIComponent(token)}` : ''}`;
    let ws = null;
    let closedByUs = false;
    let attempt = 0;
    let retryTimer = null;
    let everConnected = false;

    const connect = () => {
      ws = new WebSocket(wsUrl);
      wsRef.current = ws;

      ws.onopen = () => {
        attempt = 0;
        everConnected = true;
        setConnected(true);
        addTrace('system', `Connected session ${sessionId}`);
      };

      ws.onclose = () => {
        setConnected(false);
        stopVoiceStream(false); // the server dropped the stream too; just release the mic locally
        stopSharing();
        if (closedByUs) return;
        addTrace('system', 'Disconnected from coordinator stream');
        if (!everConnected && attempt >= 2) {
          // Never got in: most likely a wrong/expired key (the server refuses the handshake). Ask again instead of looping.
          fetch('/auth/check', { headers: token ? { Authorization: `Bearer ${token}` } : {} }).then((r) => {
            if (r.status === 401) { localStorage.removeItem('authToken'); setAuthToken(''); setAuthNotice('The server did not accept your key. Please sign in again.'); setAuth('login'); }
          }).catch(() => {});
        }
        const delay = Math.min(8000, 500 * 2 ** attempt++);
        retryTimer = setTimeout(connect, delay);
      };

      ws.onmessage = (event) => {
        try {
          const action = JSON.parse(event.data);
          if (action.type === 'error') {
            const text = { rate_limit: 'You are sending too fast; a message was dropped.', too_large: 'That message was too large.', bad_json: 'The server could not read a message.' }[action.code] || `Server: ${action.code}`;
            toast(text, 'error');
            return;
          }
          if (action.type === 'tts_status') return;
          handleIncomingAction(action);
        } catch (e) {
          console.error('Error handling WebSocket action:', e);
        }
      };
    };
    connect();

    return () => {
      closedByUs = true;
      clearTimeout(retryTimer);
      stopVoiceStream(false);
      stopSharing();
      if (ws) ws.close();
    };
  }, [sessionId, auth]);

  const addTrace = (type, text, payload = null) => {
    const now = Date.now();
    setTraceLogs((prev) => [
      ...prev,
      {
        id: Math.random().toString(36).substring(2, 9),
        type,
        text,
        payload,
        ts: now,
        time: new Date(now).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }),
      }
    ]);
  };

  const formatArguments = (args) => {
    if (!args || Object.keys(args).length === 0) return 'no arguments';
    return Object.entries(args)
      .map(([key, value]) => `${key.replace(/_/g, ' ')}: ${value}`)
      .join(', ');
  };

  const getBotRoleName = (toolName) => {
    const mapping = {
      search_flights: 'Flight Scout',
      book_flight: 'Flight Booking Agent',
      search_hotels: 'Hotel Concierge',
      book_hotel: 'Hotel Booking Agent',
      check_weather: 'Weather Sentinel',
      cancel_booking: 'Reservation Operator',
      generate_code: 'Code Generator',
      analyze_frame: 'Vision Analyst',
    };
    return mapping[toolName] || toolName.replace('_', ' ').toUpperCase();
  };

  const handleIncomingAction = (action) => {
    if (action.epoch !== undefined && action.epoch > snapshot.epoch) {
      setEpochPulsing(true);
      setTimeout(() => setEpochPulsing(false), 1500);
    }

    if (action.action_type === 'state_snapshot') {
      setSnapshot(action);
      addTrace('snapshot', `Snapshot updated (Epoch ${action.epoch})`, action);

      const activeCallIds = new Set((action.in_flight_calls || []).map((c) => c.call_id));
      setSpawnedBots((prevBots) => 
        prevBots.map((bot) => {
          if (bot.status === 'working' && !activeCallIds.has(bot.call_id)) {
            return { ...bot, status: 'completed' };
          }
          return bot;
        })
      );

    } else if (action.action_type === 'tool_call') {
      const isAgentSpawn = action.tool_name === 'spawn_agent';
      const botName = isAgentSpawn ? (action.arguments?.name || 'bob') : getBotRoleName(action.tool_name);
      const botRole = isAgentSpawn ? (action.arguments?.role || 'Autonomous Worker') : action.tool_name;

      const newBot = {
        call_id: action.call_id,
        name: botName,
        role: botRole,
        tool_name: action.tool_name,
        arguments: action.arguments,
        epoch: action.epoch,
        status: 'working',
        thought: isAgentSpawn
          ? `Initializing agent ${botName} for: ${action.arguments?.goal || 'task'}...`
          : `Processing request (${formatArguments(action.arguments)})`,
        step: 0,
        total_steps: 4,
      };

      setSpawnedBots((prev) => [
        ...prev.filter(b => b.call_id !== action.call_id),
        newBot
      ]);

      addTrace('tool_call', `SPAWNED ${newBot.name} (${newBot.role}) [ID: ${action.call_id}] under Epoch ${action.epoch}`, action.arguments);

      // A real autonomous worker was just spawned -- open the workspace panel in a
      // loading state right away so the user sees progress immediately, instead of
      // waiting for the final artifact (or worse, showing unrelated placeholder code).
      if (isAgentSpawn) {
        setArtifact(null);
        setArtifactLoading(true);
        setRightSidebarOpen(true);
      }

    } else if (action.action_type === 'agent_step') {
      setSpawnedBots((prev) => {
        const existing = prev.find(b => b.call_id === action.call_id);
        const updated = {
          call_id: action.call_id,
          name: action.name || (existing ? existing.name : 'bob'),
          role: action.role || (existing ? existing.role : 'Worker'),
          tool_name: 'spawn_agent',
          arguments: existing?.arguments || {},
          epoch: action.epoch,
          status: action.status || 'working',
          thought: action.thought,
          step: action.step,
          total_steps: action.total_steps,
        };

        if (existing) {
          return prev.map(b => b.call_id === action.call_id ? updated : b);
        } else {
          return [...prev, updated];
        }
      });

      addTrace('agent_step', `[${action.name}] Step ${action.step}/${action.total_steps}: ${action.thought}`);

      if (action.artifact) {
        setArtifact({
          ...action.artifact,
          author: action.name,
          description: `Hi, I am ${action.name}. Here is the ${action.artifact.language || 'code'} for your request.`,
        });
        setArtifactLoading(false);
        setRightSidebarOpen(true);
      }


    } else if (action.action_type === 'file_exported') {
      setExportsList((prev) => [...prev, { ...action, id: action.action_id }]);
      if (action.preview) {
        // Show what was written, as the workspace artifact, even when the model wrote the code inside the export call itself.
        setArtifact({ title: action.filename, language: action.language, content: action.preview, author: 'Kairos',
          description: `Saved to ${action.path}` });
        setArtifactLoading(false);
      }
      addTrace('agent_step', `Exported ${action.filename} (${action.bytes} bytes) to ${action.path}`, action);
      toast(`Saved ${action.filename}${action.opened_with ? ' and opened it in VS Code' : ''}`, 'ok', 5000);
      setRightSidebarOpen(true);
      setActiveRightTab('exports');

    } else if (action.action_type === 'tool_cancel') {
      setSpawnedBots((prev) => 
        prev.map((bot) => {
          if (bot.call_id === action.call_id) {
            return {
              ...bot,
              status: 'cancelled',
              thought: `ABORTED: Cancelled by user input under Epoch ${action.epoch}`,
              cancelReason: action.reason,
            };
          }
          return bot;
        })
      );

      addTrace('tool_cancel', `ABORTED bot for call ${action.call_id} (${action.tool_name}): ${action.reason}`);

      // Don't leave the workspace panel spinning forever if the worker that was
      // going to produce the artifact got cancelled mid-flight (e.g. user interrupt).
      if (!artifact) {
        setArtifactLoading(false);
      }

    } else if (action.action_type === 'transcript') {
      const heard = (action.text || '').trim();
      if (action.utterance_id) {
        // Streaming voice: partials refine one bubble; the final settles it (or drops it if it was noise).
        if (action.is_partial) setLivePartial(heard);
        setMessages((prev) => {
          const idx = prev.findIndex((m) => m.utteranceId === action.utterance_id);
          if (idx === -1) return prev;
          const next = [...prev];
          if (action.is_partial) {
            next[idx] = { ...next[idx], text: `🎤 ${heard}…` };
          } else if (heard) {
            next[idx] = { ...next[idx], pendingVoice: false, text: `🎤 ${heard}` };
          } else {
            next.splice(idx, 1);
          }
          return next;
        });
        if (!action.is_partial) {
          addTrace('audio', heard ? `Whisper (${action.asr_model}, ${action.latency_ms}ms): "${heard}"` : 'Voice: noise, no speech recognized', action);
        }
      } else {
        setMessages((prev) => {
          // Push-to-talk / legacy: resolve the oldest still-pending voice placeholder.
          const idx = prev.findIndex((m) => m.pendingVoice);
          if (idx === -1) return prev;
          const next = [...prev];
          next[idx] = {
            ...next[idx],
            pendingVoice: false,
            text: heard ? `🎤 ${heard}` : '🎤 (no speech detected — try again)',
          };
          return next;
        });
        addTrace('audio', heard ? `Whisper (${action.asr_model}, ${action.latency_ms}ms): "${heard}"` : 'Whisper: no speech detected', action);
      }

    } else if (action.action_type === 'audio_out') {
      speech.handleAction(action);

    } else if (action.action_type === 'speech_state') {
      speech.handleAction(action);
      if (action.state === 'stopped') {
        addTrace('tool_cancel', `Stopped speaking (${action.reason}) after "${(action.spoken_text || '').slice(0, 60)}"`, action);
      }

    } else if (action.action_type === 'voice_activity') {
      if (action.state === 'speech_start') {
        setIsSpeaking(true);
        setLivePartial('');
        setMessages((prev) => [
          ...prev,
          { id: `voice-${action.utterance_id}`, role: 'user', text: '🎤 …', pendingVoice: true, utteranceId: action.utterance_id },
        ]);
      } else if (action.state === 'speech_end') {
        setIsSpeaking(false);
      } else if (action.state === 'barge_in') {
        addTrace('tool_cancel', `Voice barge-in interrupted running work: "${action.detail}"`, action);
      } else {
        addTrace('audio', `Voice stream ${action.state}`);
      }

    } else if (action.action_type === 'graph_update') {
      if (action.op === 'full') {
        setGraphNodes(action.nodes || []);
        setGraphEdges(action.edges || []);
      } else {
        setGraphNodes((prev) => {
          const existingIds = new Set(prev.map((n) => n.id));
          return [...prev, ...(action.nodes || []).filter((n) => !existingIds.has(n.id))];
        });
        setGraphEdges((prev) => [...prev, ...(action.edges || [])]);
      }
      addTrace('graph_update', `Cognitive Graph: +${(action.nodes || []).length} node(s), +${(action.edges || []).length} edge(s)`, action);

    } else if (action.action_type === 'filler') {
      setMessages((prev) => [
        ...prev,
        {
          id: action.action_id || Math.random().toString(),
          role: 'agent',
          type: 'filler',
          text: action.text,
          epoch: action.epoch,
        }
      ]);
      addTrace('filler', `Fast Filler: "${action.text}"`);

    } else if (action.action_type === 'spoken_response') {
      setMessages((prev) => [
        ...prev,
        {
          id: action.action_id || Math.random().toString(),
          role: 'agent',
          type: 'spoken_response',
          text: action.text,
          epoch: action.epoch,
        }
      ]);
      addTrace('response', `Agent Output: ${action.text.slice(0, 80)}...`);
    }
  };

  const handleSendMessage = async (customText = null) => {
    const text = (customText !== null ? customText : inputText).trim();
    if (!text) return;

    // Never show a message as sent when it could not be: say so, and keep what the user typed.
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) {
      toast('Not connected yet — reconnecting. Your message was not sent.', 'error');
      return;
    }

    setMessages((prev) => [...prev, { id: Math.random().toString(), role: 'user', text }]);
    setInputText('');
    // "this" / "on my screen" must mean what is visible NOW, so push a fresh frame ahead of the text.
    if (shareRef.current) await captureAndSendFrame();
    // Whether an agent gets spawned and an artifact gets produced is entirely up to the real backend/LLM response
    // (tool_call / agent_step actions handled in handleIncomingAction) -- not guessed here from keywords in the raw text.
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'user_text', text }));
    }
  };

  // Remember every conversation (it is a server session) so it can be reopened from the sidebar.
  useEffect(() => {
    if (messages.length === 0) return;
    setHistory((h) => { const next = upsertChat(h, sessionId, messages); saveHistory(next); return next; });
  }, [messages, sessionId]);

  const resetWorkspace = () => {
    setSpawnedBots([]);
    setArtifact(null);
    setArtifactLoading(false);
    setGraphNodes([]);
    setGraphEdges([]);
    setExportsList([]);
    setTraceLogs([]);
    setSnapshot({ epoch: 1, intent: 'ready', slots: {}, in_flight_calls: [], last_updated: new Date().toISOString() });
    setRightSidebarOpen(false);
  };

  const handleCreateNewChat = () => {
    setMessages([]);
    resetWorkspace();
    setSessionId(newSessionId());
  };

  const handleDeleteChat = (id) => {
    setHistory((h) => { const next = removeChat(h, id); saveHistory(next); return next; });
    if (id === sessionId) handleCreateNewChat();
  };

  const handleSelectChat = (entry) => {
    if (entry.id === sessionId) return;
    resetWorkspace();
    setMessages(entry.messages || []);
    setSessionId(entry.id);   // reconnects with the same session id: the agent still remembers it while the server is up
  };

  // vscode://file/<path> opens the file in a desktop VS Code on THIS machine. It only works when the server runs on the same
  // computer; otherwise use Download.
  const openInEditor = (item) => {
    if (!item.editor_uri) return;
    window.location.href = item.editor_uri;
    toast('Asking your browser to open VS Code… (works when the server runs on this computer; otherwise use Download)', 'info', 6000);
  };

  const interruptAgent = () => {
    const ws = wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    ws.send(JSON.stringify({ type: 'interrupt', reason: 'ui_barge_in' }));
    speech.flush();
    toast('Interrupted', 'info', 1500);
  };

  // Esc stops whatever Kairos is doing right now (running tools, speech).
  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape') interruptAgent(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  });

  const handleCopyCode = () => {
    if (!artifact?.content) return;
    navigator.clipboard.writeText(artifact.content);
    setCopiedCode(true);
    setTimeout(() => setCopiedCode(false), 2000);
  };

  // ---- camera / screen sharing -------------------------------------------------------------------
  // Frames are only *buffered* server-side (1 fps, downscaled JPEG); the vision model runs solely when the
  // agent decides a question needs the image, so sharing costs nothing until then.
  const captureAndSendFrame = async () => {
    const s = shareRef.current;
    const ws = wsRef.current;
    if (!s || !ws || ws.readyState !== WebSocket.OPEN || ws.bufferedAmount > 1_000_000) return;
    const v = s.video;
    if (!v.videoWidth) return;
    const scale = Math.min(1, 1024 / v.videoWidth);
    const w = Math.round(v.videoWidth * scale);
    const h = Math.round(v.videoHeight * scale);
    s.canvas.width = w;
    s.canvas.height = h;
    s.canvas.getContext('2d').drawImage(v, 0, 0, w, h);
    const blob = await new Promise((resolve) => s.canvas.toBlob(resolve, 'image/jpeg', 0.7));
    if (!blob || !shareRef.current) return;
    const bytes = new Uint8Array(await blob.arrayBuffer());
    let bin = '';
    for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    ws.send(JSON.stringify({ type: 'video_frame', mime: 'image/jpeg', data: btoa(bin), source: s.kind, width: w, height: h }));
    setLastFrameAt(Date.now());
  };

  const stopSharing = () => {
    const s = shareRef.current;
    shareRef.current = null;
    if (s) {
      clearInterval(s.timer);
      s.stream.getTracks().forEach((track) => { track.onended = null; track.stop(); });
      s.video.srcObject = null;
    }
    setSharing('off');
  };

  const startSharing = async (kind) => {
    if (shareRef.current) stopSharing();
    try {
      const stream = kind === 'screen'
        ? await navigator.mediaDevices.getDisplayMedia({ video: true, audio: false })
        : await navigator.mediaDevices.getUserMedia({ video: { width: { ideal: 1280 }, height: { ideal: 720 } }, audio: false });
      const video = document.createElement('video');
      video.srcObject = stream;
      video.muted = true;
      video.playsInline = true;
      await video.play();
      shareRef.current = { stream, video, canvas: document.createElement('canvas'), kind, timer: null };
      // The browser's own "stop sharing" control ends the track: follow it.
      stream.getVideoTracks()[0].onended = () => stopSharing();
      shareRef.current.timer = setInterval(captureAndSendFrame, 1000);
      setSharing(kind);
      addTrace('vision', `Sharing ${kind} (1 frame/s, analyzed only when you ask about it)`);
      await captureAndSendFrame();
    } catch (err) {
      toast(`${kind === 'screen' ? 'Screen sharing' : 'Camera'} unavailable: ${err.name === 'NotAllowedError' ? 'permission was denied. Allow it in your browser\'s site settings.' : err.message}`, 'error', 7000);
    }
  };

  const toggleSharing = (kind) => (sharing === kind ? stopSharing() : startSharing(kind));

  // Attach the live stream to the preview thumbnail whenever that <video> mounts. A callback ref (with a
  // stable identity) is needed because the chip is a NEW element after the home -> chat view switch; an
  // effect keyed on `sharing` never re-ran then, leaving the thumbnail blank.
  const attachPreview = useCallback((el) => {
    previewVideoRef.current = el;
    if (el && shareRef.current) {
      el.srcObject = shareRef.current.stream;
      el.play().catch(() => {});
    }
  }, []);

  const stopVoiceStream = (notifyServer = true) => {
    const v = voiceRef.current;
    voiceRef.current = null;
    if (v) {
      if (notifyServer && wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
        wsRef.current.send(JSON.stringify({ type: 'voice_stream', action: 'stop' }));
      }
      v.node.port.onmessage = null;
      try { v.node.disconnect(); v.sink.disconnect(); } catch (e) { /* already disconnected */ }
      v.stream.getTracks().forEach((track) => track.stop());
      v.ctx.close().catch(() => {});
    }
    setIsRecording(false);
    setIsSpeaking(false);
    setLivePartial('');
  };

  const startVoiceStream = async () => {
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) {
      toast('Not connected yet — try again in a moment.', 'error');
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
      const ctx = new AudioContext({ sampleRate: 16000 }); // if the browser ignores this, the worklet resamples
      const url = URL.createObjectURL(new Blob([PCM_WORKLET_SOURCE], { type: 'application/javascript' }));
      await ctx.audioWorklet.addModule(url);
      URL.revokeObjectURL(url);

      const source = ctx.createMediaStreamSource(stream);
      const node = new AudioWorkletNode(ctx, 'pcm-capture');
      const sink = ctx.createGain(); // muted sink keeps the worklet pulled without playing the mic back
      sink.gain.value = 0;
      source.connect(node);
      node.connect(sink);
      sink.connect(ctx.destination);

      node.port.onmessage = (e) => {
        const ws = wsRef.current;
        // Drop frames rather than queue unboundedly if the socket backs up (stale audio is useless).
        if (ws && ws.readyState === WebSocket.OPEN && ws.bufferedAmount < 1_000_000) ws.send(e.data);
      };

      voiceRef.current = { stream, ctx, node, sink };
      wsRef.current.send(JSON.stringify({ type: 'voice_stream', action: 'start' }));
      setIsRecording(true);
    } catch (err) {
      toast(err.name === 'NotAllowedError' ? 'Microphone permission was denied. Allow it in your browser\'s site settings, then try again.' : `Microphone unavailable: ${err.message}`, 'error', 7000);
    }
  };

  const toggleRecording = () => (isRecording ? stopVoiceStream() : startVoiceStream());

  const speakButton = ttsAvailable ? (
    <button
      onClick={speech.toggle}
      className={`hover:text-white transition ${speech.enabled ? 'text-emerald-400' : ''} ${speech.speaking ? 'animate-pulse' : ''}`}
      title={speech.enabled ? 'Spoken replies on (speak to interrupt)' : 'Speak replies aloud'}
    >
      {speech.enabled ? <Volume2 className="w-4 h-4" /> : <VolumeX className="w-4 h-4" />}
    </button>
  ) : null;

  // Always visible while sharing (both views): the user must never be unsure whether the agent can see them.
  const sharingChip = sharing !== 'off' ? (
    <div className="px-2.5 py-1.5 bg-emerald-500/10 border border-emerald-500/20 rounded-lg flex items-center justify-between gap-3 text-[11px] text-emerald-300 font-mono">
      <span className="flex items-center gap-2 min-w-0">
        <video ref={attachPreview} muted playsInline autoPlay className="w-14 h-8 rounded object-cover bg-black shrink-0" />
        <span className="truncate">Sharing your {sharing} — the agent only looks when you ask about it</span>
      </span>
      <button onClick={stopSharing} className="text-[10px] text-emerald-400/80 hover:text-white transition shrink-0">Stop</button>
    </div>
  ) : null;

  // True when user is on home screen (no messages yet)
  const isHomeScreen = messages.length === 0;
  // Anything running or speaking right now: shows the Stop button.
  const agentBusy = (snapshot.in_flight_calls || []).length > 0 || speech.speaking || spawnedBots.some((b) => b.status === 'working');

  if (auth === 'checking') {
    return <div className="h-screen w-screen flex items-center justify-center bg-[#0b0e14]"><Logo className="w-14 h-14 animate-pulse" /></div>;
  }
  if (auth === 'login') return <LoginScreen onSubmit={signIn} initialError={authNotice} />;

  return (
    <div 
      className="h-screen w-screen flex bg-black text-slate-100 font-sans select-none overflow-hidden relative"
      style={{
        // A calm deep-navy field with a faint gold glow (no photo to download, nothing to attribute).
        backgroundImage: 'radial-gradient(70% 55% at 50% 0%, rgba(212,160,23,0.09), transparent 70%), radial-gradient(50% 45% at 90% 100%, rgba(56,189,248,0.07), transparent 70%), linear-gradient(to bottom, #0b0e14, #080a10)',
      }}
    >
      {/* ─────────────────────────────────────────────────────────────
          1. LEFT SIDEBAR (Collapsible + Settings Tab above Email)
      ───────────────────────────────────────────────────────────── */}
      {sidebarOpen && (
        <aside className="w-64 bg-[#111318]/92 backdrop-blur-md border-r border-white/5 flex flex-col justify-between shrink-0 z-30 transition-all duration-300">
          <div className="p-3.5 space-y-4 flex-1 flex flex-col min-h-0">
            {/* Top row: Hamburger (collapse), Title, Add Button */}
            <div className="flex items-center justify-between px-1">
              <div className="flex items-center gap-2.5">
                <button 
                  onClick={() => setSidebarOpen(false)}
                  className="text-slate-400 hover:text-white transition p-1 rounded-lg hover:bg-white/5"
                  title="Collapse Sidebar"
                >
                  <Menu className="w-4 h-4" />
                </button>
                <span className="flex items-center gap-2">
                  <Logo className="w-5 h-5" />
                  <span className="text-xs font-semibold text-amber-200 tracking-[0.2em]" style={{ fontFamily: '"Cormorant Garamond", Georgia, serif' }}>KAIROS</span>
                </span>
              </div>
              <button 
                onClick={handleCreateNewChat}
                className="w-5 h-5 rounded-full border border-slate-600 hover:border-slate-400 flex items-center justify-center text-slate-300 hover:text-white transition"
                title="New conversation"
              >
                <Plus className="w-3.5 h-3.5" />
              </button>
            </div>

            {/* Search Input */}
            <div className="relative">
              <input
                type="text"
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                placeholder="Search"
                className="w-full bg-[#1e2026] text-slate-200 placeholder:text-slate-500 text-xs rounded-xl px-3 py-2 pr-8 border border-white/5 focus:outline-none focus:border-slate-600 transition"
              />
              <Search className="w-3.5 h-3.5 text-slate-400 absolute right-2.5 top-2.5" />
            </div>

            {/* Chats List */}
            <div className="space-y-1 flex-1 overflow-y-auto">
              <span className="text-[11px] font-medium text-slate-500 px-2 block mb-1.5">Conversations</span>
              {history.length === 0 && <p className="text-[11px] text-slate-600 px-2 leading-relaxed">Your conversations appear here. Start one on the right.</p>}
              <div className="space-y-0.5">
                {history
                  .filter((c) => c.title.toLowerCase().includes(searchQuery.toLowerCase()))
                  .map((chat) => {
                    const isActive = sessionId === chat.id;
                    return (
                      <div key={chat.id} className={`group flex items-center rounded-xl transition ${isActive ? 'bg-[#252830]' : 'hover:bg-white/5'}`}>
                        <button
                          onClick={() => handleSelectChat(chat)}
                          className={`flex-1 text-left px-3 py-2 text-xs truncate ${isActive ? 'text-white font-medium' : 'text-slate-400 group-hover:text-slate-200'}`}
                          title={chat.title}
                        >
                          {chat.title}
                        </button>
                        <button onClick={() => handleDeleteChat(chat.id)} className="opacity-0 group-hover:opacity-100 pr-2 text-slate-500 hover:text-rose-400 transition" title="Delete conversation">
                          <Trash2 className="w-3.5 h-3.5" />
                        </button>
                      </div>
                    );
                  })}
              </div>
            </div>
          </div>

          {/* Bottom Area: Settings Tab Directly Above Email Profile */}
          <div className="p-3 border-t border-white/5 space-y-1 shrink-0 bg-[#0e1014]/60">
            {/* Settings Tab */}
            <button
              onClick={() => setSettingsOpen(true)}
              className="w-full flex items-center gap-2.5 px-2.5 py-2 rounded-xl text-xs text-slate-300 hover:text-white hover:bg-white/5 transition group"
            >
              <Settings className="w-4 h-4 text-slate-400 group-hover:text-amber-400 transition" />
              <span className="font-medium">Settings</span>
            </button>

            {/* Session / sign out */}
            <div className="pt-2 border-t border-white/5 flex items-center gap-2.5 px-1">
              <div className="w-6 h-6 rounded-full bg-gradient-to-tr from-amber-300 to-amber-600 flex items-center justify-center text-[11px] font-bold text-[#0b0e14] shrink-0" style={{ fontFamily: 'Georgia, serif' }}>Κ</div>
              <span className="text-[11px] text-slate-400 truncate font-mono flex-1" title={`Session ${sessionId}`}>{authToken ? 'Signed in' : 'This device'}</span>
              {authToken && (
                <button onClick={signOut} className="text-slate-500 hover:text-white transition" title="Sign out"><LogOut className="w-3.5 h-3.5" /></button>
              )}
            </div>
          </div>
        </aside>
      )}

      <ToastStack toasts={toasts} onDismiss={dismissToast} />
      {!connected && (
        <div className="absolute top-0 left-0 right-0 z-50 flex items-center justify-center gap-2 bg-amber-500/15 border-b border-amber-400/30 text-amber-200 text-[11px] py-1.5 backdrop-blur-md" role="status">
          <span className="w-2 h-2 rounded-full bg-amber-300 animate-pulse" /> Reconnecting to Kairos…
        </div>
      )}

      {/* Floating Reopen Button if Left Sidebar is Collapsed */}
      {!sidebarOpen && (
        <button 
          onClick={() => setSidebarOpen(true)}
          className="absolute top-4 left-4 z-40 p-2 rounded-xl bg-[#14161c]/90 backdrop-blur-md border border-white/10 text-slate-300 hover:text-white shadow-xl hover:scale-105 transition"
          title="Open Sidebar"
        >
          <Menu className="w-4 h-4" />
        </button>
      )}

      {/* ─────────────────────────────────────────────────────────────
          2. MAIN STAGE (Home Screen vs Morphing Conversation)
      ───────────────────────────────────────────────────────────── */}
      <div className="flex-1 flex flex-col h-full min-w-0 overflow-hidden relative">
        
        {/* TOP STATUS BAR (Session, Epoch, WebSocket live indicator, Right Sidebar Toggle) */}
        <header className="h-11 px-6 flex items-center justify-between z-20 shrink-0">
          <div className="flex items-center gap-2">
            {!sidebarOpen && <div className="w-8" />} {/* spacing for floating burger */}
            <span className="text-xs font-semibold text-amber-200/70 tracking-[0.25em]" style={{ fontFamily: '"Cormorant Garamond", Georgia, serif' }}>
              KAIROS · ΚΑΙΡΟΣ
            </span>
          </div>

          <div className="flex items-center gap-3">
            {/* Epoch badge */}
            <div className={`flex items-center gap-1.5 px-2.5 py-0.5 rounded-full border text-[11px] font-mono transition-all duration-300 ${
              epochPulsing 
                ? 'bg-rose-500/20 border-rose-500 text-rose-300 scale-105 shadow-lg shadow-rose-500/40' 
                : 'bg-emerald-500/10 border-emerald-500/30 text-emerald-400'
            }`}>
              <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse" />
              <span>Epoch #{snapshot.epoch}</span>
            </div>

            {agentBusy && (
              <button onClick={interruptAgent} className="flex items-center gap-1.5 px-2.5 py-0.5 rounded-full border border-rose-500/50 bg-rose-500/15 text-rose-300 text-[11px] font-mono hover:bg-rose-500/25 transition" title="Stop everything (Esc)">
                <Square className="w-3 h-3 fill-current" /> Stop <kbd className="text-[9px] opacity-60">Esc</kbd>
              </button>
            )}
            {/* Connection badge */}
            <div className="flex items-center gap-1.5 text-[11px] font-mono text-slate-400">
              <span className={`w-2 h-2 rounded-full ${connected ? 'bg-emerald-400' : 'bg-rose-500'}`} />
              <span className="hidden sm:inline">{connected ? 'Full-Duplex' : 'Disconnected'}</span>
            </div>

            {/* Toggle Right Sidebar button (when in active conversation) */}
            {!isHomeScreen && (
              <button
                onClick={() => setRightSidebarOpen(!rightSidebarOpen)}
                className={`p-1.5 rounded-lg border text-xs font-mono flex items-center gap-1.5 transition ${
                  rightSidebarOpen
                    ? 'bg-sky-500/15 border-sky-500/30 text-sky-300'
                    : 'bg-[#181a22] border-white/10 text-slate-400 hover:text-white'
                }`}
                title={rightSidebarOpen ? 'Collapse right panel' : 'Expand right panel'}
              >
                {rightSidebarOpen ? <PanelRightClose className="w-4 h-4" /> : <PanelRightOpen className="w-4 h-4" />}
                <span className="hidden md:inline">{rightSidebarOpen ? 'Collapse' : 'Artifact & Agents'}</span>
                {(artifact || artifactLoading) && !rightSidebarOpen && (
                  <span className="w-2 h-2 rounded-full bg-amber-400 animate-ping" />
                )}
              </button>
            )}
          </div>
        </header>

        {/* ─── SCENARIO A: HOME SCREEN (Exact match with user screenshot) ─── */}
        {isHomeScreen ? (
          <div className="flex-1 flex flex-col items-center justify-center p-6 relative">
            <div className="mb-10"><Wordmark size="lg" /></div>

            {/* Centered Floating Input Card */}
            <div className="w-full max-w-xl bg-[#181a20]/90 backdrop-blur-md border border-white/10 rounded-2xl p-4 shadow-2xl flex flex-col gap-3 transition focus-within:border-slate-500">
              <input
                type="text"
                value={inputText}
                onChange={(e) => setInputText(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && handleSendMessage()}
                placeholder="What would you like to know?"
                className="w-full bg-transparent text-sm text-slate-200 placeholder:text-slate-500 focus:outline-none px-1 py-1 font-sans"
                autoFocus
              />
              {sharingChip}
              <div className="flex items-center justify-between pt-1 border-t border-white/5">
                <div className="flex items-center gap-3 text-slate-400">
                  <button className="hover:text-white transition" title="Upload Image"><ImageIcon className="w-4 h-4" /></button>
                  <button className="hover:text-white transition" title="Code snippet"><Code2 className="w-4 h-4" /></button>
                  <button
                    onClick={() => toggleSharing('camera')}
                    className={`hover:text-white transition ${sharing === 'camera' ? 'text-emerald-400' : ''}`}
                    title={sharing === 'camera' ? 'Stop sharing camera' : 'Share camera (agent can look when you ask)'}
                  >
                    <Camera className="w-4 h-4" />
                  </button>
                  <button
                    onClick={() => toggleSharing('screen')}
                    className={`hover:text-white transition ${sharing === 'screen' ? 'text-emerald-400' : ''}`}
                    title={sharing === 'screen' ? 'Stop sharing screen' : 'Share screen (agent can look when you ask)'}
                  >
                    <ScreenShare className="w-4 h-4" />
                  </button>
                  {speakButton}
                  <button 
                    onClick={toggleRecording} 
                    className={`hover:text-white transition ${isRecording ? 'text-rose-400 animate-pulse' : ''}`}
                    title={isRecording ? 'Stop listening' : 'Hands-free voice (speak any time to interrupt)'}
                  >
                    <Mic className="w-4 h-4" />
                  </button>
                </div>
                <button 
                  onClick={() => handleSendMessage()}
                  disabled={!inputText.trim()}
                  className="w-8 h-8 rounded-full bg-[#2a2d37] hover:bg-[#383c48] disabled:opacity-40 flex items-center justify-center text-white transition shadow"
                  title="Send"
                >
                  <ArrowUp className="w-4 h-4" />
                </button>
              </div>
            </div>
            <SuggestionChips onPick={(t) => handleSendMessage(t)} cameraOn={sharing !== 'off'} />
            <p className="text-[11px] text-slate-600 mt-8 select-none">
              Tip: press <kbd className="px-1.5 py-0.5 rounded bg-white/5 border border-white/10 text-slate-400">Esc</kbd> to interrupt at any moment, or just start talking in hands-free mode.
            </p>
          </div>
        ) : (
          /* ─── SCENARIO B: MORPHED CONVERSATION VIEW ─── */
          <div className="flex-1 flex overflow-hidden">
            
            {/* ─── CONVERSATION PANE ─── */}
            <div className={`flex flex-col justify-between h-full overflow-hidden relative transition-all duration-300 ${
              rightSidebarOpen ? 'w-full lg:w-1/2 border-r border-white/5' : 'w-full'
            }`}>
              
              {/* Chat Message Stream */}
              <div ref={chatScrollRef} className="flex-1 overflow-y-auto px-6 py-6 space-y-6">
                {messages.map((m) => {
                  const isUser = m.role === 'user';
                  const isFiller = m.type === 'filler';

                  if (isUser) {
                    return (
                      <div key={m.id} className="flex justify-end">
                        <div className="bg-white text-zinc-900 text-xs font-medium px-4 py-2.5 rounded-2xl max-w-md shadow-md leading-relaxed">
                          {m.text}
                        </div>
                      </div>
                    );
                  }

                  return (
                    <div key={m.id} className="flex items-start gap-3">
                      {/* Agent avatar */}
                      <div className="w-6 h-6 rounded-md bg-[#252830] border border-white/10 flex items-center justify-center shrink-0 mt-0.5">
                        <Sparkles className="w-3.5 h-3.5 text-amber-400" />
                      </div>
                      <div className="space-y-1 max-w-lg">
                        <div className={`text-xs leading-relaxed ${
                          isFiller ? 'text-amber-300 italic' : 'text-slate-200'
                        }`}>
                          {isFiller ? m.text : <MarkdownReply text={m.text} />}
                        </div>
                      </div>
                    </div>
                  );
                })}
              </div>

              {/* Bottom Docked Input Card (Image 2 style) */}
              <div className="p-4 shrink-0 max-w-2xl mx-auto w-full">
                <div className="bg-[#181a20]/95 backdrop-blur-md border border-white/10 rounded-2xl p-3 shadow-2xl flex flex-col gap-2.5 transition focus-within:border-slate-500">
                  
                  {sharingChip}

                  {/* Hands-free voice status */}
                  {isRecording && (
                    <div className="px-2.5 py-1 bg-sky-500/10 border border-sky-500/20 rounded-lg flex items-center justify-between text-[11px] text-sky-300 font-mono">
                      <span className="flex items-center gap-1.5">
                        <Mic className={`w-3.5 h-3.5 ${isSpeaking ? 'text-rose-400 animate-pulse' : 'text-sky-400'}`} />
                        <span>{isSpeaking ? 'Hearing you…' : 'Listening — speak any time to interrupt'}</span>
                      </span>
                      <button onClick={() => stopVoiceStream()} className="text-[10px] text-sky-400/80 hover:text-white transition">
                        Stop
                      </button>
                    </div>
                  )}

                  {/* Real-time Keystroke Detection Pill */}
                  {isTyping && spawnedBots.some(b => b.status === 'working') && (
                    <div className="px-2.5 py-1 bg-amber-500/10 border border-amber-500/20 rounded-lg flex items-center justify-between text-[11px] text-amber-300 font-mono">
                      <span className="flex items-center gap-1.5">
                        <Eye className="w-3.5 h-3.5 text-amber-400 animate-bounce" />
                        <span>{isSpeaking ? 'Agents hearing you' : 'Agents reading draft'}: "{draftText.slice(0, 26)}{draftText.length > 26 ? '...' : ''}"</span>
                      </span>
                      <span className="text-[10px] text-amber-400/80">{isSpeaking ? 'Finish speaking to adapt' : 'Press Enter to adapt'}</span>
                    </div>
                  )}

                  <input
                    type="text"
                    value={inputText}
                    onChange={(e) => setInputText(e.target.value)}
                    onKeyDown={(e) => e.key === 'Enter' && handleSendMessage()}
                    placeholder="Ask Kairos, or interrupt…"
                    className="w-full bg-transparent text-xs text-slate-200 placeholder:text-slate-500 focus:outline-none px-1 font-sans"
                  />
                  <div className="flex items-center justify-between pt-1 border-t border-white/5">
                    <div className="flex items-center gap-3 text-slate-400">
                      <button className="hover:text-white transition" title="Image"><ImageIcon className="w-4 h-4" /></button>
                      <button className="hover:text-white transition" title="Code"><Code2 className="w-4 h-4" /></button>
                      <button
                        onClick={() => toggleSharing('camera')}
                        className={`hover:text-white transition ${sharing === 'camera' ? 'text-emerald-400' : ''}`}
                        title={sharing === 'camera' ? 'Stop sharing camera' : 'Share camera (agent can look when you ask)'}
                      >
                        <Camera className="w-4 h-4" />
                      </button>
                      <button
                        onClick={() => toggleSharing('screen')}
                        className={`hover:text-white transition ${sharing === 'screen' ? 'text-emerald-400' : ''}`}
                        title={sharing === 'screen' ? 'Stop sharing screen' : 'Share screen (agent can look when you ask)'}
                      >
                        <ScreenShare className="w-4 h-4" />
                      </button>
                      {speakButton}
                      <button 
                        onClick={toggleRecording} 
                        className={`hover:text-white transition ${isRecording ? 'text-rose-400 animate-pulse' : ''}`}
                        title={isRecording ? 'Stop listening' : 'Hands-free voice (speak any time to interrupt)'}
                      >
                        <Mic className="w-4 h-4" />
                      </button>
                    </div>
                    <button 
                      onClick={() => handleSendMessage()}
                      disabled={!inputText.trim()}
                      className="w-7 h-7 rounded-full bg-[#2a2d37] hover:bg-[#353945] disabled:opacity-40 flex items-center justify-center text-white transition shadow"
                    >
                      <ArrowUp className="w-3.5 h-3.5" />
                    </button>
                  </div>
                </div>
              </div>
            </div>

            {/* ─── RIGHT SIDEBAR (Collapsible, opens on artifact) ─── */}
            {rightSidebarOpen && (
              <aside className="w-full lg:w-1/2 flex flex-col h-full overflow-hidden bg-[#0c0e14]/85 backdrop-blur-md transition-all duration-300 border-l border-white/5">
                
                {/* Header with Title and Collapse Button */}
                <div className="px-4 py-2.5 border-b border-white/5 flex items-center justify-between bg-black/40">
                  <div className="flex items-center gap-2 text-xs text-slate-300 font-medium">
                    <span className="w-2 h-2 rounded-full bg-amber-400" />
                    <span>Workspace &amp; Artifact</span>
                  </div>
                  <button
                    onClick={() => setRightSidebarOpen(false)}
                    className="p-1 rounded-lg text-slate-400 hover:text-white hover:bg-white/5 transition"
                    title="Collapse sidebar"
                  >
                    <PanelRightClose className="w-4 h-4" />
                  </button>
                </div>

                {/* TOP HALF: Agent Output & Code Display */}
                <div className="flex-1 p-5 overflow-y-auto space-y-3 border-b border-white/5">
                  {artifact ? (
                    <>
                      {/* Agent header */}
                      <div className="flex items-center gap-2 text-xs text-slate-200">
                        <div className="w-5 h-5 rounded bg-sky-500/20 border border-sky-500/40 flex items-center justify-center">
                          <Bot className="w-3 h-3 text-sky-400" />
                        </div>
                        <span>{artifact.description}</span>
                      </div>

                      {/* Dark Code Container with line numbers */}
                      <div className="rounded-xl border border-white/10 bg-[#12141a]/95 overflow-hidden shadow-2xl">
                        <div className="px-4 py-2 border-b border-white/5 flex items-center justify-between text-[11px] text-slate-400 font-mono">
                          <span>{artifact.title}</span>
                          <button
                            onClick={handleCopyCode}
                            className="flex items-center gap-1 hover:text-white transition text-xs"
                          >
                            {copiedCode ? <Check className="w-3 h-3 text-emerald-400" /> : <Copy className="w-3 h-3" />}
                            <span>{copiedCode ? 'Copied' : 'Copy'}</span>
                          </button>
                          <button
                            onClick={() => handleSendMessage(`Save this code as ${artifact.title || 'code.txt'} and open it in VS Code`)}
                            className="flex items-center gap-1 hover:text-sky-300 transition text-xs text-sky-400"
                            title="Ask Kairos to save this file and open it in VS Code"
                          >
                            <FileCode2 className="w-3 h-3" />
                            <span>Open in VS Code</span>
                          </button>
                        </div>
                        <div className="p-3 font-mono text-[11px] leading-relaxed overflow-x-auto text-slate-300 max-h-[340px]">
                          <pre className="flex">
                            {/* Line numbers */}
                            <span className="select-none text-slate-600 pr-4 text-right border-r border-white/5">
                              {(artifact.content || '').split('\n').map((_, i) => (
                                <div key={i}>{i + 1}</div>
                              ))}
                            </span>
                            {/* Code lines */}
                            <code className="pl-4 text-slate-200">
                              {(artifact.content || '').split('\n').map((line, idx) => {
                                const isImport = line.includes('import') || line.includes('export') || line.includes('default') || line.includes('function') || line.includes('const') || line.includes('return');
                                const isString = line.includes('"') || line.includes("'");
                                const isHook = line.includes('useState') || line.includes('useEffect');

                                return (
                                  <div key={idx} className={isImport ? 'text-amber-400 font-semibold' : isString ? 'text-emerald-300' : isHook ? 'text-purple-300' : 'text-slate-300'}>
                                    {line || ' '}
                                  </div>
                                );
                              })}
                            </code>
                          </pre>
                        </div>
                      </div>
                    </>
                  ) : artifactLoading ? (
                    <div className="flex flex-col items-center justify-center gap-3 py-16 text-center">
                      <div className="w-8 h-8 border-2 border-sky-500/30 border-t-sky-400 rounded-full animate-spin" />
                      <p className="text-xs text-slate-400 font-mono">Agent is generating your artifact...</p>
                    </div>
                  ) : (
                    <div className="flex flex-col items-center justify-center gap-2 py-16 text-center">
                      <p className="text-xs text-slate-500 font-mono">No artifact yet. Ask Kairos to write some code.</p>
                    </div>
                  )}
                </div>

                {/* BOTTOM HALF: Agent Activity Panel */}
                <div className={`${activeRightTab === 'graph' ? 'h-[60%]' : 'h-64'} flex flex-col bg-[#111319]/90 border-t border-white/10 shrink-0 transition-[height] duration-200`}>
                  {/* Section Header */}
                  <div className="px-5 py-3 border-b border-white/5 flex items-center justify-between bg-black/30">
                    <div className="flex items-center gap-2">
                      <h2 className="text-base font-bold tracking-tight text-white font-sans">
                        Live Agent Activity
                      </h2>
                      <span className="text-[10px] font-mono px-2 py-0.5 rounded-full bg-slate-800 text-slate-400 border border-white/5">
                        {spawnedBots.filter((b) => b.status === 'working').length} active · {spawnedBots.length} total
                      </span>
                    </div>

                    {/* Tabs for Bots Swarm, Snapshot, Trace */}
                    <div className="flex items-center gap-1.5 bg-[#1a1d24] p-0.5 rounded-lg border border-white/5 text-[11px] font-mono">
                      <button
                        onClick={() => setActiveRightTab('agents')}
                        className={`px-2.5 py-1 rounded-md transition ${activeRightTab === 'agents' ? 'bg-[#282b34] text-white font-semibold' : 'text-slate-400 hover:text-slate-200'}`}
                      >
                        Bots Swarm
                      </button>
                      <button
                        onClick={() => setActiveRightTab('snapshot')}
                        className={`px-2.5 py-1 rounded-md transition ${activeRightTab === 'snapshot' ? 'bg-[#282b34] text-white font-semibold' : 'text-slate-400 hover:text-slate-200'}`}
                      >
                        Snapshot
                      </button>
                      <button
                        onClick={() => setActiveRightTab('trace')}
                        className={`px-2.5 py-1 rounded-md transition ${activeRightTab === 'trace' ? 'bg-[#282b34] text-white font-semibold' : 'text-slate-400 hover:text-slate-200'}`}
                      >
                        Trace
                      </button>
                      <button
                        onClick={() => setActiveRightTab('graph')}
                        className={`px-2.5 py-1 rounded-md transition ${activeRightTab === 'graph' ? 'bg-[#282b34] text-white font-semibold' : 'text-slate-400 hover:text-slate-200'}`}
                      >
                        Cognitive Graph
                      </button>
                      <button
                        onClick={() => setActiveRightTab('exports')}
                        className={`px-2.5 py-1 rounded-md transition ${activeRightTab === 'exports' ? 'bg-[#282b34] text-white font-semibold' : 'text-slate-400 hover:text-slate-200'}`}
                      >
                        Exports{exportsList.length > 0 ? ` (${exportsList.length})` : ''}
                      </button>
                    </div>
                  </div>

                  {/* Sub-Panel Content */}
                  <div className="flex-1 p-4 overflow-y-auto">
                    {/* TAB 1: Spawned Bots List */}
                    {activeRightTab === 'agents' && (
                      <div className="space-y-2.5">
                        {spawnedBots.length === 0 ? (
                          <div className="text-center py-6 text-slate-500 text-xs font-mono">
                            No worker agents yet. Ask Kairos to build something.
                          </div>
                        ) : (
                          spawnedBots.map((bot) => {
                            const isCancelled = bot.status === 'cancelled';
                            const isCompleted = bot.status === 'completed';
                            const isWorking = bot.status === 'working';

                            return (
                              <div 
                                key={bot.call_id} 
                                className={`flex items-start gap-3 p-3 rounded-xl border transition-all duration-300 ${
                                  isCancelled 
                                    ? 'bg-rose-950/20 border-rose-800/40 opacity-75' 
                                    : isWorking && isTyping
                                    ? 'bg-amber-950/20 border-amber-500/50 ring-1 ring-amber-500/30'
                                    : isWorking
                                    ? 'bg-[#161a24] border-sky-500/30 shadow'
                                    : 'bg-[#15171f] border-white/5'
                                }`}
                              >
                                <MiniBotAvatar status={bot.status} isWatching={isWorking && isTyping} />
                                
                                <div className="flex-1 min-w-0">
                                  <div className="flex items-center justify-between gap-1">
                                    <div className="flex items-center gap-2">
                                      <span className="font-bold text-xs text-white font-mono">{bot.name}</span>
                                      <span className="text-[10px] text-slate-500 font-mono">({bot.role})</span>
                                    </div>
                                    <span className={`text-[10px] font-mono px-2 py-0.5 rounded uppercase font-bold tracking-wider ${
                                      isCancelled 
                                        ? 'bg-rose-500/20 text-rose-300 border border-rose-500/30' 
                                        : isWorking && isTyping
                                        ? 'bg-amber-500/20 text-amber-300 border border-amber-500/40 animate-pulse'
                                        : isWorking
                                        ? 'bg-sky-500/20 text-sky-300 border border-sky-500/30'
                                        : 'bg-emerald-500/20 text-emerald-300'
                                    }`}>
                                      {isCancelled ? 'ABORTED' : isWorking && isTyping ? 'READING DRAFT...' : bot.status}
                                    </span>
                                  </div>

                                  <p className={`text-[11px] mt-1 leading-snug ${
                                    isCancelled 
                                      ? 'text-rose-300 line-through' 
                                      : isWorking && isTyping
                                      ? 'text-amber-200 italic font-mono'
                                      : 'text-slate-400 font-sans'
                                  }`}>
                                    {isWorking && isTyping 
                                      ? `👀 ${isSpeaking ? 'Hearing you' : 'Noticing typing'}: "${draftText.slice(0, 36)}${draftText.length > 36 ? '...' : ''}"` 
                                      : bot.thought}
                                  </p>

                                  {bot.total_steps > 0 && bot.step > 0 && !isCancelled && (
                                    <div className="mt-1.5 space-y-1">
                                      <div className="flex items-center justify-between text-[10px] text-slate-400 font-mono">
                                        <span>Step {bot.step}/{bot.total_steps}</span>
                                        <span className="text-sky-300 font-semibold">{Math.round((bot.step / bot.total_steps) * 100)}%</span>
                                      </div>
                                      <div className="w-full bg-slate-800 h-1 rounded-full overflow-hidden">
                                        <div 
                                          className={`h-full transition-all duration-300 ${bot.status === 'completed' ? 'bg-emerald-400' : 'bg-sky-400'}`}
                                          style={{ width: `${Math.round((bot.step / bot.total_steps) * 100)}%` }}
                                        />
                                      </div>
                                    </div>
                                  )}

                                  <div className="mt-1.5 flex items-center justify-between text-[10px] text-slate-500 font-mono">
                                    <span>Epoch #{bot.epoch} · ID: {bot.call_id}</span>
                                    {isWorking && (
                                      <span className="text-sky-400 flex items-center gap-1 font-semibold">
                                        <span className="w-1.5 h-1.5 rounded-full bg-sky-400 animate-ping" /> Working live
                                      </span>
                                    )}
                                  </div>

                                </div>
                              </div>
                            );
                          })
                        )}
                      </div>
                    )}

                    {/* TAB 2: State Snapshot */}
                    {activeRightTab === 'snapshot' && (
                      <div className="space-y-3 font-mono text-xs">
                        <div className="grid grid-cols-2 gap-2 text-[11px]">
                          <div className="bg-[#161a24] p-2.5 rounded-lg border border-white/5">
                            <span className="text-slate-500 block">Intent</span>
                            <span className="font-bold text-sky-300">{snapshot.intent || 'None'}</span>
                          </div>
                          <div className="bg-[#161a24] p-2.5 rounded-lg border border-white/5">
                            <span className="text-slate-500 block">Epoch</span>
                            <span className="font-bold text-emerald-400">#{snapshot.epoch}</span>
                          </div>
                        </div>
                        <div>
                          <span className="text-[10px] text-slate-500 uppercase font-bold block mb-1">Slots</span>
                          <div className="space-y-1">
                            {Object.keys(snapshot.slots || {}).length === 0 ? (
                              <div className="text-slate-500 text-xs italic">No active slots</div>
                            ) : (
                              Object.entries(snapshot.slots || {}).map(([k, v]) => (
                                <div key={k} className="p-1.5 bg-[#161a24] rounded border border-white/5 flex justify-between text-[11px]">
                                  <span className="text-slate-400">{k}:</span>
                                  <span className="text-emerald-300 font-semibold">{String(v)}</span>
                                </div>
                              ))
                            )}
                          </div>
                        </div>
                      </div>
                    )}

                    {/* TAB 4: Cognitive Graph Visualizer */}
                    {activeRightTab === 'exports' && (
                      <ExportsList items={exportsList} authToken={authToken} onOpenInEditor={openInEditor} />
                    )}

                    {activeRightTab === 'graph' && (() => {
                      const columns = { turn: 0, entity: 1, artifact: 2 };
                      const colColors = { turn: '#38bdf8', entity: '#facc15', artifact: '#c084fc' };
                      const colCounts = { turn: 0, entity: 0, artifact: 0 };
                      const colWidth = 150;
                      const rowHeight = 56;
                      const positioned = graphNodes.map((node) => {
                        const col = columns[node.node_type] ?? 0;
                        const row = colCounts[node.node_type] ?? 0;
                        colCounts[node.node_type] = row + 1;
                        return { ...node, x: 40 + col * colWidth, y: 24 + row * rowHeight };
                      });
                      const byId = Object.fromEntries(positioned.map((n) => [n.id, n]));
                      const svgHeight = Math.max(160, (Math.max(...Object.values(colCounts), 1)) * rowHeight + 48);

                      return (
                        <div className="space-y-2 font-mono text-[11px]">
                          {graphNodes.length === 0 ? (
                            <div className="text-center py-6 text-slate-500 text-xs font-mono">
                              No cognitive graph yet. Turns, entities, and artifacts will appear here as the conversation progresses.
                            </div>
                          ) : (
                            <>
                              <div className="flex items-center gap-3 text-[10px] text-slate-400 px-1">
                                <span className="flex items-center gap-1"><span className="w-2 h-2 rounded-full bg-sky-400 inline-block" /> Turn</span>
                                <span className="flex items-center gap-1"><span className="w-2 h-2 rounded-full bg-yellow-400 inline-block" /> Entity</span>
                                <span className="flex items-center gap-1"><span className="w-2 h-2 rounded-full bg-purple-400 inline-block" /> Artifact</span>
                              </div>
                              <div className="bg-[#0f1117] rounded-lg border border-white/5 p-2 overflow-auto">
                                <svg width={40 + 3 * colWidth} height={svgHeight}>
                                  {graphEdges.map((edge, i) => {
                                    const s = byId[edge.source];
                                    const t = byId[edge.target];
                                    if (!s || !t) return null;
                                    const isBack = t.x <= s.x && edge.edge_type !== 'PRODUCED' && edge.edge_type !== 'REFERENCES';
                                    return (
                                      <line
                                        key={`${edge.source}-${edge.target}-${i}`}
                                        x1={s.x} y1={s.y} x2={t.x} y2={t.y}
                                        stroke={isBack ? '#f43f5e' : '#3f4657'}
                                        strokeWidth={1.5}
                                        strokeDasharray={edge.edge_type === 'SUPERSEDES' || edge.edge_type === 'BRANCHES_FROM' ? '3,3' : undefined}
                                      />
                                    );
                                  })}
                                  {positioned.map((node) => {
                                    const fullLabel = node.label || '';
                                    const displayLabel = fullLabel.length > 18 ? `${fullLabel.slice(0, 17)}…` : fullLabel;
                                    return (
                                      <g
                                        key={node.id}
                                        onClick={() => setSelectedGraphNode(node)}
                                        style={{ cursor: 'pointer' }}
                                      >
                                        <title>{fullLabel}</title>
                                        <circle cx={node.x} cy={node.y} r={7} fill={colColors[node.node_type] || '#94a3b8'} stroke="#0f1117" strokeWidth={2} />
                                        <text x={node.x + 12} y={node.y + 4} fill="#cbd5e1" fontSize="9">
                                          {displayLabel}
                                        </text>
                                      </g>
                                    );
                                  })}
                                </svg>
                              </div>
                              {selectedGraphNode && (
                                <div className="bg-[#161a24] p-2.5 rounded-lg border border-white/5 space-y-1">
                                  <div className="flex justify-between items-center">
                                    <span className="text-slate-500 uppercase text-[10px]">{selectedGraphNode.node_type} node</span>
                                    <button onClick={() => setSelectedGraphNode(null)} className="text-slate-500 hover:text-white">✕</button>
                                  </div>
                                  <div className="text-slate-200 font-semibold">{selectedGraphNode.label}</div>
                                  <pre className="text-[10px] text-slate-400 whitespace-pre-wrap break-all">
                                    {JSON.stringify(selectedGraphNode.data, null, 2)}
                                  </pre>
                                </div>
                              )}
                            </>
                          )}
                        </div>
                      );
                    })()}

                    {/* TAB 3: Trace Logs */}
                    {activeRightTab === 'trace' && (
                      <div className="space-y-1.5 font-mono text-[11px]">
                        <TraceTimeline items={traceLogs} />
                        {traceLogs.map((item) => (
                          <div key={item.id} className="p-2 rounded bg-[#161a24] border border-white/5 space-y-0.5">
                            <div className="flex justify-between text-[10px]">
                              <span className={`font-bold uppercase ${
                                item.type === 'tool_cancel' ? 'text-rose-400' :
                                item.type === 'tool_call' ? 'text-amber-400' :
                                item.type === 'filler' ? 'text-sky-300' :
                                'text-slate-400'
                              }`}>
                                [{item.type}]
                              </span>
                              <span className="text-slate-500">{item.time}</span>
                            </div>
                            <p className="text-slate-300 text-[10px]">{item.text}</p>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                </div>
              </aside>
            )}
          </div>
        )}
      </div>

      {/* ─────────────────────────────────────────────────────────────
          3. SETTINGS MODAL (Triggered by Left Sidebar Tab)
      ───────────────────────────────────────────────────────────── */}
      {settingsOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm p-4">
          <div className="w-full max-w-md bg-[#161820] border border-white/10 rounded-2xl p-6 shadow-2xl space-y-5 text-slate-200">
            <div className="flex items-center justify-between border-b border-white/10 pb-3">
              <div className="flex items-center gap-2">
                <Settings className="w-5 h-5 text-amber-400" />
                <h3 className="font-bold text-sm text-white">System Settings</h3>
              </div>
              <button 
                onClick={() => setSettingsOpen(false)}
                className="text-slate-400 hover:text-white p-1 rounded-lg hover:bg-white/5 transition"
              >
                <X className="w-4 h-4" />
              </button>
            </div>

            <div className="space-y-4 text-xs">
              <div className="space-y-1.5">
                <label className="text-slate-400 font-medium">LLM Reasoning Engine</label>
                <select
                  value={selectedModel}
                  onChange={(e) => setSelectedModel(e.target.value)}
                  className="w-full bg-[#1e212b] border border-white/10 rounded-xl px-3 py-2 text-slate-200 focus:outline-none focus:border-amber-400"
                >
                  <option value="openai/gpt-oss-120b">openai/gpt-oss-120b (Groq — Fast Re-planner)</option>
                  <option value="qwen/qwen-2.5-7b-instruct">qwen/qwen-2.5-7b-instruct (OpenRouter)</option>
                  <option value="meta-llama/llama-3.1-8b-instruct">meta-llama/llama-3.1-8b-instruct</option>
                  <option value="google/gemini-2.0-flash">google/gemini-2.0-flash</option>
                </select>
              </div>

              <div className="space-y-1.5">
                <label className="text-slate-400 font-medium">Speech-to-Text (ASR) Pipeline</label>
                <input
                  type="text"
                  readOnly
                  value={asrEngine}
                  className="w-full bg-[#1e212b] border border-white/10 rounded-xl px-3 py-2 text-slate-400 select-none"
                />
              </div>

              <div className="space-y-2 pt-2 border-t border-white/5">
                <span className="text-slate-400 font-medium block">Interruption &amp; Full-Duplex</span>
                <div className="p-3 rounded-xl bg-[#1b1e27] border border-white/5 space-y-2">
                  <div className="flex items-center justify-between text-[11px]">
                    <span className="text-slate-300">Continuous Draft Watching</span>
                    <span className="text-emerald-400 font-semibold font-mono">ENABLED</span>
                  </div>
                  <div className="flex items-center justify-between text-[11px]">
                    <span className="text-slate-300">Monotonic Epoch Bumping</span>
                    <span className="text-emerald-400 font-semibold font-mono">ACTIVE</span>
                  </div>
                  <div className="flex items-center justify-between text-[11px]">
                    <span className="text-slate-300">Active Session ID</span>
                    <span className="text-sky-300 font-mono text-[10px]">{sessionId}</span>
                  </div>
                </div>
              </div>
            </div>

            <div className="pt-2 flex justify-end">
              <button
                onClick={() => setSettingsOpen(false)}
                className="px-4 py-2 rounded-xl bg-amber-500 hover:bg-amber-400 text-slate-950 font-semibold text-xs transition"
              >
                Done
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
