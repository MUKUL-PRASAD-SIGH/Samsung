"""Records the 6-minute 1920x1080@60fps Kairos demo: Playwright + CDP screencast of a mock desktop that hosts the REAL app.

Run the demo server first (see README.md), then:   python record.py [--only scene,scene]
Everything inside the Kairos window is the real web app talking to the real server and the real LLM; the desktop, the VS Code
window and the phone frame are mock-ups (the phone shows real Compose renders, see android DemoFrames).
"""
import argparse, asyncio, base64, json, os, re, shutil, socket, subprocess, sys, time, wave
import numpy as np
from pathlib import Path
from playwright.async_api import async_playwright

HERE = Path(__file__).parent
REC = Path(os.getenv("KAIROS_REC", "/tmp/kairos-rec")); FRAMES = REC / "frames"; WWW = REC / "www"
BASE = "http://localhost:8200/"; DESK_PORT = 8301; DESK = f"http://localhost:{DESK_PORT}/desktop.html"
TOKEN = os.getenv("AUTH_TOKEN", "athena-kairos-2026"); WRONG = "kairos-demo"
EXPORT_DIR = Path(os.getenv("EXPORT_DIR", REC / "exports"))
OVERLAY = (HERE / "overlay.js").read_text()
MIC_ON_AT = 173.6          # the scene clicks the mic at about this time; clip C (talking over the agent) must land when the agent speaks
TALK_OVER_AT = float(os.getenv("TALK_OVER_AT", "216.0"))
CLICK_C_AT = TALK_OVER_AT - MIC_ON_AT

ap = argparse.ArgumentParser()
ap.add_argument("--only", default="", help="comma list of scene names for a dry run")
args = ap.parse_args()
ONLY = [s for s in args.only.split(",") if s]

# ---------------------------------------------------------------------------------- staging + microphone file
def stage():
    WWW.mkdir(parents=True, exist_ok=True)
    shutil.copy(HERE / "desktop.html", WWW / "desktop.html"); shutil.copy(HERE.parent.parent / "frontend/public/kairos.svg", WWW / "kairos.svg")
    s = socket.socket()
    if s.connect_ex(("127.0.0.1", DESK_PORT)) != 0:
        subprocess.Popen([sys.executable, "-m", "http.server", str(DESK_PORT), "--bind", "127.0.0.1"], cwd=WWW, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        time.sleep(1.0)
    s.close()

def build_mic():
    RATE = 48000
    with wave.open(str(REC / "assets/mic_base.wav")) as w:
        buf = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).copy()
    C = np.load(REC / "assets/C.npy")
    i = int(CLICK_C_AT * RATE); buf[i:i + len(C)] = C
    with wave.open(str(REC / "mic.wav"), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE); w.writeframes(buf.tobytes())
stage(); build_mic()

