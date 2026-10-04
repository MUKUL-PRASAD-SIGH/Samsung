import { useCallback, useRef, useState } from 'react';
import { AlertTriangle, CheckCircle2, Info, X } from 'lucide-react';

/** Small transient messages ("Interrupted", "Saved to VS Code", errors) instead of blocking alert() boxes. */
export function useToasts() {
  const [toasts, setToasts] = useState([]);
  const next = useRef(1);
  const dismiss = useCallback((id) => setToasts((t) => t.filter((x) => x.id !== id)), []);
  const push = useCallback((text, kind = 'info', ms = 4000) => {
    const id = next.current++;
    setToasts((t) => [...t.slice(-3), { id, text, kind }]);
    if (ms > 0) setTimeout(() => dismiss(id), ms);
    return id;
  }, [dismiss]);
  return { toasts, push, dismiss };
}

const ICON = { info: Info, ok: CheckCircle2, error: AlertTriangle };
const TONE = {
  info: 'border-sky-400/30 text-sky-100',
  ok: 'border-emerald-400/30 text-emerald-100',
  error: 'border-rose-400/40 text-rose-100',
};

export function ToastStack({ toasts, onDismiss }) {
  return (
    <div className="fixed bottom-5 left-1/2 -translate-x-1/2 z-[60] flex flex-col gap-2 items-center pointer-events-none" aria-live="polite">
      {toasts.map((t) => {
        const Icon = ICON[t.kind] || Info;
        return (
          <div key={t.id} className={`pointer-events-auto flex items-center gap-2.5 bg-[#161a22]/95 backdrop-blur-md border rounded-xl px-4 py-2.5 text-xs shadow-2xl ${TONE[t.kind] || TONE.info}`}>
            <Icon className="w-4 h-4 shrink-0" />
            <span>{t.text}</span>
            <button onClick={() => onDismiss(t.id)} className="text-slate-400 hover:text-white ml-1" title="Dismiss"><X className="w-3.5 h-3.5" /></button>
          </div>
        );
      })}
    </div>
  );
}
