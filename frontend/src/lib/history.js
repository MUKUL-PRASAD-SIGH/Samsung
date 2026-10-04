// Conversation history kept on this device. A conversation IS a server session: reopening one reconnects with the same
// session id, so the agent's memory of it (while the server is up) comes back too.
const KEY = 'kairos.history';
const MAX_CHATS = 30;
const MAX_MESSAGES = 200;

export function loadHistory(storage = localStorage) {
  try {
    const parsed = JSON.parse(storage.getItem(KEY) || '[]');
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

export function titleFrom(messages) {
  const first = messages.find((m) => m.role === 'user' && m.text && !m.text.startsWith('🎤 …'));
  const text = (first ? first.text : 'New conversation').replace(/^🎤\s*/, '').trim();
  return text.length > 42 ? text.slice(0, 41) + '…' : text;
}

/** Insert/refresh one conversation, newest first, bounded. Pure: returns the new list. */
export function upsert(history, id, messages, now = Date.now()) {
  const kept = messages.filter((m) => m.text).slice(-MAX_MESSAGES).map(({ id: mid, role, text, type }) => ({ id: mid, role, text, type }));
  const entry = { id, title: titleFrom(kept), updatedAt: now, messages: kept };
  return [entry, ...history.filter((h) => h.id !== id)].slice(0, MAX_CHATS);
}

export function save(history, storage = localStorage) {
  try {
    storage.setItem(KEY, JSON.stringify(history));
  } catch { /* storage full or blocked: history is a convenience, never a failure */ }
}

export function remove(history, id) {
  return history.filter((h) => h.id !== id);
}

export function newSessionId() {
  return 'web_' + Math.random().toString(36).slice(2, 10) + Date.now().toString(36).slice(-3);
}
