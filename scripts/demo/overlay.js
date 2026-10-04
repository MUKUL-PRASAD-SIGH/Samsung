// Recording overlay (injected into every frame). In the TOP frame it draws the cursor, click ripples, highlight rings, captions, the
// title cards, the live event stream and a small terminal. In the app IFRAME it only taps the WebSocket and forwards what it sees.
(() => {
  const isTop = window.top === window;
  const T0 = '__rec_t0';
  // ---- WebSocket tap (both frames): keeps the raw frames for the phone replay and the audio track, and forwards a summary upward.
  if (!window.__ws) {
    window.__ws = [];          // summaries for the driver
    window.__wsraw = [];       // {t, dir, data} raw text frames (audio payloads are stripped to a length)
    const OW = window.WebSocket;
    const strip = (d) => { try { const j = JSON.parse(d); if (j.audio_b64) { j.audio_len = j.audio_b64.length; j.audio_b64 = ''; } return JSON.stringify(j); } catch (_) { return d; } };
    window.WebSocket = function (...a) {
      const w = new OW(...a);
      const send = w.send.bind(w);
      w.send = (d) => { if (typeof d === 'string') window.__wsraw.push({ t: performance.now() + performance.timeOrigin, dir: 'out', data: d }); return send(d); };
      w.addEventListener('message', (e) => {
        try {
          const raw = e.data; const d = JSON.parse(raw); const t = d.action_type || d.type;
          const now = performance.now() + performance.timeOrigin;
          if (t === 'audio_out') window.__audio = (window.__audio || []).concat([{ t: now, rate: d.sample_rate, b64: d.audio_b64, utt: d.utterance_id, seq: d.seq }]);
          window.__wsraw.push({ t: now, dir: 'in', data: strip(raw) });
          window.__ws.push({ t: now, type: t, state: d.state, reason: d.reason, epoch: d.epoch, text: d.text, spoken: d.spoken_text, tool: d.tool_name, filename: d.filename });
          window.parent.postMessage({ __recEvent: { type: t, d: { tool_name: d.tool_name, reason: d.reason, epoch: d.epoch, state: d.state, text: d.text, is_partial: d.is_partial, nodes: (d.nodes || []).length, filename: d.filename, seq: d.seq } } }, '*');
        } catch (_) {}
      });
      return w;
    };
    window.WebSocket.prototype = OW.prototype; Object.assign(window.WebSocket, OW);
    // when the page asks for the microphone, note when (the fake-microphone file starts playing then)
    const gum = navigator.mediaDevices && navigator.mediaDevices.getUserMedia ? navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices) : null;
    if (gum) navigator.mediaDevices.getUserMedia = async (c) => { const s = await gum(c); if (c && c.audio) window.__micStart = performance.now() + performance.timeOrigin; return s; };
  }
  if (!isTop) return;

  const boot = () => {
    if (window.__rec) return;
    if (!localStorage.getItem(T0)) localStorage.setItem(T0, String(Date.now()));
    const css = `
    #rec-root{position:fixed;inset:0;pointer-events:none;z-index:2147483000;font-family:Inter,ui-sans-serif,system-ui,sans-serif}
    #rec-progress{position:fixed;left:0;top:0;height:3px;width:100%;background:linear-gradient(90deg,#fde68a,#d4a017,#38bdf8);transform-origin:0 50%;transform:scaleX(0);z-index:2147483600;box-shadow:0 0 10px #d4a017}
    #rec-cursor{position:fixed;left:0;top:0;width:28px;height:28px;z-index:2147483500;transform:translate(-100px,-100px);transition:transform 750ms cubic-bezier(.22,.8,.28,1);filter:drop-shadow(0 3px 5px rgba(0,0,0,.6))}
    .rec-ripple{position:fixed;width:14px;height:14px;margin:-7px 0 0 -7px;border-radius:50%;border:3px solid #fbbf24;z-index:2147483400;animation:rec-rip 650ms ease-out forwards}
    @keyframes rec-rip{from{transform:scale(.4);opacity:1}to{transform:scale(5);opacity:0}}
    #rec-hl{position:fixed;left:0;top:0;width:0;height:0;border-radius:12px;opacity:0;z-index:2147483300;
      transition:left 520ms cubic-bezier(.22,.8,.28,1),top 520ms cubic-bezier(.22,.8,.28,1),width 520ms cubic-bezier(.22,.8,.28,1),height 520ms cubic-bezier(.22,.8,.28,1),opacity 300ms;
      box-shadow:0 0 0 3px #fbbf24,0 0 28px 8px rgba(251,191,36,.55);animation:rec-pulse 1.6s ease-in-out infinite}
    @keyframes rec-pulse{0%,100%{filter:brightness(1)}50%{filter:brightness(1.45)}}
    #rec-hl-label{position:fixed;z-index:2147483310;padding:6px 12px;border-radius:8px;background:#fbbf24;color:#111;font:700 15px/1 Inter,system-ui,sans-serif;opacity:0;transition:opacity 300ms,left 520ms cubic-bezier(.22,.8,.28,1),top 520ms cubic-bezier(.22,.8,.28,1);white-space:nowrap;box-shadow:0 6px 18px rgba(0,0,0,.45)}
    #rec-cap{position:fixed;left:50%;bottom:34px;transform:translate(-50%,14px);opacity:0;padding:12px 26px;border-radius:999px;background:rgba(10,14,26,.88);backdrop-filter:blur(8px);
      border:1px solid rgba(212,160,23,.35);color:#e2e8f0;font:600 22px/1.25 Inter,system-ui,sans-serif;transition:opacity 380ms,transform 380ms cubic-bezier(.22,.8,.28,1);box-shadow:0 10px 30px rgba(0,0,0,.5);z-index:2147483200;max-width:1400px;text-align:center}
    #rec-cap.on{opacity:1;transform:translate(-50%,0)}
    #rec-cap b{color:#fbbf24}
    #rec-hud{position:fixed;left:14px;top:420px;width:236px;height:560px;overflow:hidden;z-index:2147482900;display:flex;flex-direction:column;justify-content:flex-end;gap:3px;
      font:500 12px/1.3 ui-monospace,SFMono-Regular,Menlo,monospace;opacity:0;transition:opacity 500ms}
    #rec-hud.on{opacity:1}
    #rec-hud .hh{color:#64748b;font-size:11px;letter-spacing:.14em;text-transform:uppercase;margin-bottom:4px}
    #rec-hud .row{padding:4px 7px;border-radius:6px;background:rgba(15,23,42,.9);border-left:3px solid #64748b;color:#cbd5e1;animation:rec-in 320ms cubic-bezier(.22,.8,.28,1);word-break:break-word}
    @keyframes rec-in{from{transform:translateX(-14px);opacity:0}to{transform:none;opacity:1}}
    #rec-title{position:fixed;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:20px;opacity:0;transition:opacity 900ms ease;z-index:2147483450;
      background:radial-gradient(1100px 650px at 50% 38%,#2a2210 0%,#0b0e14 58%,#05070c 100%);color:#fff;text-align:center}
    #rec-title.on{opacity:1}
    #rec-title img{width:120px;height:120px}
    #rec-title h1{margin:0;font:600 120px/1 "Cormorant Garamond",Georgia,serif;letter-spacing:.2em;background:linear-gradient(90deg,#fde68a,#d4a017);-webkit-background-clip:text;background-clip:text;color:transparent;transform:translateY(24px);transition:transform 1100ms cubic-bezier(.22,.8,.28,1)}
    #rec-title.on h1{transform:none}
    #rec-title .gr{font:500 34px "Cormorant Garamond",Georgia,serif;letter-spacing:.5em;color:rgba(253,230,138,.65);margin-top:-6px}
    #rec-title h2{margin:6px 0 0;font:500 28px/1.35 Inter,system-ui,sans-serif;color:#cbd5e1;max-width:1250px}
    #rec-title .chips{display:flex;gap:12px;margin-top:14px;flex-wrap:wrap;justify-content:center}
    #rec-title .chip{padding:8px 16px;border-radius:999px;border:1px solid rgba(212,160,23,.4);color:#e2e8f0;font:600 20px Inter,system-ui,sans-serif;background:rgba(30,41,59,.55)}
    #rec-term{position:fixed;inset:0;background:#0b0f19;z-index:2147483050;opacity:0;transition:opacity 600ms;display:flex;align-items:center;justify-content:center}
    #rec-term.on{opacity:1}
    #rec-term .tw{width:1560px;height:860px;border-radius:16px;background:#0d1117;border:1px solid #30363d;box-shadow:0 30px 90px rgba(0,0,0,.7);overflow:hidden;display:flex;flex-direction:column}
    #rec-term .bar{height:44px;background:#161b22;display:flex;align-items:center;gap:9px;padding:0 16px;border-bottom:1px solid #30363d;color:#8b949e;font:500 15px ui-monospace,monospace}
    #rec-term .dot{width:13px;height:13px;border-radius:50%}
    #rec-term .body{flex:1;padding:22px 28px;font:500 21px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;color:#c9d1d9;overflow:hidden;white-space:pre-wrap}
    #rec-term .ps{color:#7ee787} #rec-term .dim{color:#6e7681} #rec-term .ok{color:#3fb950} #rec-term .hl{color:#79c0ff}
    #rec-term .cur{display:inline-block;width:11px;height:22px;background:#c9d1d9;vertical-align:-4px;animation:rec-blink 1s steps(1) infinite}
    @keyframes rec-blink{50%{opacity:0}}
    `;
    const style = document.createElement('style'); style.textContent = css; document.documentElement.appendChild(style);
    const root = document.createElement('div'); root.id = 'rec-root';
    root.innerHTML = `
      <div id="rec-progress"></div>
      <svg id="rec-cursor" viewBox="0 0 28 28"><path d="M4 2l17 10.5-7.4 2.1 4.6 8.4-3.5 1.9-4.6-8.4L4 22z" fill="#fff" stroke="#0f172a" stroke-width="1.6" stroke-linejoin="round"/></svg>
      <div id="rec-hl"></div><div id="rec-hl-label"></div>
      <div id="rec-cap"></div>
      <div id="rec-hud"><div class="hh">live event stream</div></div>
      <div id="rec-term"><div class="tw"><div class="bar"><span class="dot" style="background:#ff5f56"></span><span class="dot" style="background:#ffbd2e"></span><span class="dot" style="background:#27c93f"></span><span style="margin-left:14px" id="rec-term-title">kairos@athens: ~</span></div><div class="body" id="rec-term-body"></div></div></div>
      <div id="rec-title"><img src="" alt=""><h1></h1><div class="gr"></div><h2></h2><div class="chips"></div></div>`;
    document.documentElement.appendChild(root);
    const $ = (s) => root.querySelector(s);
    const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
    const cursor = $('#rec-cursor'); let cx = -100, cy = -100;
    const prog = $('#rec-progress'); const total = +localStorage.getItem('__rec_total') || 360000;
    // continuous progress bar: also guarantees the compositor produces a frame every vsync (steady 60 fps capture)
    (function tick() { const t0 = +localStorage.getItem(T0); prog.style.transform = `scaleX(${Math.min(1, (Date.now() - t0) / total)})`; requestAnimationFrame(tick); })();
    const colors = { filler: '#38bdf8', tool_call: '#fbbf24', tool_cancel: '#f43f5e', state_snapshot: '#34d399', spoken_response: '#a78bfa', graph_update: '#2dd4bf',
      speech_state: '#f472b6', audio_out: '#f472b6', voice_activity: '#fb923c', transcript: '#fb923c', agent_step: '#818cf8', clarification: '#a78bfa', file_exported: '#fde68a' };
    const api = {
      cursorTo(x, y, ms = 750) { cursor.style.transitionDuration = ms + 'ms'; cursor.style.transform = `translate(${x - 4}px,${y - 2}px)`; cx = x; cy = y; return sleep(ms); },
      ripple() { const r = document.createElement('div'); r.className = 'rec-ripple'; r.style.left = cx + 'px'; r.style.top = cy + 'px'; root.appendChild(r); setTimeout(() => r.remove(), 700); },
      hl(rect, label) {
        const h = $('#rec-hl'), l = $('#rec-hl-label'); const p = 8;
        h.style.left = rect.x - p + 'px'; h.style.top = rect.y - p + 'px'; h.style.width = rect.w + p * 2 + 'px'; h.style.height = rect.h + p * 2 + 'px'; h.style.opacity = 1;
        if (label) { l.textContent = label; const top = rect.y - p - 38; l.style.left = Math.max(10, rect.x - p) + 'px'; l.style.top = (top < 40 ? rect.y + rect.h + p + 8 : top) + 'px'; l.style.opacity = 1; } else l.style.opacity = 0;
      },
      hlOff() { $('#rec-hl').style.opacity = 0; $('#rec-hl-label').style.opacity = 0; },
      cap(html) { const c = $('#rec-cap'); if (!html) { c.classList.remove('on'); return; } c.innerHTML = html; c.classList.add('on'); },
      hud(on) { $('#rec-hud').classList.toggle('on', on); },
      hudAdd(text, color) {
        const hud = $('#rec-hud'); const row = document.createElement('div'); row.className = 'row'; row.style.borderLeftColor = color || '#64748b'; row.textContent = text;
        hud.appendChild(row); while (hud.querySelectorAll('.row').length > 22) hud.querySelector('.row').remove();
      },
      title(h1, gr, h2, chips) { const t = $('#rec-title'); t.querySelector('img').src = window.__kairosLogo || 'kairos.svg';
        t.querySelector('h1').textContent = h1; t.querySelector('.gr').textContent = gr || ''; t.querySelector('h2').textContent = h2 || '';
        t.querySelector('.chips').innerHTML = (chips || []).map((c) => `<span class="chip">${c}</span>`).join(''); t.classList.add('on'); },
      titleOff() { $('#rec-title').classList.remove('on'); },
      termShow(on, title) { if (title) $('#rec-term-title').textContent = title; $('#rec-term').classList.toggle('on', on); },
      termClear() { $('#rec-term-body').innerHTML = ''; },
      async termLine(html, ms = 0) { const b = $('#rec-term-body'); const d = document.createElement('div'); d.innerHTML = html; b.appendChild(d); if (ms) await sleep(ms); },
      async termType(cmd, perChar = 38, prompt = '<span class="ps">$</span> ') {
        const b = $('#rec-term-body'); const d = document.createElement('div'); d.innerHTML = prompt + '<span class="t"></span><span class="cur"></span>'; b.appendChild(d);
        const t = d.querySelector('.t');
        for (const ch of cmd) { t.textContent += ch; await sleep(perChar * (0.6 + Math.random() * 0.9)); }
        await sleep(380); d.querySelector('.cur').remove();
      },
      rect(sel) { const e = document.querySelector(sel); if (!e) return null; const r = e.getBoundingClientRect(); return { x: r.x, y: r.y, w: r.width, h: r.height }; },
    };
    window.__rec = api;
    // the app iframe forwards its WebSocket events: show them in the live stream
    window.addEventListener('message', (m) => {
      const e = m.data && m.data.__recEvent; if (!e) return; const t = e.type, d = e.d;
      if (t === 'audio_out' && (d.seq || 0) > 0) return;
      if (t === 'transcript' && d.is_partial) return;
      let line = t;
      if (t === 'tool_call') line = `tool_call ${d.tool_name}`; else if (t === 'tool_cancel') line = `tool_cancel ${d.tool_name} (${(d.reason || '').slice(0, 22)})`;
      else if (t === 'state_snapshot') line = `snapshot · epoch ${d.epoch}`; else if (t === 'speech_state') line = `speech ${d.state}${d.reason ? ' (' + d.reason + ')' : ''}`;
      else if (t === 'voice_activity') line = `voice ${d.state}`; else if (t === 'transcript') line = `FINAL: ${(d.text || '').slice(0, 30)}`;
      else if (t === 'filler') line = `ack: ${(d.text || '').slice(0, 30)}`; else if (t === 'spoken_response') line = 'reply ready'; else if (t === 'graph_update') line = `graph +${d.nodes} nodes`;
      else if (t === 'audio_out') line = 'audio ▶ sentence'; else if (t === 'file_exported') line = `exported ${d.filename}`; else if (t === 'agent_step') line = 'worker step';
      api.hudAdd(line, colors[t]);
    });
  };
  if (document.documentElement) boot();
  else { const mo = new MutationObserver(() => { if (document.documentElement) { mo.disconnect(); boot(); } }); mo.observe(document, { childList: true }); }
})();
