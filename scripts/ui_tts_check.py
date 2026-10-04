"""Browser check for spoken replies (needs the backend on :8000 with TTS, the frontend on :5173, and
`pip install playwright` somewhere -- it drives the system Chrome, no browser download).

Turns the speaker button on, asks for a reply, checks audio was scheduled in the page, then interrupts by typing while
the agent is talking and reports how fast the server stopped the speech and that the page really stopped its sources.
Run:  python scripts/ui_tts_check.py
"""
import asyncio
from playwright.async_api import async_playwright

INSTR = """
window.__audio = {started: [], stopped: 0, gain: []};
const S = AudioBufferSourceNode.prototype;
const origStart = S.start, origStop = S.stop;
S.start = function(when){ window.__audio.started.push({t: performance.now(), dur: this.buffer ? this.buffer.duration : 0}); return origStart.apply(this, arguments); };
S.stop = function(){ window.__audio.stopped++; return origStop.apply(this, arguments); };
window.__ws = [];
const OW = window.WebSocket;
window.WebSocket = function(...a){ const w = new OW(...a); w.addEventListener('message', e => { try { const d = JSON.parse(e.data); window.__ws.push({t: performance.now(), type: d.action_type || d.type, state: d.state, reason: d.reason}); } catch(_){} }); return w; };
window.WebSocket.prototype = OW.prototype; Object.assign(window.WebSocket, OW);
"""

async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(executable_path="/usr/bin/google-chrome-stable", headless=True,
            args=["--autoplay-policy=no-user-gesture-required", "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream", "--no-sandbox"])
        page = await b.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        await page.add_init_script(INSTR)
        await page.goto("http://localhost:5173")
        await page.wait_for_timeout(2500)
        btn = page.locator('button[title="Speak replies aloud"]')
        print("speaker button visible:", await btn.count() > 0)
        await btn.first.click()
        await page.wait_for_timeout(500)
        print("after click title:", await page.locator('button[title^="Spoken replies on"]').count() > 0)

        box = page.locator('input[placeholder="What would you like to know?"], input[placeholder^="Ask Flippy"], textarea[placeholder^="Ask Flippy"], textarea[placeholder="What would you like to know?"]')
        await box.first.fill("What's the weather like in Paris?")
        await box.first.press("Enter")
        await page.wait_for_timeout(1500)
        print("PAGE TEXT:", (await page.inner_text("body"))[:500].replace("\n"," | "))
        print("ws log:", await page.evaluate("window.__ws.length"))
        # (timing is reported from the page clock below)
        for _ in range(60):
            await page.wait_for_timeout(250)
            n = await page.evaluate("window.__audio.started.length")
            if n >= 2: break
        a = await page.evaluate("window.__audio")
        print("sources scheduled after a normal reply:", len(a["started"]), "total seconds of audio:", round(sum(x["dur"] for x in a["started"]), 2))
        ws = await page.evaluate("window.__ws.map(x => x.type + (x.state ? ':' + x.state : ''))")
        print("action stream:", [x for x in ws if x.startswith(('audio_out','speech_state','filler','spoken'))][:10])
        await page.wait_for_timeout(7000)   # let it finish

        # second turn, then a typed interruption while it is speaking
        before = await page.evaluate("window.__audio.stopped")
        await box.first.fill("Find flights from Delhi to Mumbai")
        await box.first.press("Enter")
        for _ in range(80):
            await page.wait_for_timeout(250)
            ws = await page.evaluate("window.__ws.filter(x => x.type==='audio_out').length")
            if ws >= 3: break
        t_int = await page.evaluate("performance.now()")
        await box.first.fill("No wait, make it Goa instead")
        await box.first.press("Enter")
        await page.wait_for_timeout(1500)
        after = await page.evaluate("window.__audio.stopped")
        stops = await page.evaluate("window.__ws.filter(x => x.type==='speech_state' && x.state==='stopped').map(x => ({reason: x.reason, dt: Math.round(x.t - %s)}))" % t_int)
        print("sources stopped by the page during the interruption:", after - before, "| server stop events:", stops)
        print("js errors:", errors[:3])
        await b.close()
asyncio.run(main())
