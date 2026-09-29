const TYPE_COLOR = {
  tool_call: '#fbbf24',      // amber
  tool_cancel: '#f43f5e',    // rose
  filler: '#38bdf8',         // sky
  response: '#38bdf8',
  snapshot: '#34d399',       // emerald
  graph_update: '#34d399',
  agent_step: '#a78bfa',     // violet
  audio: '#c084fc',
  vision: '#c084fc',
  system: '#64748b',         // slate
};

function colorFor(type) {
  return TYPE_COLOR[type] || '#94a3b8';
}

/**
 * Horizontal timeline of trace events (§5.1 recommendation): every filler, tool call,
 * cancellation and voice event as a tick positioned by wall-clock time, so a cancellation
 * landing right after a correction is visible at a glance instead of buried in a scrolling list.
 */
export default function TraceTimeline({ items }) {
  if (!items || items.length === 0) return null;

  const first = items[0].ts ?? 0;
  const last = items[items.length - 1].ts ?? first;
  const span = Math.max(last - first, 1000); // avoid a zero-width scale for a single/instant event

  return (
    <div className="bg-[#0f1117] border border-white/5 rounded-lg p-3 mb-2">
      <div className="flex justify-between text-[9px] text-slate-500 mb-1.5 uppercase tracking-wide">
        <span>Trace Timeline</span>
        <span>{(span / 1000).toFixed(1)}s span</span>
      </div>
      <div className="relative h-6">
        <div className="absolute left-0 right-0 top-1/2 h-px bg-white/10" />
        {items.map((item) => {
          const pct = Math.min(100, Math.max(0, (((item.ts ?? first) - first) / span) * 100));
          const isCancel = item.type === 'tool_cancel';
          return (
            <div
              key={item.id}
              title={`[${item.type}] ${item.time} — ${item.text}`}
              className="absolute top-1/2 -translate-y-1/2 -translate-x-1/2 rounded-full cursor-default transition-transform hover:scale-150"
              style={{
                left: `${pct}%`,
                width: isCancel ? 8 : 6,
                height: isCancel ? 8 : 6,
                background: colorFor(item.type),
                boxShadow: isCancel ? `0 0 6px ${colorFor(item.type)}` : undefined,
              }}
            />
          );
        })}
      </div>
      <div className="flex flex-wrap gap-x-3 gap-y-1 mt-2 text-[9px] text-slate-500">
        {Object.entries(TYPE_COLOR).map(([type, color]) => (
          <span key={type} className="flex items-center gap-1">
            <span className="w-1.5 h-1.5 rounded-full inline-block" style={{ background: color }} />
            {type}
          </span>
        ))}
      </div>
    </div>
  );
}