# ----------------------------------------------------------------------------------------- recorder
class Rec:
    def __init__(self, page, cdp):
        self.page, self.cdp = page, cdp
        self.stamps = []; self.t_start = None; self.offset = 0.0; self.n = 0
    def now(self): return time.perf_counter() - self.t_start + self.offset
    async def at(self, t):
        d = t - self.now()
        if d > 0: await asyncio.sleep(d)
        return d
    def on_frame(self, ev):
        name = f"{self.n:06d}.jpg"; self.n += 1
        (FRAMES / name).write_bytes(base64.b64decode(ev["data"]))
        self.stamps.append((ev["metadata"]["timestamp"], name))
        asyncio.ensure_future(self.cdp.send("Page.screencastFrameAck", {"sessionId": ev["sessionId"]}))
    async def start(self):
        shutil.rmtree(FRAMES, ignore_errors=True); FRAMES.mkdir(parents=True)
        self.cdp.on("Page.screencastFrame", self.on_frame)
        self.t_start = time.perf_counter()
        await self.cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 92, "maxWidth": 1920, "maxHeight": 1080, "everyNthFrame": 1})
    async def stop(self):
        await self.cdp.send("Page.stopScreencast")
        json.dump(self.stamps, open(REC / "stamps.json", "w"))
    async def ev(self, js, *a): return await self.page.evaluate(js, *a)
    async def box(self, loc): return await loc.first.bounding_box()
    async def to_xy(self, x, y, ms=750): await self.ev("([x,y,ms])=>__rec.cursorTo(x,y,ms)", [x, y, ms]); await asyncio.sleep(ms / 1000)
    async def move(self, loc, ms=750, dx=0, dy=0):
        b = await self.box(loc)
        if b: await self.ev("([x,y,ms])=>__rec.cursorTo(x,y,ms)", [b["x"] + b["width"] / 2 + dx, b["y"] + b["height"] / 2 + dy, ms])
        await asyncio.sleep(ms / 1000)
    async def click(self, loc, ms=750):
        await self.move(loc, ms); await self.ev("__rec.ripple()"); await asyncio.sleep(0.12); await loc.first.click(); await asyncio.sleep(0.2)
    async def hl(self, loc, label=""):
        b = await self.box(loc)
        if b: await self.ev("([r,l])=>__rec.hl(r,l)", [{"x": b["x"], "y": b["y"], "w": b["width"], "h": b["height"]}, label])
    async def hl_off(self): await self.ev("__rec.hlOff()")
    async def cap(self, html=""): await self.ev("(h)=>__rec.cap(h)", html)
    async def type(self, loc, text, delay=55):
        await self.click(loc); await loc.first.press_sequentially(text, delay=delay)
    async def send(self, loc, text, delay=55):
        await self.type(loc, text, delay); await asyncio.sleep(0.25); await loc.first.press("Enter")

def real_get(path, token=True):
    import urllib.request
    req = urllib.request.Request("http://localhost:8200" + path, headers={"Authorization": f"Bearer {TOKEN}"} if token else {})
    return urllib.request.urlopen(req, timeout=10).read().decode()

SAFE_IMPORTS = {"math", "sys", "unittest", "typing", "itertools", "functools", "re", "collections", "doctest", "string"}
def safe_to_run(src):
    if re.search(r"\b(open|exec|eval|__import__|input|os|subprocess|socket|shutil|pathlib|requests)\b\s*[(.]", src): return False
    mods = set(re.findall(r"^\s*(?:from|import)\s+([A-Za-z_][\w]*)", src, re.M))
    return mods <= SAFE_IMPORTS

def run_file(path):
    try:
        r = subprocess.run([sys.executable, path.name], cwd=path.parent, capture_output=True, text=True, timeout=10)
        out = (r.stdout + r.stderr).strip()
        return out or "(no output: all assertions passed)"
    except Exception as e:
        return f"(could not run: {e})"

