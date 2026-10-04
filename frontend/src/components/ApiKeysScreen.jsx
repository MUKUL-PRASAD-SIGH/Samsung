import { useState } from 'react';
import { Check, Eye, EyeOff, KeyRound, Loader2, X } from 'lucide-react';
import { Logo, Wordmark } from './Brand';

const PROVIDERS = [
  { id: 'groq', label: 'Groq', placeholder: 'gsk_…', link: 'https://console.groq.com/keys', note: 'Fastest replies (recommended)' },
  { id: 'openrouter', label: 'OpenRouter', placeholder: 'sk-or-…', link: 'https://openrouter.ai/keys', note: 'Also used for camera / vision questions' },
];

function KeyField({ provider, status, value, onChange, error }) {
  const [show, setShow] = useState(false);
  const configured = status?.[provider.id]?.configured;
  return (
    <label className="w-full block">
      <div className="flex items-center justify-between mb-1">
        <span className="text-xs font-medium text-slate-300">{provider.label} API key</span>
        {configured && !value && (
          <span className="text-[11px] text-emerald-400 flex items-center gap-1"><Check className="w-3 h-3" /> saved {status[provider.id].hint}</span>
        )}
      </div>
      <div className={`flex items-center gap-2 bg-[#1b1f29] border rounded-xl px-3 py-2.5 transition ${error ? 'border-rose-500/60' : 'border-white/10 focus-within:border-amber-300/50'}`}>
        <KeyRound className="w-4 h-4 text-slate-400 shrink-0" />
        <input
          type={show ? 'text' : 'password'} value={value} onChange={(e) => onChange(e.target.value)}
          placeholder={configured ? 'Enter a new key to replace it' : provider.placeholder}
          autoComplete="off" spellCheck={false} aria-invalid={!!error}
          className="flex-1 bg-transparent text-sm text-slate-100 placeholder:text-slate-500 focus:outline-none"
        />
        <button type="button" onClick={() => setShow(!show)} className="text-slate-400 hover:text-white transition" title={show ? 'Hide key' : 'Show key'}>
          {show ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
        </button>
      </div>
      <p className="text-[11px] text-slate-500 mt-1">
        {provider.note}. <a href={provider.link} target="_blank" rel="noreferrer" className="text-sky-400 hover:underline">Get a key</a>
      </p>
      {error && <p role="alert" className="text-xs text-rose-300 mt-1">{error}</p>}
    </label>
  );
}

/**
 * Where a user gives Kairos their own Groq / OpenRouter API keys. Shown full-screen on first run (the desktop app has no
 * `.env` and no access token) and as a dialog from Settings. Keys are sent to the local server, which checks them with the
 * provider before saving them on this machine; they are never shown again, only a masked hint.
 *
 * onSave(updates) resolves to true, or {error, field}. `onSkip` (first run only) continues in offline demo mode.
 */
export default function ApiKeysScreen({ status, onSave, onSkip, onClose }) {
  const [values, setValues] = useState({ groq: '', openrouter: '' });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState({});
  const [notice, setNotice] = useState('');
  const dialog = !!onClose;
  const anyTyped = Object.values(values).some((v) => v.trim());

  const submit = async (e) => {
    e.preventDefault();
    if (!anyTyped || busy) return;
    setBusy(true);
    setError({});
    setNotice('');
    const updates = {};
    PROVIDERS.forEach((p) => { if (values[p.id].trim()) updates[`${p.id}_api_key`] = values[p.id].trim(); });
    const result = await onSave(updates);
    setBusy(false);
    if (result !== true) setError({ [result?.field || 'form']: result?.error || 'Could not save the key.' });
    else setValues({ groq: '', openrouter: '' });
  };

  const clear = async (provider) => {
    setBusy(true);
    setNotice('');
    const result = await onSave({ [`${provider}_api_key`]: '' });
    setBusy(false);
    if (result !== true) setError({ form: result?.error || 'Could not remove the key.' });
    else setNotice(`${provider === 'groq' ? 'Groq' : 'OpenRouter'} key removed.`);
  };

  const form = (
    <form onSubmit={submit} className="relative w-full max-w-md bg-[#12151c]/95 backdrop-blur-md border border-white/10 rounded-3xl p-8 shadow-2xl flex flex-col items-center gap-5">
      {dialog && (
        <button type="button" onClick={onClose} className="absolute top-4 right-4 text-slate-400 hover:text-white p-1 rounded-lg hover:bg-white/5 transition" title="Close">
          <X className="w-4 h-4" />
        </button>
      )}
      <Logo className="w-14 h-14" />
      <Wordmark size="sm" />
      <div className="text-center -mt-1">
        <h2 className="text-lg font-semibold text-white">{dialog ? 'API keys' : 'Connect your AI provider'}</h2>
        <p className="text-xs text-slate-400 mt-1">
          Add a Groq and/or OpenRouter key. Either one is enough; if both are set, Groq is used for replies.
        </p>
      </div>

      {PROVIDERS.map((p) => (
        <div key={p.id} className="w-full">
          <KeyField provider={p} status={status} value={values[p.id]} error={error[p.id]}
            onChange={(v) => { setValues({ ...values, [p.id]: v }); setError({}); }} />
          {status?.[p.id]?.configured && !values[p.id] && (
            <button type="button" disabled={busy} onClick={() => clear(p.id)} className="text-[11px] text-slate-500 hover:text-rose-300 mt-1 transition">
              Remove saved {p.label} key
            </button>
          )}
        </div>
      ))}

      {error.form && <p role="alert" className="text-xs text-rose-300 w-full">{error.form}</p>}
      {notice && <p className="text-xs text-emerald-300 w-full">{notice}</p>}

      <button type="submit" disabled={!anyTyped || busy}
        className="w-full rounded-xl py-2.5 text-sm font-semibold text-[#0b0e14] bg-gradient-to-r from-amber-200 to-amber-400 hover:from-amber-100 hover:to-amber-300 disabled:opacity-40 transition flex items-center justify-center gap-2">
        {busy ? <><Loader2 className="w-4 h-4 animate-spin" /> Checking with the provider…</> : (dialog ? 'Save keys' : 'Save and continue')}
      </button>
      {!dialog && onSkip && (
        <button type="button" onClick={onSkip} className="text-xs text-slate-400 hover:text-white transition">
          Skip for now: use the offline demo mode
        </button>
      )}
      <p className="text-[11px] text-slate-500 text-center">
        Keys are checked with the provider, then stored only on this computer (in your user profile). They are sent nowhere else.
      </p>
    </form>
  );

  if (dialog) {
    return <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm p-4">{form}</div>;
  }
  return (
    <div className="h-screen w-screen flex items-center justify-center bg-[#0b0e14] relative overflow-hidden">
      <div className="absolute inset-0 pointer-events-none" style={{
        background: 'radial-gradient(60% 50% at 50% 35%, rgba(212,160,23,0.10), transparent 70%), radial-gradient(40% 40% at 80% 90%, rgba(56,189,248,0.08), transparent 70%)',
      }} />
      {form}
    </div>
  );
}
