#!/usr/bin/env python3
"""Papermorph GUI: a small local web page for starting a book with the agent of your choice.

    python3 papermorph_gui.py            # then open the address it prints (it opens your browser too)
    python3 papermorph_gui.py 8790       # a different port

Put this file in the repository root, next to site/ and .claude/. It uses only the Python standard library.

On the page you choose the source PDF, your own pictures, the model, and the agent (OpenCode or any
command you type). Start writes the task to books/<book>/prompt.md, runs the agent with a short message
that points at that file, and shows the agent's output live. Nothing is sent anywhere by this program:
the agent you choose decides what goes online.

The server only answers on 127.0.0.1, checks the Host and Origin headers, and requires a random token
on every call, so another web page open in your browser cannot make it start commands.
"""
import http.server, json, os, re, secrets, shlex, shutil, signal, socketserver, subprocess, sys
import threading, time, urllib.request, webbrowser
from pathlib import Path
from urllib.parse import urlparse, parse_qs

ROOT = Path(__file__).resolve().parent
PORT = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 8780
PREVIEW_PORT = 8765
TOKEN = secrets.token_urlsafe(24)
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"}
MAX_PDF = 1 << 30      # 1 GB
MAX_IMAGE = 100 << 20  # 100 MB
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")

OPENCODE = "opencode run --model {model} --dir {root} {auto} {message}"
MESSAGE = "Read the file {prompt_file} and carry out the task it describes, following its instructions exactly."
TYPES = {
    "lesson": "an animated lesson book from the PDF",
    "picture": "an animated picture book",
    "documentary": "a narrated documentary",
    "shorts": "upright shorts",
}


class Run:
    """The one agent process the GUI manages, and its output."""
    def __init__(self):
        self.lock = threading.Lock()
        self.proc = None
        self.lines = []
        self.code = None
        self.stopped = False
        self.slug = None
        self.command = ""

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def add(self, text):
        with self.lock:
            self.lines.append(text)
            if len(self.lines) > 20000:
                del self.lines[:5000]

    def pump(self, proc):
        for raw in proc.stdout:
            self.add(ANSI.sub("", raw.rstrip("\r\n")))
        proc.wait()
        self.code = proc.returncode
        self.add(f"--- finished with exit code {proc.returncode} ---")

    def stop(self):
        p = self.proc
        if not p or p.poll() is not None:
            return False
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True)
            else:
                os.killpg(os.getpgid(p.pid), signal.SIGTERM)
        except Exception as e:  # say what failed instead of pretending it stopped
            self.add(f"--- could not stop the agent: {e} ---")
            return False
        self.stopped = True
        self.add("--- stopped by you ---")
        return True


RUN = Run()
PREVIEW = {"proc": None}


def sane_name(name):
    name = os.path.basename(name.replace("\\", "/"))
    name = re.sub(r"[^A-Za-z0-9._ -]", "_", name).strip(" .")
    return name or "file"


def ollama_models():
    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=1.5) as r:
            return [m["name"] for m in json.load(r).get("models", [])]
    except Exception:
        return None  # not running, or not installed


def book_files(slug):
    d = ROOT / "books" / slug
    pdf = d / "book.pdf"
    pics = sorted(p.name for p in (d / "pictures").glob("*") if p.is_file()) if (d / "pictures").is_dir() else []
    return pdf.is_file(), pics