# ------------------------------------------------------------------------------------------- scenes
async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(executable_path="/usr/bin/google-chrome-stable", headless=True, args=[
            "--no-sandbox", "--force-device-scale-factor=1", "--autoplay-policy=no-user-gesture-required",
            "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
            f"--use-file-for-fake-audio-capture={REC}/mic.wav%noloop", f"--use-file-for-fake-video-capture={REC}/assets/poster.y4m",
            "--disable-features=LocalNetworkAccessChecks,PrivateNetworkAccessSendPreflights,PrivateNetworkAccessForWorkers,BlockInsecurePrivateNetworkRequests,LocalNetworkAccessChecksWebSockets",
            "--disable-background-timer-throttling", "--disable-renderer-backgrounding", "--hide-scrollbars",
            "--use-gl=angle", "--use-angle=gl", "--ignore-gpu-blocklist", "--enable-gpu-rasterization"])
        ctx = await b.new_context(viewport={"width": 1920, "height": 1080}, device_scale_factor=1)
        await ctx.add_init_script(OVERLAY)
        async def strip_xfo(route):      # the server forbids framing (X-Frame-Options); the recording desktop embeds it on purpose
            r = await route.fetch()
            await route.fulfill(response=r, headers={k: v for k, v in r.headers.items() if k.lower() != "x-frame-options"})
        await ctx.route("http://localhost:8200/**", strip_xfo)
        page = await ctx.new_page()
        cdp = await ctx.new_cdp_session(page)
        R = Rec(page, cdp)
        F = page.frame_locator("#app")
        scenes = []
        def scene(name, start):
            def deco(fn): scenes.append((name, start, fn)); return fn
            return deco

        INPUT = F.locator('input[placeholder="What would you like to know?"], input[placeholder^="Ask Kairos"]')
        def tab(name): return F.get_by_role("button", name=name, exact=True)
        PANEL_BTN = F.get_by_text("Artifact & Agents", exact=True)
        EPOCH = F.get_by_text("Epoch #", exact=False)
        MIC = F.locator('button[title^="Hands-free voice"], button[title="Stop listening"]')
        SPEAK = F.locator('button[title="Speak replies aloud"], button[title^="Spoken replies on"]')
        CAM = F.locator('button[title^="Share camera"], button[title^="Stop sharing camera"]')

        def appframe(): return next((f for f in page.frames if f.url.startswith(BASE)), None)
        async def appjs(expr, arg=None):
            f = appframe()
            return await f.evaluate(expr, arg) if f else None
        async def ws_mark(): return (await appjs("window.__ws.length")) or 0
        async def wait_ws(pred, since=0, timeout=20):
            """wait until some WebSocket event after index `since` satisfies the JS predicate `x=>...`; returns it or None"""
            t0 = time.time()
            while time.time() - t0 < timeout:
                r = await appjs("([p,s])=>{const f=eval(p);return window.__ws.slice(s).find(f)||null}", [pred, since])
                if r: return r
                await asyncio.sleep(0.25)
            return None
        async def open_panel():
            if await PANEL_BTN.count(): await R.click(PANEL_BTN.first)
            await asyncio.sleep(0.6)
        async def ask(text, delay=45, hold=0.0):
            await R.send(INPUT, text, delay)
            if hold: await asyncio.sleep(hold)

        # 0 -------------------------------------------------------------------------------- title
        @scene("title", 0)
        async def s_title():
            await R.ev("__rec.title('Kairos','ΚΑΙΡΟΣ','The right moment to act: a real-time agent you can interrupt. One click on your desktop, one tap on your phone.',['Epoch-tagged interruption','Cognitive memory','Voice barge-in','Code → VS Code','Vision','Android app'])")
            await R.at(5.6); await R.ev("__rec.titleOff()")

        # 1 -------------------------------------------------------------------------------- desktop → icon → launch → login
        @scene("desktop", 6)
        async def s_desktop():
            await R.cap("A normal Linux desktop. Kairos is installed like any app: <b>one click</b> and it opens")
            icon = page.locator("#ico-kairos img")
            await R.at(7.2); await R.hl(icon, "Kairos"); await R.at(8.4)
            await R.move(icon, 1200); await R.hl_off(); await R.ev("__rec.ripple()"); await asyncio.sleep(0.15)
            await R.cap("Launching… the app starts its own server if it isn't running yet")
            await page.evaluate("__desk.launch(%s)" % json.dumps(BASE))
            await page.evaluate("__desk.maximize()")
            await R.at(17.0)
            await R.ev("__rec.hud(true)")
            await R.cap("The server is protected by an <b>access key</b>: sign in")
            key = F.locator('input[placeholder="Access key"]'); go = F.get_by_role("button", name="Continue")
            await R.hl(key, "access key")
            await R.at(18.6); await R.type(key, WRONG, 70)
            await R.click(go)
            try: await F.get_by_text("not accepted", exact=False).first.wait_for(state="visible", timeout=8000)
            except Exception: pass
            await R.cap("A wrong key is refused, no way in"); await asyncio.sleep(2.2)
            await R.at(24.0)
            await key.first.fill(""); await R.type(key, TOKEN, 60); await R.hl_off()
            await R.at(29.0); await R.cap("The right key stores the token on this device and opens the app")
            await R.click(go)
            try: await INPUT.first.wait_for(state="visible", timeout=10000)
            except Exception: pass
            await R.at(33.5)
            await R.cap("Home: type, speak, or tap a suggestion. <b>Esc</b> interrupts at any moment")
            chips = F.get_by_text("Find flights", exact=True)
            if await chips.count(): await R.hl(chips.first, "")
            await R.at(38.0); await R.hl_off()
            await R.cap("Greek <i>kairos</i> means the <b>right moment to act</b>, which is exactly what interruption is about")
            await R.at(41.0); await R.cap("")

        # 2 -------------------------------------------------------------------------------- flights + panels
        @scene("flights", 42)
        async def s_flights():
            await R.cap("Type a request: the <b>fast path</b> acknowledges in ~100 ms while the planner thinks")
            await R.hl(INPUT, "ask anything"); await asyncio.sleep(0.5)
            m = await ws_mark()
            await ask("Find flights from Delhi to Mumbai", 50); await R.hl_off()
            await wait_ws("x=>x.type==='spoken_response'", m, 16)
            await R.cap("Planner called <b>search_flights</b>; every call is tagged with the session <b>epoch</b>")
            await R.hl(EPOCH.first, "epoch")
            await R.at(52.5); await R.hl_off()
            await open_panel(); await R.cap("Live agent activity: one worker per in-flight tool call")
            await R.at(55.5)
            await R.click(tab("Snapshot")); await R.cap("<b>State snapshot</b>: slots (origin / destination), epoch, in-flight calls")
            await R.at(59.5)
            await R.click(tab("Trace")); await R.cap("<b>Trace timeline</b>: every filler, call, cancel and voice event, schema-validated")
            await R.at(63.5)
            await R.click(tab("Cognitive Graph")); await R.cap("<b>Cognitive graph</b>: turns, entities and artifacts the agent remembers")
            await R.at(67.5); await R.cap("")

        # 3 -------------------------------------------------------------------------------- correction, memory, Esc
        @scene("correction", 68)
        async def s_corr():
            await R.click(tab("Trace"))
            await R.cap("Now interrupt it. Book a flight…")
            m = await ws_mark()
            await ask("Book a flight from Delhi to Mumbai", 40)
            await wait_ws("x=>x.type==='tool_call'", m, 8); await asyncio.sleep(0.4)      # correct it only once the booking call is really in flight
            await R.cap("…and <b>correct it while it is booking</b>")
            await INPUT.first.press_sequentially("No wait, change it to Goa", delay=28); await INPUT.first.press("Enter")
            await asyncio.sleep(0.6)
            await R.hl(EPOCH.first, "epoch bumped")
            await R.cap("The correction bumps the <b>epoch</b> and <b>cancels</b> the in-flight Mumbai call: stale work never completes")
            await wait_ws("x=>x.type==='spoken_response'", m + 1, 16)
            await R.at(81.0); await R.hl_off()
            await R.click(tab("Snapshot")); await R.cap("Snapshot after the correction: destination is <b>Goa</b>, no stale Mumbai value")
            await R.at(86.0)
            await R.cap("Memory carries context: <b>“there”</b> means Goa. Then <b>Esc</b> to stop it")
            m = await ws_mark()
            await ask("Find hotels there for 2 nights", 40, 1.1)
            await INPUT.first.click(); await page.keyboard.press("Escape")
            await R.cap("<b>Esc</b> (or the Stop button) cancels the running search and starts nothing")
            await asyncio.sleep(2.5)
            await R.at(94.0)
            await R.click(tab("Cognitive Graph")); await R.cap("Graph: turns linked <b>NEXT_TURN</b>, and <b>SUPERSEDES</b> where a value was really overridden")
            await R.at(97.5); await R.cap("")

        # 4 -------------------------------------------------------------------------------- code → export → VS Code
        @scene("code", 98)
        async def s_code():
            await R.cap("Beyond travel: ask it to <b>write code</b> and hand it to your editor")
            m = await ws_mark()
            await ask("Write a Python function that checks whether a number is prime, with a few tests. Save it as prime.py and open it in VS Code.", 38)
            await R.at(108.0)
            await R.click(tab("Bots Swarm")); await R.cap("A worker agent writes the code (an <b>artifact</b>) while you watch")
            ex = await wait_ws("x=>x.type==='file_exported'", m, 40)
            if not ex: print("   !! no file_exported event", flush=True)
            await R.cap("The <b>export_artifact</b> tool saved the file, then launched <b>VS Code</b> on it")
            name = (ex or {}).get("filename") or "prime.py"
            await R.at(122.0)
            src = (EXPORT_DIR / name).read_text() if (EXPORT_DIR / name).exists() else "# (export missing)\n"
            print(f"   exported {name}: {len(src)} bytes; code CLI log: {Path(REC/'code_opened.log').read_text().strip().splitlines()[-1:] if (REC/'code_opened.log').exists() else 'none'}", flush=True)
            files = sorted(p.name for p in EXPORT_DIR.iterdir() if p.is_file())
            await R.ev("__rec.hud(false)"); await page.evaluate("__desk.hideApp()")
            lang = {"py": "Python", "js": "JavaScript", "html": "HTML", "md": "Markdown", "json": "JSON", "svg": "SVG"}.get(name.rsplit(".", 1)[-1], "Plain Text")
            await page.evaluate("(s)=>__desk.openEditor(s)", {"dir": "kairos-exports", "files": files, "name": name, "content": src, "language": lang, "terminal": ""})
            await R.cap("VS Code opens on the <b>real exported file</b> (path is returned to the agent, never overwritten)")
            await R.at(132.0)
            if safe_to_run(src):
                await page.evaluate("(c)=>__desk.termType(c)", f"$ python3 {name}"); await asyncio.sleep(0.5)
                out = run_file(EXPORT_DIR / name)
                await page.evaluate("(t)=>{document.getElementById('cw-term').textContent+='\\n'+t}", "\n".join(out.splitlines()[:7])[:500])
                await R.cap("…and it really runs: the output above is from executing the exported file")
            await R.at(142.0)
            await page.evaluate("__desk.closeEditor()"); await page.evaluate("__desk.showApp()"); await R.ev("__rec.hud(true)")
            try:
                await F.get_by_role("button", name=re.compile(r"^Exports")).first.click()
            except Exception: pass
            await R.cap("Every export is listed in <b>Exports</b>: open in VS Code, download, or copy the path")
            await R.at(149.5); await R.cap("")

        # 5 -------------------------------------------------------------------------------- timer + weather (concurrent)
        @scene("timer", 150)
        async def s_timer():
            await R.cap("Timers: a background tool call, <b>cancellable</b> like any other")
            m = await ws_mark()
            await ask("Set a timer for 12 seconds called tea", 45)
            await R.at(158.5)
            await R.cap("While it counts down the agent is free: ask something else")
            await ask("What is the weather in Goa?", 45)
            await wait_ws("x=>x.type==='spoken_response'", m + 1, 14)
            await R.cap("Weather answered while the timer is still running")
            await R.at(166.0)
            await wait_ws("x=>x.type==='spoken_response'", m + 3, 12)
            await R.cap("Timer done: the agent announces it. Set another and <b>cancel</b> it:")
            await R.at(168.4)
            await ask("Set a timer for 60 seconds", 40, 1.0)
            await INPUT.first.click(); await page.keyboard.press("Escape")
            await R.at(172.5); await R.cap("")

        # 6 -------------------------------------------------------------------------------- hands-free voice + barge-in
        @scene("voice", 173)
        async def s_voice():
            await R.cap("Hands-free voice: server-side <b>VAD → Whisper</b> with live partial transcripts")
            await R.hl(MIC, "hands-free mic")
            await R.at(MIC_ON_AT - 1.2)
            await R.click(MIC, 900); await R.hl_off()          # mic on: the fake microphone starts playing here
            await R.cap("Listening. The simulated microphone says: <b>“Book a flight from Delhi to Mumbai”</b>")
            await R.at(MIC_ON_AT + 7.5)
            await R.cap("Barge-in by <b>voice</b>: “Wait, actually make that Mumbai to Goa” arrives mid-booking")
            await R.at(MIC_ON_AT + 14.5)
            await R.click(tab("Trace")); await R.cap("The spoken correction cancelled the booking from a <b>partial</b> transcript, before the sentence even ended")
            await R.at(MIC_ON_AT + 22.0)
            await R.cap("Measured: ack ≈ <b>0.35 s</b> after you stop · cancel ≈ <b>1.0 s</b> after speech onset")
            await R.at(MIC_ON_AT + 30.0); await R.cap("")

        # 7 -------------------------------------------------------------------------------- spoken replies + talk-over
        @scene("speak", 205)
        async def s_speak():
            await R.cap("Spoken replies: <b>Piper</b> TTS, sentence by sentence")
            await R.hl(SPEAK, "speak replies"); await R.at(207.0)
            await R.click(SPEAK); await R.hl_off()
            await R.cap("The agent now talks, and the mic stays open so you can <b>talk over it</b>")
            await R.at(210.0)
            await ask("Find flights from Delhi to Goa", 40)
            await R.at(TALK_OVER_AT - 3.0)
            await R.cap("User speaks over the reply → volume <b>ducks</b>, then speech <b>stops</b>")
            n0 = await appjs("window.__ws.filter(x=>x.type==='speech_state'&&x.state==='stopped').length") or 0
            for _ in range(40):
                if (await appjs("window.__ws.filter(x=>x.type==='speech_state'&&x.state==='stopped').length") or 0) > n0: break
                await asyncio.sleep(0.25)
            heard = await appjs("(()=>{const s=[...window.__ws].reverse().find(x=>x.type==='speech_state'&&x.state==='stopped');return s?{why:s.reason,t:s.spoken}:null})()")
            if heard and heard.get("t"): await R.cap(f"Cut off ({heard['why']}): memory keeps only what you <b>actually heard</b>: “{heard['t']}…”")
            else: await R.cap("Echo-safe full duplex: the agent never mistakes its own voice for you")
            await R.at(TALK_OVER_AT + 9.0)
            await R.click(tab("Trace")); await R.cap("Next turn the model sees <b>[interrupted by the user]</b> instead of the reply it never finished")
            await R.at(233.5); await R.cap("")
            if await MIC.count():          # leave hands-free mode before the next scene
                try: await MIC.first.click()
                except Exception: pass

        # 8 -------------------------------------------------------------------------------- vision
        @scene("vision", 235)
        async def s_vision():
            await R.cap("Vision: share the camera. Frames are <b>buffered, never analyzed</b> unless a question needs them")
            await R.hl(CAM, "camera"); await R.at(237.5)
            await R.click(CAM); await R.hl_off(); await asyncio.sleep(1.2)
            chip = F.get_by_text("Sharing your camera", exact=False)
            if await chip.count(): await R.hl(chip, "privacy indicator")
            await R.at(241.0); await R.hl_off()
            m = await ws_mark()
            await ask("Find flights from Delhi to the city on this poster", 40)
            await R.cap("The planner calls <b>analyze_frame</b> (VLM), reads <b>GOA</b>, and feeds it into <b>search_flights</b>")
            await wait_ws("x=>x.type==='spoken_response'", m, 28)
            await R.at(250.5)
            if await CAM.count():
                try: await CAM.first.click()
                except Exception: pass
            await R.cap("")

        # 9 -------------------------------------------------------------------------------- settings + metrics
        @scene("settings", 252)
        async def s_settings():
            st = F.get_by_text("Settings", exact=True)
            await R.cap("Settings: live engine info from <b>/health</b>")
            await R.click(st.first); await asyncio.sleep(0.9)
            await R.at(256.5)
            done = F.get_by_role("button", name="Done", exact=True)
            if await done.count(): await R.click(done.first)
            await asyncio.sleep(0.4)

        @scene("metrics", 258)
        async def s_metrics():
            await R.cap(""); await R.ev("__rec.hud(false); __rec.termClear(); __rec.termShow(true,'varshith@athens: ~/Samsung')")
            await R.at(259.2)
            await R.ev("([c])=>__rec.termType(c,22)", ["curl -s -H \"Authorization: Bearer $AUTH_TOKEN\" localhost:8200/metrics | grep -E '^agent_(first_ack|interrupt_cancel|llm_requests|speech_stop|rejected)'"])
            m = real_get("/metrics")
            sel = [l for l in m.splitlines() if re.match(r"agent_(first_ack_seconds_(sum|count)|interrupt_cancel_seconds_(sum|count)|llm_requests_total|speech_stop_seconds_count|rejected_total)", l)][:12]
            await R.ev("([t])=>__rec.termLine(\"<span class='hl'>\"+t.replace(/</g,'&lt;')+\"</span>\",400)", ["\n".join(sel)])
            await R.at(264.5)
            await R.ev("([c])=>__rec.termType(c,22)", ["python -m agent.eval --llm mock --virtual --set all --fail-under 97"])
            ev = [l for l in open(REC / "eval_out.txt").read().splitlines() if "Loading" not in l]
            i = next(k for k, l in enumerate(ev) if l.startswith("SUITE"))
            keep = [l for l in ev[i + 3:i + 11] if l.strip() and not l.startswith("Raw measurements")]
            show = [ev[i]] + keep + [l for l in ev if l.startswith("generalization")] + [l for l in ev if l.startswith("GATE")]
            await R.ev("([t])=>__rec.termLine(\"<span class='ok'>\"+t.replace(/</g,'&lt;')+\"</span>\",400)", ["\n".join(show)])
            await R.at(271.0); await R.ev("__rec.termShow(false)")

        # 10 ------------------------------------------------------------------------------- the phone edition
        @scene("phone", 272)
        async def s_phone():
            plan_path = REC / "phone_plan.json"
            if not plan_path.exists(): print("   !! no phone_plan.json (run build_phone_plan.py), skipping phone scene", flush=True); return
            plan = json.loads(plan_path.read_text())
            await R.ev("__rec.hud(false)"); await page.evaluate("__desk.hideApp()")
            await page.evaluate("(a)=>{__desk.phoneList(a,-1);__desk.phoneOn()}", plan["bullets"])
            await R.at(273.4)
            await R.cap("The same agent on Android: a native Kotlin / Jetpack Compose app. Tap the icon")
            tapr = await page.evaluate("__desk.phoneTapRect()")
            await R.to_xy(tapr["x"] + 160, tapr["y"] + 300, 1)
            await R.to_xy(tapr["x"], tapr["y"], 1200)
            await R.ev("__rec.ripple()"); await page.evaluate("([x,y])=>__desk.tapAt(x,y)", [tapr["x"], tapr["y"]])
            await page.evaluate("__desk.phoneLaunch()")
            for s in plan["steps"]:
                await R.at(s["t"])
                if s.get("tap"):
                    sr = await page.evaluate("__desk.scrRect()")
                    x = sr["x"] + s["tap"][0] * sr["w"]; y = sr["y"] + 46 + s["tap"][1] * (sr["h"] - 46)
                    await R.to_xy(x, y, 420); await R.ev("__rec.ripple()"); await page.evaluate("([x,y])=>__desk.tapAt(x,y)", [x, y])
                if s.get("bullet") is not None: await page.evaluate("(i)=>__desk.phoneStep(i)", s["bullet"])
                await page.evaluate("(src)=>__desk.phoneShow(src)", "phone/" + s["img"])
                await R.cap(s.get("cap") or "")
            await R.at(plan["end"])
            await page.evaluate("__desk.phoneOff()"); await R.cap(""); await asyncio.sleep(0.9)

        # 11 ------------------------------------------------------------------------------- closing
        @scene("closing", 340)
        async def s_close():
            await R.ev("__rec.hud(false)")
            await R.ev("__rec.title('Built for interruption','ΚΑΙΡΟΣ','Epoch model · 2-tier cognitive memory · Whisper + VAD · Piper · Vision · Export to VS Code · Android · Eval harness · Docker + CI',['Web','Linux desktop launcher','Android','github.com/V4RSH1TH-R3DDY/Samsung'])")
            await R.at(359.0)

        todo = [s for s in scenes if (not ONLY or s[0] in ONLY)]
        await page.goto(DESK); await asyncio.sleep(0.8)
        first = todo[0][0]
        if ONLY and first not in ("title", "desktop"):
            await page.evaluate("__desk.launch(%s)" % json.dumps(BASE)); await page.evaluate("__desk.maximize()")
            f = page.frame_locator("#app")
            await f.locator('input[placeholder="Access key"]').fill(TOKEN); await f.get_by_role("button", name="Continue").click(); await asyncio.sleep(2.5)
            await page.evaluate("__rec.hud(true)")
            if first in ("correction", "code", "timer", "voice", "speak", "vision", "settings", "metrics"):
                inp = f.locator('input[placeholder="What would you like to know?"]'); await inp.first.fill("Find flights from Delhi to Mumbai"); await inp.first.press("Enter"); await asyncio.sleep(6)
                await f.get_by_text("Artifact & Agents", exact=True).first.click(); await asyncio.sleep(0.8)
        t0_scene = todo[0][1] if ONLY else 0.0
        await page.evaluate(f"localStorage.setItem('__rec_t0', String(Date.now() - {t0_scene}*1000))")
        await R.start(); R.offset = t0_scene
        for name, start, fn in todo:
            if not ONLY: await R.at(start)
            print(f"[{R.now():6.1f}s] scene {name}", flush=True)
            try: await fn()
            except Exception as e: print(f"   !! scene {name} failed: {type(e).__name__}: {str(e)[:300]}", flush=True)
        await asyncio.sleep(0.4)
        # logs for the audio track, the phone replay and the report
        f = appframe()
        if f:
            evs = await f.evaluate("window.__ws"); raw = await f.evaluate("window.__wsraw"); audio = await f.evaluate("window.__audio||[]"); mic = await f.evaluate("window.__micStart||null")
            json.dump({"wsraw": raw}, open(REC / "wsraw.json", "w")); json.dump({"audio": audio, "micStart": mic, "ws": evs}, open(REC / "audio.json", "w"))
        await R.stop()
        ts0 = R.stamps[0][0]
        json.dump({"ts0": ts0}, open(REC / "meta.json", "w"))
        if f:
            with open(REC / "events.txt", "w") as fh:     # times on the video timeline (+ the scene offset of a --only run)
                for e in evs: fh.write(f"{e['t']/1000 - ts0 + R.offset:7.2f}s {e['type']:16s} {e.get('state') or ''} {e.get('reason') or ''} {(e.get('spoken') or e.get('text') or e.get('filename') or '')[:60]}\n")
        print(f"frames captured: {len(R.stamps)}; duration {R.stamps[-1][0]-R.stamps[0][0]:.1f}s", flush=True)
        await b.close()

asyncio.run(main())
