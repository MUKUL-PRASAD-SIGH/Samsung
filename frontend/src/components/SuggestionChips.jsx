import { Code2, CloudSun, Plane, Save, Timer, Eye } from 'lucide-react';

// What to try, grouped by what Kairos can do. Each one is a complete request, so a click is a working demo.
export const SUGGESTIONS = [
  { icon: Plane, label: 'Find flights', text: 'Find flights from Delhi to Mumbai' },
  { icon: CloudSun, label: 'Weather', text: "What's the weather like in Paris?" },
  { icon: Code2, label: 'Write code', text: 'Write a TypeScript debounce function with a short usage example' },
  { icon: Save, label: 'Open in VS Code', text: 'Write a Python function that reverses a string, save it as reverse.py and open it in VS Code' },
  { icon: Timer, label: 'Set a timer', text: 'Set a 20 second timer for my tea' },
  { icon: Eye, label: 'Look at this', text: 'What does this sign say?', needsCamera: true },
];

export default function SuggestionChips({ onPick, cameraOn }) {
  return (
    <div className="flex flex-wrap justify-center gap-2 max-w-2xl mt-6">
      {SUGGESTIONS.map(({ icon: Icon, label, text, needsCamera }) => (
        <button key={label} onClick={() => onPick(text)}
          title={needsCamera && !cameraOn ? 'Turn on the camera first, then ask' : text}
          className="group flex items-center gap-2 px-3.5 py-2 rounded-full bg-white/[0.04] hover:bg-white/[0.09] border border-white/10 hover:border-amber-300/40 text-xs text-slate-300 hover:text-white transition">
          <Icon className="w-3.5 h-3.5 text-amber-300/80 group-hover:text-amber-300" />
          {label}
        </button>
      ))}
    </div>
  );
}
