import { useState } from 'react';
import { Eye, EyeOff, KeyRound, Loader2 } from 'lucide-react';
import { Logo, Wordmark } from './Brand';

/**
 * Shown when the server requires an access key (AUTH_TOKEN). The key is verified against the server before it is stored,
 * so a typo is reported here, not as a mysteriously refused connection.
 */
export default function LoginScreen({ onSubmit, initialError = '' }) {
  const [key, setKey] = useState('');
  const [show, setShow] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(initialError);

  const submit = async (e) => {
    e.preventDefault();
    if (!key.trim() || busy) return;
    setBusy(true);
    setError('');
    const result = await onSubmit(key.trim());
    setBusy(false);
    if (result !== true) setError(result || 'That key was not accepted.');
  };

  return (
    <div className="h-screen w-screen flex items-center justify-center bg-[#0b0e14] relative overflow-hidden">
      <div className="absolute inset-0 pointer-events-none" style={{
        background: 'radial-gradient(60% 50% at 50% 35%, rgba(212,160,23,0.10), transparent 70%), radial-gradient(40% 40% at 80% 90%, rgba(56,189,248,0.08), transparent 70%)',
      }} />
      <form onSubmit={submit} className="relative w-full max-w-sm bg-[#12151c]/90 backdrop-blur-md border border-white/10 rounded-3xl p-8 shadow-2xl flex flex-col items-center gap-6">
        <Logo className="w-16 h-16" />
        <Wordmark size="sm" />
        <div className="text-center -mt-2">
          <h2 className="text-lg font-semibold text-white">Welcome back</h2>
          <p className="text-xs text-slate-400 mt-1">Enter your access key to continue.</p>
        </div>

        <label className="w-full">
          <span className="sr-only">Access key</span>
          <div className={`flex items-center gap-2 bg-[#1b1f29] border rounded-xl px-3 py-2.5 transition ${error ? 'border-rose-500/60' : 'border-white/10 focus-within:border-amber-300/50'}`}>
            <KeyRound className="w-4 h-4 text-slate-400 shrink-0" />
            <input
              type={show ? 'text' : 'password'} value={key} onChange={(e) => { setKey(e.target.value); setError(''); }}
              placeholder="Access key" autoFocus autoComplete="current-password" spellCheck={false}
              className="flex-1 bg-transparent text-sm text-slate-100 placeholder:text-slate-500 focus:outline-none"
              aria-invalid={!!error}
            />
            <button type="button" onClick={() => setShow(!show)} className="text-slate-400 hover:text-white transition" title={show ? 'Hide key' : 'Show key'}>
              {show ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
            </button>
          </div>
        </label>

        {error && <p role="alert" className="text-xs text-rose-300 -mt-3 w-full">{error}</p>}

        <button type="submit" disabled={!key.trim() || busy}
          className="w-full rounded-xl py-2.5 text-sm font-semibold text-[#0b0e14] bg-gradient-to-r from-amber-200 to-amber-400 hover:from-amber-100 hover:to-amber-300 disabled:opacity-40 transition flex items-center justify-center gap-2">
          {busy ? <><Loader2 className="w-4 h-4 animate-spin" /> Checking…</> : 'Continue'}
        </button>
        <p className="text-[11px] text-slate-500 text-center">
          The key is the server&apos;s <code className="text-slate-400">AUTH_TOKEN</code>. It is stored on this device only.
        </p>
      </form>
    </div>
  );
}
