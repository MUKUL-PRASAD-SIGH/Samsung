import { Check, Copy, Download, FileCode2 } from 'lucide-react';
import { useState } from 'react';

/** Files the agent exported (export_artifact): open in VS Code, download, or copy the path. */
export default function ExportsList({ items, authToken, onOpenInEditor }) {
  const [copied, setCopied] = useState('');
  const copy = async (path) => {
    try {
      await navigator.clipboard.writeText(path);
      setCopied(path);
      setTimeout(() => setCopied(''), 1500);
    } catch { /* clipboard unavailable (insecure context): nothing to do */ }
  };
  // A plain <a download> cannot send the bearer token, so fetch the file and hand the browser a blob.
  const download = async (item) => {
    const res = await fetch(item.download_path, authToken ? { headers: { Authorization: `Bearer ${authToken}` } } : undefined);
    if (!res.ok) return;
    const url = URL.createObjectURL(await res.blob());
    const a = document.createElement('a');
    a.href = url;
    a.download = item.filename;
    a.click();
    URL.revokeObjectURL(url);
  };

  if (!items.length) {
    return (
      <div className="flex flex-col items-center justify-center gap-2 py-10 text-center">
        <FileCode2 className="w-6 h-6 text-slate-600" />
        <p className="text-xs text-slate-500">Nothing exported yet. Ask for some code, then say “open it in VS Code”.</p>
      </div>
    );
  }
  return (
    <div className="space-y-2">
      {items.map((it) => (
        <div key={it.id} className="rounded-xl bg-[#161a24] border border-white/5 p-3 space-y-2">
          <div className="flex items-center justify-between gap-2">
            <div className="flex items-center gap-2 min-w-0">
              <FileCode2 className="w-4 h-4 text-amber-300 shrink-0" />
              <span className="text-xs font-semibold text-white truncate">{it.filename}</span>
            </div>
            <span className="text-[10px] text-slate-500 font-mono shrink-0">{(it.bytes / 1000).toFixed(1)} KB</span>
          </div>
          <div className="text-[10px] text-slate-500 font-mono truncate" title={it.path}>{it.path}</div>
          {it.opened_with && <div className="text-[10px] text-emerald-400">Opened in {it.opened_with === 'code' ? 'VS Code' : it.opened_with} on the server machine</div>}
          <div className="flex flex-wrap gap-1.5">
            <button onClick={() => onOpenInEditor(it)} className="px-2.5 py-1 rounded-md text-[11px] bg-sky-500/15 text-sky-300 border border-sky-500/30 hover:bg-sky-500/25 transition">
              Open in VS Code
            </button>
            <button onClick={() => download(it)} className="px-2.5 py-1 rounded-md text-[11px] bg-white/5 text-slate-300 border border-white/10 hover:bg-white/10 transition flex items-center gap-1">
              <Download className="w-3 h-3" /> Download
            </button>
            <button onClick={() => copy(it.path)} className="px-2.5 py-1 rounded-md text-[11px] bg-white/5 text-slate-300 border border-white/10 hover:bg-white/10 transition flex items-center gap-1">
              {copied === it.path ? <Check className="w-3 h-3 text-emerald-400" /> : <Copy className="w-3 h-3" />} Path
            </button>
          </div>
        </div>
      ))}
    </div>
  );
}