def build_prompt(f):
    slug, kind = f["slug"], f["type"]
    has_pdf, pics = book_files(slug)
    out = [
        "Read .claude/skills/papermorph/SKILL.md in this repository and follow it. "
        '"SKILL" in that file means the folder .claude/skills/papermorph. '
        "Work from the repository root, the folder that holds site/.",
        "",
        f"Task: make {TYPES.get(kind, kind)}. Book kind: {kind}. Book slug: {slug}.",
    ]
    if f.get("request", "").strip():
        out += ["What to make: " + f["request"].strip()]
    if has_pdf:
        out += [f"Source PDF: books/{slug}/book.pdf"]
    if pics:
        shown = ", ".join(pics[:30]) + (f" and {len(pics) - 30} more" if len(pics) > 30 else "")
        out += [
            f"Pictures supplied by the user: books/{slug}/pictures/ ({len(pics)} files: {shown}). "
            f"Use them wherever the book needs pictures. A published book can only use files under its own "
            f"site folder, so copy the ones you use into site/{slug}/img/."
        ]
    out += [
        "",
        "This is a hands-off run: nobody can answer questions while you work. The choices the skill's intake "
        "would ask about are answered by this file. For anything it does not settle, take the skill's recommended "
        "option and write your choices into the book's style file as the skill describes. "
        "Run slow commands in the background and wait for them. If you cannot view images, say so in your final message.",
    ]
    if f.get("extra", "").strip():
        out += ["", "Extra instructions from the user:", f["extra"].strip()]
    out += [
        "",
        f"When you finish, list the files you made. The book can be previewed at http://localhost:{PREVIEW_PORT}/{slug}/ "
        "once the preview server is running.",
    ]
    return "\n".join(out) + "\n"


def build_command(f, prompt_file):
    agent = f["agent"]
    template = OPENCODE if agent == "opencode" else f.get("custom", "").strip()
    if not template:
        raise ValueError("The custom command is empty.")
    toks = shlex.split(template, posix=(os.name != "nt"))
    if os.name == "nt":
        toks = [t[1:-1] if len(t) > 1 and t[0] == t[-1] and t[0] in "\"'" else t for t in toks]
    values = {
        "model": f.get("model", "").strip(),
        "root": str(ROOT),
        "prompt_file": str(prompt_file),
        "message": MESSAGE.format(prompt_file=prompt_file),
        "auto": "--auto" if f.get("auto") else "",
    }
    if agent == "opencode" and not values["model"]:
        toks = [t for t in toks if t not in ("--model", "{model}")]
    argv = []
    for t in toks:
        if t == "{auto}":
            if values["auto"]:
                argv.append(values["auto"])
            continue
        try:
            argv.append(t.format(**values))
        except (KeyError, IndexError) as e:
            raise ValueError(f"Unknown placeholder {e} in the command. Use {{model}}, {{message}}, {{prompt_file}}, {{root}}.")
    exe = shutil.which(argv[0])
    if not exe:
        hint = " Install OpenCode from https://opencode.ai, or use the custom command box." if argv[0] == "opencode" else ""
        raise ValueError(f"Cannot find the program '{argv[0]}' on your PATH.{hint}")
    argv[0] = exe
    return argv


def start_run(f):
    if RUN.running():
        raise ValueError("An agent run is already in progress. Stop it first.")
    slug = f.get("slug", "")
    if not SLUG_RE.match(slug):
        raise ValueError("The book name must be lowercase letters, digits and hyphens, e.g. my-physics.")
    if f.get("type") not in TYPES:
        raise ValueError("Unknown book kind.")
    if not (ROOT / ".claude" / "skills" / "papermorph" / "SKILL.md").is_file():
        raise ValueError("This file must sit in the Papermorph repository root (next to .claude/ and site/).")
    book = ROOT / "books" / slug
    book.mkdir(parents=True, exist_ok=True)
    prompt_file = book / "prompt.md"
    prompt_file.write_text(build_prompt(f), encoding="utf-8")
    argv = build_command(f, prompt_file)
    env = dict(os.environ, PYTHONUNBUFFERED="1", NO_COLOR="1", TERM="dumb")
    kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    proc = subprocess.Popen(argv, cwd=ROOT, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", bufsize=1, **kw)
    with RUN.lock:
        RUN.lines, RUN.code, RUN.proc, RUN.slug, RUN.stopped = [], None, proc, slug, False
        RUN.command = " ".join(shlex.quote(a) for a in argv)
    RUN.add("$ " + RUN.command)
    threading.Thread(target=RUN.pump, args=(proc,), daemon=True).start()
    return RUN.command


def start_preview():
    p = PREVIEW["proc"]
    if p and p.poll() is None:
        return "already running"
    serve = ROOT / ".claude" / "skills" / "papermorph" / "scripts" / "serve.py"
    if not serve.is_file() or not (ROOT / "site").is_dir():
        raise ValueError("serve.py or site/ not found; is this file in the repository root?")
    kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    PREVIEW["proc"] = subprocess.Popen([sys.executable, str(serve), str(PREVIEW_PORT), "site"], cwd=ROOT,
                                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)
    time.sleep(0.8)
    if PREVIEW["proc"].poll() is not None:
        raise ValueError(f"The preview server exited at once (is port {PREVIEW_PORT} already in use? "
                         f"If you started serve.py yourself, just use http://localhost:{PREVIEW_PORT}/).")
    return "started"


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "PapermorphGUI"

    def log_message(self, *a):
        pass

    def reply(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else (body if isinstance(body, str) else json.dumps(body)).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def trusted(self):
        host = (self.headers.get("Host") or "").lower()
        if host not in (f"127.0.0.1:{PORT}", f"localhost:{PORT}"):
            return False
        origin = self.headers.get("Origin")
        return origin is None or origin.lower() in (f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}")

    def guard(self, need_token=True):
        if not self.trusted():
            self.reply(403, {"error": "Forbidden host or origin."})
            return False
        if need_token and self.headers.get("X-Token") != TOKEN:
            self.reply(403, {"error": "Bad token. Reload the page."})
            return False
        return True

    def body_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":
            if not self.guard(need_token=False):
                return
            return self.reply(200, PAGE.replace("__TOKEN__", TOKEN), "text/html")
        if not self.guard():
            return
        if u.path == "/api/info":
            models = ollama_models()
            return self.reply(200, {
                "root": str(ROOT),
                "skill": (ROOT / ".claude" / "skills" / "papermorph" / "SKILL.md").is_file(),
                "ollama": models,
                "running": RUN.running(),
                "types": TYPES,
                "opencode": OPENCODE,
                "windows": os.name == "nt",
                "preview_port": PREVIEW_PORT,
            })
        if u.path == "/api/log":
            since = int(parse_qs(u.query).get("since", ["0"])[0])
            with RUN.lock:
                lines = RUN.lines[since:]
                nxt = since + len(lines)
            return self.reply(200, {"lines": lines, "next": nxt, "running": RUN.running(), "code": RUN.code,
                                    "stopped": RUN.stopped, "slug": RUN.slug})
        if u.path == "/api/prompt":
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            q.setdefault("type", "lesson")
            if not SLUG_RE.match(q.get("slug", "")):
                return self.reply(200, {"prompt": "(enter a valid book name to see the prompt)"})
            return self.reply(200, {"prompt": build_prompt(q)})
        self.reply(404, {"error": "Not found."})

    def do_PUT(self):
        if not self.guard():
            return
        u = urlparse(self.path)
        if u.path != "/api/upload":
            return self.reply(404, {"error": "Not found."})
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        slug, kind, name = q.get("slug", ""), q.get("kind", ""), sane_name(q.get("name", ""))
        if not SLUG_RE.match(slug):
            return self.reply(400, {"error": "Bad book name."})
        ext = os.path.splitext(name)[1].lower()
        if kind == "pdf":
            if ext != ".pdf":
                return self.reply(400, {"error": "That is not a PDF."})
            dest, limit = ROOT / "books" / slug / "book.pdf", MAX_PDF
        elif kind == "image":
            if ext not in IMAGE_EXT:
                return self.reply(400, {"error": f"{name}: not a picture type ({', '.join(sorted(IMAGE_EXT))})."})
            dest, limit = ROOT / "books" / slug / "pictures" / name, MAX_IMAGE
        else:
            return self.reply(400, {"error": "Unknown upload kind."})
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
            return self.reply(413, {"error": f"{name} is larger than {limit >> 20} MB."})
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        left = n
        with open(tmp, "wb") as fh:
            while left:
                chunk = self.rfile.read(min(1 << 20, left))
                if not chunk:
                    break
                fh.write(chunk)
                left -= len(chunk)
        if left:
            tmp.unlink(missing_ok=True)
            return self.reply(400, {"error": "Upload was cut off."})
        os.replace(tmp, dest)
        self.reply(200, {"ok": True, "saved": str(dest.relative_to(ROOT)), "bytes": n})

    def do_POST(self):
        if not self.guard():
            return
        try:
            f = self.body_json()
            if self.path == "/api/run":
                return self.reply(200, {"ok": True, "command": start_run(f)})
            if self.path == "/api/stop":
                return self.reply(200, {"ok": RUN.stop()})
            if self.path == "/api/preview":
                return self.reply(200, {"ok": True, "status": start_preview()})
            if self.path == "/api/rename":
                old, new = f.get("old", ""), f.get("new", "")
                if not (SLUG_RE.match(old) and SLUG_RE.match(new)):
                    raise ValueError("The book name must be lowercase letters, digits and hyphens, e.g. my-physics.")
                if RUN.running():
                    raise ValueError("Stop the running agent before renaming the book.")
                src, dst = ROOT / "books" / old, ROOT / "books" / new
                if src.is_dir() and old != new:
                    if dst.exists() and any(dst.iterdir()):
                        raise ValueError(f"books/{new}/ already has files. Choose another name.")
                    if dst.exists():
                        dst.rmdir()
                    os.replace(src, dst)
                return self.reply(200, {"ok": True})
            if self.path == "/api/clear-pictures":
                slug = f.get("slug", "")
                if not SLUG_RE.match(slug):
                    raise ValueError("Bad book name.")
                shutil.rmtree(ROOT / "books" / slug / "pictures", ignore_errors=True)
                return self.reply(200, {"ok": True})
        except ValueError as e:
            return self.reply(400, {"ok": False, "error": str(e)})
        except OSError as e:
            return self.reply(500, {"ok": False, "error": f"Could not start: {e}"})
        self.reply(404, {"error": "Not found."})


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Papermorph GUI</title>
<style>
:root{--bg:#f6f5f1;--card:#fff;--ink:#1c1b19;--mute:#6b6760;--line:#dcd8cf;--acc:#2f6f4f;--accink:#fff;--warn:#a4452b;--log:#12110f;--logink:#e8e4da}
@media (prefers-color-scheme:dark){:root{--bg:#161512;--card:#201e1a;--ink:#ece8de;--mute:#9a958a;--line:#3a362f;--acc:#6fb98f;--accink:#10201a;--warn:#e58b6f;--log:#0b0a09;--logink:#e8e4da}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:860px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:22px;margin:0 0 4px}.sub{color:var(--mute);margin:0 0 20px}
section{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin:0 0 14px}
h2{font-size:13px;letter-spacing:.06em;text-transform:uppercase;color:var(--mute);margin:0 0 10px}
label{display:block;font-weight:600;margin:10px 0 4px}.hint{color:var(--mute);font-weight:400;font-size:13px}
input[type=text],select,textarea{width:100%;padding:8px 10px;border:1px solid var(--line);border-radius:7px;background:var(--bg);color:var(--ink);font:inherit}
textarea{min-height:70px;resize:vertical}input[type=file]{width:100%}
.row{display:flex;gap:12px;flex-wrap:wrap}.row>div{flex:1;min-width:200px}
button{font:inherit;padding:8px 14px;border-radius:7px;border:1px solid var(--line);background:var(--card);color:var(--ink);cursor:pointer}
button.primary{background:var(--acc);color:var(--accink);border-color:var(--acc);font-weight:600}
button.danger{color:var(--warn);border-color:var(--warn)}button:disabled{opacity:.5;cursor:not-allowed}
.bar{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-top:12px}
.pill{padding:2px 10px;border-radius:99px;border:1px solid var(--line);font-size:13px;color:var(--mute)}
.pill.run{color:var(--accink);background:var(--acc);border-color:var(--acc)}.pill.bad{color:var(--warn);border-color:var(--warn)}
.msg{margin-top:8px;font-size:14px}.msg.err{color:var(--warn)}ul.files{margin:6px 0 0;padding-left:18px;color:var(--mute);font-size:13px}
pre{margin:0;white-space:pre-wrap;word-break:break-word}
#log{background:var(--log);color:var(--logink);border-radius:8px;padding:12px;height:340px;overflow:auto;font:13px/1.45 ui-monospace,Menlo,Consolas,monospace}
details{margin-top:10px}summary{cursor:pointer;color:var(--mute)}
details pre{background:var(--bg);border:1px solid var(--line);border-radius:7px;padding:10px;margin-top:8px;font:12.5px/1.45 ui-monospace,Menlo,Consolas,monospace}
.check{display:flex;gap:8px;align-items:flex-start;margin-top:10px}.check input{margin-top:4px}
a{color:var(--acc)}
</style></head><body><main>
<h1>Papermorph</h1>
<p class="sub">Choose what to make, with which model and agent, then start it. The agent's output appears below.</p>
<div id="setup" class="msg err" hidden></div>

<section><h2>1. Source</h2>
 <div class="row">
  <div><label for="type">Kind of book</label><select id="type"></select></div>
  <div><label for="slug">Book name <span class="hint">(lowercase, digits, hyphens)</span></label><input type="text" id="slug" placeholder="my-physics"></div>
 </div>
 <label for="pdf">PDF <span class="hint">(optional for a picture book or film made from a story or idea)</span></label>
 <input type="file" id="pdf" accept=".pdf,application/pdf">
 <div id="pdfmsg" class="msg"></div>
 <label for="request">What should the agent make?</label>
 <textarea id="request" placeholder="e.g. Turn chapter 3 into an animated lesson."></textarea>
</section>

<section><h2>2. Your own pictures <span class="hint" style="text-transform:none;letter-spacing:0">(optional)</span></h2>
 <div class="row">
  <div><label for="imgs">Choose pictures</label><input type="file" id="imgs" accept="image/*" multiple></div>
  <div><label for="imgdir">or a whole folder</label><input type="file" id="imgdir" webkitdirectory multiple></div>
 </div>
 <div id="imgmsg" class="msg"></div>
 <div class="bar"><button id="clearimgs" type="button">Remove uploaded pictures for this book</button></div>
</section>

<section><h2>3. Model</h2>
 <label for="model">Model <span class="hint" id="modelhint">(for OpenCode: provider/model, for example ollama/gemma4:26b; run <code>opencode models</code> to list names)</span></label>
 <input type="text" id="model" list="models" placeholder="ollama/gemma4:26b" autocomplete="off">
 <datalist id="models"></datalist>
 <div id="ollamamsg" class="msg hint"></div>
</section>

<section><h2>4. Agent</h2>
 <label for="agent">Agent</label>
 <select id="agent"><option value="opencode">OpenCode</option><option value="custom">Custom command</option></select>
 <div id="customwrap" hidden>
  <label for="custom">Command <span class="hint">placeholders: {model} {message} {prompt_file} {root}</span></label>
  <input type="text" id="custom" placeholder='my-agent --model {model} "{message}"'>
 </div>
 <label class="check"><input type="checkbox" id="auto" checked><span>Let the agent run without asking me for each permission
  <span class="hint"><br>A hands-off run cannot answer permission prompts. With OpenCode this adds <code>--auto</code>: the agent may write files and run commands in this folder without asking. For a custom command, add your agent's own flag.</span></span></label>
 <label for="extra">Extra instructions <span class="hint">(optional, added to the task)</span></label>
 <textarea id="extra" placeholder="e.g. Use Kokoro for narration and do not use any online service."></textarea>
 <details><summary>Show the task the agent will receive</summary><pre id="promptview">(choose a book name)</pre></details>
</section>

<section><h2>5. Run</h2>
 <div class="bar">
  <button class="primary" id="start">Start</button>
  <button class="danger" id="stop" disabled>Stop</button>
  <span class="pill" id="state">idle</span>
  <button id="preview" type="button">Start preview server</button>
  <a id="open" href="#" target="_blank" rel="noopener" hidden>Open the book</a>
 </div>
 <div id="runmsg" class="msg"></div>
 <div id="log" aria-live="polite"><pre id="logtext">Nothing has run yet.</pre></div>
</section>
</main>
<script>
const TOKEN="__TOKEN__";
const $=id=>document.getElementById(id);
const H={"X-Token":TOKEN};
let info=null, next=0, polling=null, lastSlug="", renameP=Promise.resolve();
const api=(p,o={})=>fetch(p,{...o,headers:{...H,...(o.headers||{})}}).then(r=>r.json());
const post=(p,b)=>api(p,{method:"POST",body:JSON.stringify(b||{}),headers:{"Content-Type":"application/json"}});
const slugOf=s=>s.toLowerCase().replace(/\.[^.]+$/,"").replace(/[^a-z0-9]+/g,"-").replace(/^-+|-+$/g,"").slice(0,40);
const state=(t,c)=>{const e=$("state");e.textContent=t;e.className="pill "+(c||"")};
const slug=()=>$("slug").value.trim();

async function init(){
  info=await api("/api/info");
  for(const[k,v]of Object.entries(info.types)){const o=document.createElement("option");o.value=k;o.textContent=k[0].toUpperCase()+k.slice(1)+" - "+v;$("type").appendChild(o)}
  if(!info.skill){const e=$("setup");e.hidden=false;e.textContent="This file is not in the Papermorph repository root (.claude/skills/papermorph was not found next to it). Move it there and start it again."}
  const m=info.ollama;
  if(m===null){$("ollamamsg").textContent="Ollama was not found on this computer (http://127.0.0.1:11434). Type a model name yourself."}
  else if(!m.length){$("ollamamsg").textContent="Ollama is running but has no models yet. Run: ollama pull <model>"}
  else{$("ollamamsg").textContent=m.length+" Ollama model(s) found. Pick one from the list."; $("models").innerHTML=m.map(n=>`<option value="ollama/${n}">`).join("")}
  if(info.running){watch()}
  refreshPrompt();
}
$("agent").onchange=()=>{$("customwrap").hidden=$("agent").value!=="custom";refreshPrompt()};

async function upload(kind,file,name){
  await renameP;
  const r=await (await fetch(`/api/upload?kind=${kind}&slug=${encodeURIComponent(slug())}&name=${encodeURIComponent(name||file.name)}`,{method:"PUT",headers:H,body:file})).json();
  if(r.ok)lastSlug=slug();
  return r;
}
$("pdf").onchange=async()=>{
  const f=$("pdf").files[0]; if(!f)return;
  if(!slug())$("slug").value=slugOf(f.name);
  if(!/^[a-z0-9][a-z0-9-]*$/.test(slug())){$("pdfmsg").className="msg err";$("pdfmsg").textContent="Give the book a valid name first (lowercase, digits, hyphens), then choose the PDF again.";return}
  $("pdfmsg").className="msg";$("pdfmsg").textContent="Uploading "+f.name+" ...";
  const r=await upload("pdf",f);
  $("pdfmsg").className="msg"+(r.ok?"":" err");
  $("pdfmsg").textContent=r.ok?`Saved as ${r.saved} (${(r.bytes/1048576).toFixed(1)} MB).`:r.error;
  refreshPrompt();
};
async function sendImages(list){
  if(!list.length)return;
  if(!/^[a-z0-9][a-z0-9-]*$/.test(slug())){$("imgmsg").className="msg err";$("imgmsg").textContent="Give the book a valid name first (section 1).";return}
  const ok=[],bad=[];let i=0;
  for(const f of list){
    $("imgmsg").className="msg";$("imgmsg").textContent=`Uploading ${++i} of ${list.length} ...`;
    const r=await upload("image",f,f.name);(r.ok?ok:bad).push(r.ok?f.name:r.error);
  }
  $("imgmsg").className="msg"+(bad.length?" err":"");
  $("imgmsg").innerHTML=`Saved ${ok.length} picture(s).`+(bad.length?"<ul class='files'>"+bad.slice(0,8).map(b=>`<li>${b.replace(/</g,"&lt;")}</li>`).join("")+(bad.length>8?`<li>and ${bad.length-8} more skipped</li>`:"")+"</ul>":"");
  refreshPrompt();
}
$("imgs").onchange=()=>sendImages([...$("imgs").files]);
$("imgdir").onchange=()=>sendImages([...$("imgdir").files].filter(f=>/\.(png|jpe?g|webp|gif|svg)$/i.test(f.name)));
$("clearimgs").onclick=async()=>{const r=await post("/api/clear-pictures",{slug:slug()});$("imgmsg").className="msg"+(r.ok?"":" err");$("imgmsg").textContent=r.ok?"Pictures removed.":r.error;refreshPrompt()};

function form(){return{slug:slug(),type:$("type").value,request:$("request").value,extra:$("extra").value,model:$("model").value,agent:$("agent").value,custom:$("custom").value,auto:$("auto").checked}}
let pt=null;
function refreshPrompt(){clearTimeout(pt);pt=setTimeout(async()=>{
  const f=form();const q=new URLSearchParams({slug:f.slug,type:f.type,request:f.request,extra:f.extra});
  const r=await api("/api/prompt?"+q);$("promptview").textContent=r.prompt||"";},250)}
for(const id of["slug","type","request","extra"])$(id).addEventListener("input",refreshPrompt);
// Files already uploaded follow the book when its name is edited afterwards.
$("slug").addEventListener("change",()=>{renameP=renameP.then(async()=>{
  const n=slug();if(!lastSlug||n===lastSlug)return;
  const r=await post("/api/rename",{old:lastSlug,new:n});
  if(r.ok){lastSlug=n;if($("pdfmsg").textContent.startsWith("Saved"))$("pdfmsg").textContent=`Moved to books/${n}/.`;if($("imgmsg").textContent.startsWith("Saved"))$("imgmsg").textContent+=` Moved to books/${n}/.`}
  else{$("pdfmsg").className="msg err";$("pdfmsg").textContent=r.error;$("slug").value=lastSlug}
  refreshPrompt();
})});

function watch(){clearInterval(polling);polling=setInterval(tick,700);tick()}
async function tick(){
  const r=await api("/api/log?since="+next);
  if(r.lines&&r.lines.length){
    const el=$("log"),atEnd=el.scrollHeight-el.scrollTop-el.clientHeight<40;
    if(next===0)$("logtext").textContent="";
    $("logtext").textContent+=r.lines.join("\n")+"\n";next=r.next;
    if(atEnd)el.scrollTop=el.scrollHeight;
  }
  $("start").disabled=r.running;$("stop").disabled=!r.running;
  if(r.running)state("running","run");
  else{state(r.stopped?"stopped":(r.code===0?"finished":(r.code===null?"idle":"exit code "+r.code)),r.stopped||r.code===0||r.code===null?"":"bad");clearInterval(polling)}
  if(r.slug){const a=$("open");a.hidden=false;a.href=`http://localhost:${info.preview_port}/${r.slug}/`;a.textContent="Open the book (needs the preview server)"}
}
$("start").onclick=async()=>{
  $("runmsg").className="msg";$("runmsg").textContent="";
  await renameP;
  const r=await post("/api/run",form());
  if(!r.ok){$("runmsg").className="msg err";$("runmsg").textContent=r.error;return}
  next=0;$("logtext").textContent="";$("runmsg").textContent="Started. The task was written to books/"+slug()+"/prompt.md";watch();
};
$("stop").onclick=async()=>{await post("/api/stop");tick()};
$("preview").onclick=async()=>{const r=await post("/api/preview");$("runmsg").className="msg"+(r.ok?"":" err");$("runmsg").textContent=r.ok?`Preview server ${r.status}: http://localhost:${info.preview_port}/`:r.error};
init();
</script></body></html>
"""


def main():
    srv = None
    for port in range(PORT, PORT + 20):
        try:
            globals()["PORT"] = port
            srv = Server(("127.0.0.1", port), Handler)
            break
        except OSError:
            continue
    if srv is None:
        sys.exit(f"Could not open a port between {PORT} and {PORT + 19}.")
    url = f"http://127.0.0.1:{PORT}/"
    print(f"Papermorph GUI: {url}   (Ctrl+C to quit)")
    if not (ROOT / ".claude" / "skills" / "papermorph" / "SKILL.md").is_file():
        print("Warning: this file is not in the Papermorph repository root, so Start will refuse to run.")
    threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        RUN.stop()
        p = PREVIEW["proc"]
        if p and p.poll() is None:
            p.terminate()


if __name__ == "__main__":
    main()
