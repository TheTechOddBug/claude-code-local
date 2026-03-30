#!/usr/bin/env python3
"""
Orchestrator API — Controls browser, macOS apps, and screen recording from one endpoint.
Accepts natural language prompts, uses local AI to plan and execute multi-step tasks.

    POST /tasks              → submit a task (natural language)
    GET  /tasks/{id}         → get task status + result
    POST /tasks/{id}/stop    → cancel a running task
    WS   /ws/tasks/{id}      → real-time progress stream
    POST /tools/browser/*    → direct browser control
    POST /tools/macos/*      → AppleScript, open/close apps
    POST /tools/record/*     → start/stop Studio Record
    GET  /status             → server health + capabilities

Runs on port 4001. Expects MLX server on 4000 and Brave on 9222.
"""

import asyncio
import json
import os
import re
import subprocess
import sys
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from enum import Enum
from typing import Optional

import urllib.request
import websockets as ws_lib
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ─── Config ──────────────────────────────────────────────────────────────────

MLX_URL = os.environ.get("MLX_URL", "http://localhost:4000")
CDP_URL = os.environ.get("CDP_URL", "http://127.0.0.1:9222")
MODEL = os.environ.get("MLX_MODEL_NAME", "claude-sonnet-4-6")
API_PORT = int(os.environ.get("ORCHESTRATOR_PORT", "4001"))
MAX_STEPS = int(os.environ.get("MAX_STEPS", "30"))
MAX_CONTEXT_TOKENS = int(os.environ.get("MAX_CONTEXT_TOKENS", "32000"))

STUDIO_RECORD_APP = "/Users/dtribe/Desktop/Studio Record.app"


# ─── Models ──────────────────────────────────────────────────────────────────

class TaskStatus(str, Enum):
    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


class TaskRequest(BaseModel):
    prompt: str
    timeout: Optional[int] = 300  # seconds


class ToolRequest(BaseModel):
    args: Optional[dict] = {}


class TaskState:
    def __init__(self, task_id: str, prompt: str, timeout: int = 300):
        self.id = task_id
        self.prompt = prompt
        self.status = TaskStatus.pending
        self.steps: list[dict] = []
        self.result: Optional[str] = None
        self.error: Optional[str] = None
        self.created_at = datetime.now().isoformat()
        self.started_at: Optional[str] = None
        self.completed_at: Optional[str] = None
        self.timeout = timeout
        self.cancel_flag = False
        self._ws_clients: list[WebSocket] = []

    def to_dict(self):
        return {
            "id": self.id,
            "prompt": self.prompt,
            "status": self.status.value,
            "steps": self.steps,
            "result": self.result,
            "error": self.error,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "step_count": len(self.steps),
        }

    async def broadcast(self, event: dict):
        """Send event to all connected WebSocket clients."""
        for client in self._ws_clients[:]:
            try:
                await client.send_json(event)
            except Exception:
                self._ws_clients.remove(client)

    async def add_step(self, step: dict):
        self.steps.append(step)
        await self.broadcast({"type": "step", "step": step, "step_number": len(self.steps)})


# Global task store
tasks: dict[str, TaskState] = {}


# ─── CDP (Browser Control) ──────────────────────────────────────────────────

class CDP:
    """Chrome DevTools Protocol client for Brave browser."""

    def __init__(self):
        self.ws = None
        self.mid = 0

    async def connect(self):
        try:
            with urllib.request.urlopen(f"{CDP_URL}/json", timeout=5) as r:
                pages = json.loads(r.read())
        except Exception:
            raise ConnectionError("Cannot connect to Brave on port 9222. Launch Brave with --remote-debugging-port=9222")

        ws_url = next(
            (p["webSocketDebuggerUrl"] for p in pages
             if p.get("type") == "page" and "devtools" not in p.get("url", "")),
            None
        )
        if not ws_url and pages:
            ws_url = pages[0].get("webSocketDebuggerUrl")
        if not ws_url:
            # Create a new tab
            req = urllib.request.Request(f"{CDP_URL}/json/new", method="PUT")
            with urllib.request.urlopen(req, timeout=5) as r:
                new_page = json.loads(r.read())
            ws_url = new_page.get("webSocketDebuggerUrl")
            await asyncio.sleep(1)
        if not ws_url:
            raise ConnectionError("No Brave tab available")

        self.ws = await ws_lib.connect(ws_url, max_size=50 * 1024 * 1024)
        for m in ["DOM.enable", "Accessibility.enable", "Page.enable", "Runtime.enable"]:
            await self.cmd(m)

    async def reconnect(self):
        try:
            if self.ws:
                await self.ws.close()
        except Exception:
            pass
        await asyncio.sleep(1)
        with urllib.request.urlopen(f"{CDP_URL}/json", timeout=5) as r:
            pages = json.loads(r.read())
        ws_url = next(
            (p["webSocketDebuggerUrl"] for p in pages
             if p.get("type") == "page" and "devtools" not in p.get("url", "")),
            None
        )
        if ws_url:
            self.ws = await ws_lib.connect(ws_url, max_size=50 * 1024 * 1024)
            self.mid = 0
            for m in ["DOM.enable", "Accessibility.enable", "Page.enable", "Runtime.enable"]:
                await self.cmd(m)

    async def cmd(self, method, params=None):
        self.mid += 1
        msg = {"id": self.mid, "method": method}
        if params:
            msg["params"] = params
        try:
            await self.ws.send(json.dumps(msg))
            while True:
                r = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=30))
                if r.get("id") == self.mid:
                    return r.get("result", r.get("error", {}))
        except Exception:
            await self.reconnect()
            return {"error": "Connection lost, reconnected"}

    async def navigate(self, url):
        await self.cmd("Page.navigate", {"url": url})
        # Wait for page to actually load
        for _ in range(10):
            await asyncio.sleep(1)
            state = await self.cmd("Runtime.evaluate", {
                "expression": "document.readyState",
                "returnByValue": True
            })
            if state.get("result", {}).get("value") in ("complete", "interactive"):
                break
        await asyncio.sleep(1)  # extra settle time for JS rendering
        return f"Navigated to {url}"

    async def snapshot(self):
        tree = await self.cmd("Accessibility.getFullAXTree", {"max_depth": 8})
        nodes = tree.get("nodes", [])
        lines = []
        priority_roles = {"link", "button", "textbox", "searchbox", "heading", "combobox", "menuitem", "checkbox", "radio"}
        for n in nodes:
            role = n.get("role", {}).get("value", "")
            name = n.get("name", {}).get("value", "")
            nid = n.get("nodeId", "")
            if not name or len(name) < 3:
                continue
            if role not in priority_roles and role != "StaticText":
                continue
            if role == "StaticText" and len(name) < 30:
                continue
            lines.append(f"[{nid}] {role} \"{name[:120]}\"")
            if len(lines) >= 200:
                break
        return "\n".join(lines) if lines else "(Empty page)"

    async def click(self, uid):
        r = await self.cmd("DOM.resolveNode", {"backendNodeId": int(uid)})
        if "error" in r:
            return f"Error: {r['error']}"
        oid = r.get("object", {}).get("objectId")
        if not oid:
            return "Error: can't resolve"
        await self.cmd("Runtime.callFunctionOn", {
            "objectId": oid,
            "functionDeclaration": "function(){this.scrollIntoView({block:'center'})}"
        })
        await asyncio.sleep(0.2)
        box = await self.cmd("DOM.getBoxModel", {"objectId": oid})
        if "error" in box or "model" not in box:
            await self.cmd("Runtime.callFunctionOn", {
                "objectId": oid,
                "functionDeclaration": "function(){this.click()}"
            })
            return "Clicked(JS)"
        c = box["model"]["content"]
        x, y = (c[0] + c[4]) / 2, (c[1] + c[5]) / 2
        await self.cmd("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1})
        await self.cmd("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1})
        return "Clicked"

    async def type_into(self, uid, text):
        await self.click(uid)
        await asyncio.sleep(0.3)
        for ch in text:
            await self.cmd("Input.dispatchKeyEvent", {"type": "keyDown", "text": ch, "key": ch})
            await self.cmd("Input.dispatchKeyEvent", {"type": "keyUp", "key": ch})
        return f"Typed {len(text)} chars"

    async def scroll(self, direction="down"):
        delta = -500 if direction == "up" else 500
        await self.cmd("Input.dispatchMouseEvent", {
            "type": "mouseWheel", "x": 400, "y": 400, "deltaX": 0, "deltaY": delta
        })
        await asyncio.sleep(0.5)
        return f"Scrolled {direction}"

    async def js(self, code):
        r = await self.cmd("Runtime.evaluate", {"expression": code, "returnByValue": True, "awaitPromise": True})
        if "error" in r:
            return f"Error: {r['error']}"
        return str(r.get("result", {}).get("value", r.get("result", {}).get("description", "")))[:2000]

    async def post_comment(self, text):
        """Handle commenting — iframes, Shadow DOM, ProseMirror. Never auto-submits."""
        # Scroll to comment section
        await self.cmd("Runtime.evaluate", {"expression": """
            const section = document.querySelector('#comments, .comments-area, .comment-section, [id*=comment], textarea#comment, #respond');
            if(section) section.scrollIntoView({block:'center',behavior:'instant'});
            else {
                const btn=Array.from(document.querySelectorAll('button,a')).find(b=>/show comment|view comment|add comment|leave comment/i.test(b.textContent) && !/post|submit|send/i.test(b.textContent));
                if(btn){btn.scrollIntoView({block:'center'});btn.click()}
            }
        """})
        await asyncio.sleep(3)

        # Wait for widget
        await asyncio.sleep(5)

        # Find comment iframe
        for attempt in range(8):
            with urllib.request.urlopen(f"{CDP_URL}/json", timeout=5) as r:
                targets = json.loads(r.read())
            ow = [t for t in targets if t.get("type") == "iframe"
                  and any(k in t.get("url", "") for k in ["openweb", "spot.im", "disqus", "comment"])
                  and t.get("webSocketDebuggerUrl")]
            if ow:
                break
            await self.cmd("Runtime.evaluate", {"expression": "window.scrollBy(0,150)"})
            await asyncio.sleep(2)

        if ow:
            iws = await ws_lib.connect(ow[0]["webSocketDebuggerUrl"], max_size=50 * 1024 * 1024)
            imid = [0]

            async def isend(m, p=None):
                imid[0] += 1
                msg = {"id": imid[0], "method": m}
                if p:
                    msg["params"] = p
                await iws.send(json.dumps(msg))
                while True:
                    r = json.loads(await asyncio.wait_for(iws.recv(), timeout=15))
                    if r.get("id") == imid[0]:
                        return r.get("result", r.get("error", {}))

            for m in ["DOM.enable", "Runtime.enable", "Input.enable"]:
                await isend(m)
            await isend("DOM.getDocument", {"depth": -1, "pierce": True})

            for attempt in range(5):
                await isend("DOM.getDocument", {"depth": -1, "pierce": True})
                r = await isend("DOM.performSearch", {"query": ".ProseMirror", "includeUserAgentShadowDOM": True})
                count = r.get("resultCount", 0)
                sid = r.get("searchId", "")
                if count > 0:
                    results = await isend("DOM.getSearchResults", {"searchId": sid, "fromIndex": 0, "toIndex": count})
                    nid = results.get("nodeIds", [])[0]
                    fr = await isend("DOM.focus", {"nodeId": nid})
                    if "error" not in fr:
                        await asyncio.sleep(1)
                        await isend("Input.insertText", {"text": text})
                        await asyncio.sleep(0.5)
                        if sid:
                            await isend("DOM.discardSearchResults", {"searchId": sid})
                        await iws.close()
                        return f"Comment drafted ({len(text)} chars) — NOT posted, ready for review."
                if sid:
                    await isend("DOM.discardSearchResults", {"searchId": sid})
                await asyncio.sleep(3)

            await iws.close()

        # Fallback: standard textarea
        escaped = text.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")
        r = await self.cmd("Runtime.evaluate", {"expression": f"""
            const el = document.querySelector('textarea#comment, textarea[name=comment], textarea.comment-textarea, textarea');
            if(el) {{
                el.scrollIntoView({{block:'center',behavior:'instant'}});
                el.focus();
                el.value = '{escaped}';
                el.dispatchEvent(new Event('input', {{bubbles:true}}));
                'found'
            }} else 'none'
        """, "returnByValue": True})
        if r.get("result", {}).get("value") == "found":
            return f"Comment drafted ({len(text)} chars) — NOT posted, ready for review."

        return "No comment input found on this page."

    async def close(self):
        if self.ws:
            await self.ws.close()


# ─── macOS Automation ────────────────────────────────────────────────────────

class MacOS:
    """Control macOS apps, run AppleScript, manage processes."""

    @staticmethod
    async def open_app(app_name_or_path: str) -> str:
        """Open a macOS application by name or path."""
        if app_name_or_path.endswith(".app") and os.path.exists(app_name_or_path):
            cmd = ["open", app_name_or_path]
        else:
            cmd = ["open", "-a", app_name_or_path]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            return f"Error opening {app_name_or_path}: {stderr.decode().strip()}"
        await asyncio.sleep(2)  # let app launch
        return f"Opened {app_name_or_path}"

    @staticmethod
    async def close_app(app_name: str) -> str:
        """Quit a macOS application by name."""
        script = f'tell application "{app_name}" to quit'
        return await MacOS.applescript(script)

    @staticmethod
    async def applescript(code: str) -> str:
        """Execute AppleScript and return output."""
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", code,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        output = stdout.decode().strip()
        if proc.returncode != 0:
            err = stderr.decode().strip()
            return f"AppleScript error: {err}"
        return output if output else "OK"

    @staticmethod
    async def run_command(command: str, timeout: int = 30) -> str:
        """Run a shell command and return output."""
        proc = await asyncio.create_subprocess_shell(
            command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return f"Command timed out after {timeout}s"
        output = stdout.decode().strip()
        if proc.returncode != 0:
            output += f"\nSTDERR: {stderr.decode().strip()}"
        return output if output else "OK"

    @staticmethod
    async def notification(title: str, message: str) -> str:
        """Show a macOS notification."""
        script = f'display notification "{message}" with title "{title}"'
        return await MacOS.applescript(script)


# ─── Studio Record Control ───────────────────────────────────────────────────

class Recorder:
    """Control Studio Record.app for screen recording."""

    @staticmethod
    async def start(duration_seconds: Optional[int] = None) -> str:
        """Launch Studio Record and start recording."""
        # Open the app
        result = await MacOS.open_app(STUDIO_RECORD_APP)
        if "Error" in result:
            return result
        await asyncio.sleep(2)

        # Click the Record button via AppleScript
        script = '''
            tell application "System Events"
                tell process "Studio Record"
                    set frontmost to true
                    delay 0.5
                    -- Look for the Record button and click it
                    click button 1 of window 1
                end tell
            end tell
        '''
        click_result = await MacOS.applescript(script)

        msg = f"Studio Record started"
        if duration_seconds:
            msg += f" (will record for {duration_seconds}s)"
        return msg

    @staticmethod
    async def stop() -> str:
        """Stop recording in Studio Record."""
        script = '''
            tell application "System Events"
                tell process "Studio Record"
                    set frontmost to true
                    delay 0.3
                    click button 1 of window 1
                end tell
            end tell
        '''
        result = await MacOS.applescript(script)
        return f"Studio Record stopped. {result}"

    @staticmethod
    async def start_timed(duration_seconds: int) -> str:
        """Start recording for a specific duration, then auto-stop."""
        start_result = await Recorder.start()
        if "Error" in start_result:
            return start_result

        async def auto_stop():
            await asyncio.sleep(duration_seconds)
            await Recorder.stop()
            await MacOS.notification("Studio Record", f"Recording finished ({duration_seconds}s)")

        asyncio.create_task(auto_stop())
        return f"Recording started — will auto-stop in {duration_seconds}s"


# ─── AI Engine ───────────────────────────────────────────────────────────────

ORCHESTRATOR_SYSTEM = """You are a task orchestrator that controls a Mac computer. You have these tools:

BROWSER TOOLS (control Brave browser):
- browser_navigate(url) — Go to a URL
- browser_snapshot() — Get page elements with UIDs
- browser_click(uid) — Click an element
- browser_type(uid, text) — Type into an element
- browser_scroll(direction) — "up" or "down"
- browser_js(code) — Run JavaScript on the page. Use this to extract article text or interact with dynamic content.
- browser_comment(text) — Draft a comment (never auto-submits)

MACOS TOOLS (control the Mac):
- open_app(path_or_name) — Open any Mac application
- close_app(name) — Quit an application
- applescript(code) — Run AppleScript
- run_command(command) — Run a shell command
- notification(title, message) — Show a macOS notification

RECORDING TOOLS:
- start_recording() — Open Studio Record and start recording
- start_recording_timed(seconds) — Record for N seconds then auto-stop
- stop_recording() — Stop recording

CONTROL:
- wait(seconds) — Pause for N seconds
- done(message) — Task complete, return result

RULES:
- Return ONE JSON tool call per response: {"tool": "name", "args": {...}}
- After browser_navigate, always browser_snapshot next.
- If browser_snapshot returns "(Empty page)", wait 3 seconds and try again. Pages need time to load.
- For finding articles: use browser_js to search links rather than clicking blindly. Example:
  browser_js(code="Array.from(document.querySelectorAll('a')).filter(a=>a.href&&a.textContent.length>20).slice(0,10).map(a=>a.href+'|||'+a.textContent.trim().substring(0,80)).join('\\n')")
- STAY ON THE REQUESTED SITE. Do not navigate to external sites. If you need a Yahoo article, stay on yahoo.com.
- For commenting: navigate to article, read it with browser_js, generate a relevant comment, use browser_comment.
- For recording: start recording BEFORE the main task, stop AFTER.
- Never auto-submit comments. Always leave as draft.
- Be decisive and efficient. Minimize steps. Don't explain, just call the tool.
"""


def ask_model(messages: list[dict], system: str = ORCHESTRATOR_SYSTEM) -> str:
    """Send a prompt to the local MLX AI and return the response text."""
    body = json.dumps({
        "model": MODEL,
        "max_tokens": 2048,
        "temperature": 0.2,
        "system": system,
        "messages": messages,
    }).encode()
    req = urllib.request.Request(
        f"{MLX_URL}/v1/messages",
        data=body,
        headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        result = json.loads(r.read())
    return "".join(b.get("text", "") for b in result.get("content", []) if b.get("type") == "text")


def generate_comment(article_text: str) -> str:
    """Generate a relevant comment for an article using local AI."""
    body = json.dumps({
        "model": MODEL,
        "max_tokens": 1024,
        "temperature": 0.7,
        "system": "Write a comment on this news article. 2-3 sentences. Be thoughtful and relevant. Output ONLY the comment text, nothing else.",
        "messages": [{"role": "user", "content": f"Article: {article_text[:800]}"}],
    }).encode()
    req = urllib.request.Request(f"{MLX_URL}/v1/messages", data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        result = json.loads(r.read())
    raw = "".join(b.get("text", "") for b in result.get("content", []) if b.get("type") == "text")
    text = re.sub(r'<think>.*?</think>', '', raw, flags=re.DOTALL).strip()
    text = re.sub(r'\*+', '', text)

    # Filter out meta-reasoning
    sentences = re.findall(r'([A-Z][^.!?]{20,}[.!?])', text)
    meta = ['draft', 'constraint', 'sentence', 'critique', 'user', 'task', 'goal',
            'checking', 'format', 'plain text', 'let me', "let's", 'count',
            'analyze', 'request', 'input', 'output', 'concise', 'polish',
            'revised', 'alternative', 'stick to', 'meets', 'criteria',
            'thinking', 'process', 'step', 'final', 'make sure']
    real = [s.strip() for s in sentences if not any(w in s.lower() for w in meta) and len(s) > 30]
    if real:
        return ' '.join(real[-3:])
    return text[:200] if text else "This raises important questions worth discussing further."


def parse_tool_call(text: str) -> Optional[dict]:
    """Extract a JSON tool call from model output."""
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()
    # Also strip <tool_call> wrappers
    text = re.sub(r'</?tool_call>', '', text).strip()

    start = text.find('{"tool"')
    if start < 0:
        start = text.find('{ "tool"')
    if start >= 0:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == '{':
                depth += 1
            elif text[i] == '}':
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        break
    # Fallback: find any JSON with "tool" key
    for m in re.finditer(r'\{[^{}]+\}', text):
        try:
            o = json.loads(m.group(0))
            if "tool" in o:
                return o
        except json.JSONDecodeError:
            continue
    return None


def estimate_tokens(text: str) -> int:
    return len(text) // 4


def smart_trim(messages: list[dict], original_task: str) -> list[dict]:
    """Trim context intelligently when nearing limits."""
    total = sum(estimate_tokens(m.get("content", "")) for m in messages)
    if total < MAX_CONTEXT_TOKENS * 0.6:
        return messages
    keep_recent = 12
    if len(messages) <= keep_recent + 1:
        return messages
    first = messages[0]
    middle = messages[1:-keep_recent]
    recent = messages[-keep_recent:]

    # Compress middle into summary
    summaries = []
    for m in middle:
        if m["role"] == "assistant":
            try:
                tc = json.loads(m["content"])
                tool = tc.get("tool", "?")
                summaries.append(tool)
            except (json.JSONDecodeError, AttributeError):
                pass
    summary = ", ".join(summaries) if summaries else "previous steps"

    context_msg = {
        "role": "user",
        "content": f"[CONTEXT: completed steps: {summary}]\nOriginal task: {original_task}\nContinue. Return ONE JSON tool call."
    }
    return [first, context_msg] + recent


# ─── Task Execution Engine ───────────────────────────────────────────────────

async def execute_task(task: TaskState):
    """Run a task through the AI orchestration loop."""
    task.status = TaskStatus.running
    task.started_at = datetime.now().isoformat()
    await task.broadcast({"type": "status", "status": "running"})

    cdp = None
    try:
        task_prompt = (
            f"Task: {task.prompt}\n\n"
            "Plan your steps and execute them one at a time. Return ONE JSON tool call."
        )
        messages = [{"role": "user", "content": task_prompt}]

        for step_num in range(1, MAX_STEPS + 1):
            if task.cancel_flag:
                task.status = TaskStatus.cancelled
                task.result = f"Cancelled at step {step_num}"
                await task.broadcast({"type": "status", "status": "cancelled"})
                return

            # Trim context if needed
            messages = smart_trim(messages, task.prompt)

            # Ask the AI what to do next
            t0 = time.time()
            resp = await asyncio.to_thread(ask_model, messages)
            elapsed = time.time() - t0

            tc = parse_tool_call(resp)
            if not tc:
                # No tool call — nudge the model
                messages.append({"role": "assistant", "content": resp})
                messages.append({"role": "user", "content": 'Respond with ONLY: {"tool":"name","args":{...}}'})
                await task.add_step({"step": step_num, "action": "no_tool", "time": round(elapsed, 1)})
                continue

            tool = tc.get("tool", "")
            args = tc.get("args", {})

            step_info = {"step": step_num, "tool": tool, "args": args, "time": round(elapsed, 1)}

            # Execute the tool
            result = ""
            try:
                # ── Browser tools ──
                if tool == "browser_navigate":
                    if not cdp:
                        cdp = CDP()
                        await cdp.connect()
                    result = await cdp.navigate(args.get("url", ""))

                elif tool == "browser_snapshot":
                    if not cdp:
                        cdp = CDP()
                        await cdp.connect()
                    result = await cdp.snapshot()

                elif tool == "browser_click":
                    if cdp:
                        result = await cdp.click(str(args.get("uid", "")))
                    else:
                        result = "Error: browser not connected"

                elif tool == "browser_type":
                    if cdp:
                        result = await cdp.type_into(str(args.get("uid", "")), args.get("text", ""))
                    else:
                        result = "Error: browser not connected"

                elif tool == "browser_scroll":
                    if cdp:
                        result = await cdp.scroll(args.get("direction", "down"))
                    else:
                        result = "Error: browser not connected"

                elif tool == "browser_js":
                    if cdp:
                        result = await cdp.js(args.get("code", ""))
                    else:
                        result = "Error: browser not connected"

                elif tool == "browser_comment":
                    if cdp:
                        comment_text = args.get("text", "")
                        if not comment_text:
                            # Generate from article content
                            article_text = await cdp.js(
                                "document.title + '. ' + Array.from(document.querySelectorAll('p'))"
                                ".map(p=>p.innerText).filter(t=>t.length>40).slice(0,6).join(' ')"
                            )
                            comment_text = await asyncio.to_thread(generate_comment, article_text[:600])
                        result = await cdp.post_comment(comment_text)
                    else:
                        result = "Error: browser not connected"

                # ── macOS tools ──
                elif tool == "open_app":
                    result = await MacOS.open_app(args.get("path_or_name", args.get("name", "")))

                elif tool == "close_app":
                    result = await MacOS.close_app(args.get("name", ""))

                elif tool == "applescript":
                    result = await MacOS.applescript(args.get("code", ""))

                elif tool == "run_command":
                    result = await MacOS.run_command(args.get("command", ""))

                elif tool == "notification":
                    result = await MacOS.notification(args.get("title", ""), args.get("message", ""))

                # ── Recording tools ──
                elif tool == "start_recording":
                    result = await Recorder.start()

                elif tool == "start_recording_timed":
                    seconds = int(args.get("seconds", args.get("duration", 240)))
                    result = await Recorder.start_timed(seconds)

                elif tool == "stop_recording":
                    result = await Recorder.stop()

                # ── Control tools ──
                elif tool == "wait":
                    seconds = int(args.get("seconds", 5))
                    await asyncio.sleep(seconds)
                    result = f"Waited {seconds}s"

                elif tool == "done":
                    step_info["result"] = args.get("message", "Task complete")
                    await task.add_step(step_info)
                    task.status = TaskStatus.completed
                    task.result = args.get("message", "Task complete")
                    task.completed_at = datetime.now().isoformat()
                    await task.broadcast({"type": "status", "status": "completed", "result": task.result})
                    if cdp:
                        await cdp.close()
                    return

                else:
                    result = f"Unknown tool: {tool}"

            except Exception as e:
                result = f"Error: {str(e)}"

            # Truncate long results
            if len(result) > 4000:
                result = result[:4000] + "...(truncated)"

            step_info["result"] = result[:200]
            await task.add_step(step_info)

            messages.append({"role": "assistant", "content": json.dumps(tc)})
            messages.append({"role": "user", "content": f"Result: {result}"})

        # Hit max steps
        task.status = TaskStatus.completed
        task.result = f"Reached max steps ({MAX_STEPS})"
        task.completed_at = datetime.now().isoformat()
        await task.broadcast({"type": "status", "status": "completed", "result": task.result})

    except Exception as e:
        task.status = TaskStatus.failed
        task.error = str(e)
        task.completed_at = datetime.now().isoformat()
        await task.broadcast({"type": "status", "status": "failed", "error": str(e)})

    finally:
        if cdp:
            try:
                await cdp.close()
            except Exception:
                pass


# ─── FastAPI App ─────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    print(f"\n  Orchestrator API running on http://localhost:{API_PORT}")
    print(f"  MLX Server: {MLX_URL}")
    print(f"  Brave CDP:  {CDP_URL}")
    print(f"  Docs:       http://localhost:{API_PORT}/docs\n")
    yield

app = FastAPI(
    title="Orchestrator API",
    description="Controls browser, macOS apps, and screen recording via local AI.",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── Routes ──────────────────────────────────────────────────────────────────

@app.get("/status")
async def status():
    """Server health and capability check."""
    # Check MLX
    mlx_ok = False
    try:
        with urllib.request.urlopen(f"{MLX_URL}/health", timeout=3) as r:
            mlx_ok = json.loads(r.read()).get("status") == "ok"
    except Exception:
        pass

    # Check Brave
    brave_ok = False
    try:
        with urllib.request.urlopen(f"{CDP_URL}/json", timeout=3) as r:
            brave_ok = len(json.loads(r.read())) > 0
    except Exception:
        pass

    return {
        "status": "ok",
        "services": {
            "mlx_server": {"url": MLX_URL, "connected": mlx_ok},
            "brave_cdp": {"url": CDP_URL, "connected": brave_ok},
            "studio_record": {"path": STUDIO_RECORD_APP, "exists": os.path.exists(STUDIO_RECORD_APP)},
        },
        "capabilities": ["browser", "macos", "recording", "ai_orchestration"],
        "active_tasks": len([t for t in tasks.values() if t.status == TaskStatus.running]),
    }


@app.post("/tasks")
async def create_task(req: TaskRequest):
    """Submit a natural language task for AI orchestration."""
    task_id = uuid.uuid4().hex[:12]
    task = TaskState(task_id, req.prompt, req.timeout)
    tasks[task_id] = task

    # Run task in background
    asyncio.create_task(execute_task(task))

    return {"id": task_id, "status": "pending", "prompt": req.prompt}


@app.get("/tasks/{task_id}")
async def get_task(task_id: str):
    """Get task status and results."""
    if task_id not in tasks:
        raise HTTPException(404, "Task not found")
    return tasks[task_id].to_dict()


@app.post("/tasks/{task_id}/stop")
async def stop_task(task_id: str):
    """Cancel a running task."""
    if task_id not in tasks:
        raise HTTPException(404, "Task not found")
    task = tasks[task_id]
    if task.status != TaskStatus.running:
        raise HTTPException(400, f"Task is {task.status.value}, not running")
    task.cancel_flag = True
    return {"id": task_id, "status": "cancelling"}


@app.get("/tasks")
async def list_tasks():
    """List all tasks."""
    return [t.to_dict() for t in tasks.values()]


# ─── WebSocket for real-time progress ────────────────────────────────────────

@app.websocket("/ws/tasks/{task_id}")
async def ws_task(websocket: WebSocket, task_id: str):
    """Stream real-time task progress."""
    if task_id not in tasks:
        await websocket.close(code=4004, reason="Task not found")
        return

    await websocket.accept()
    task = tasks[task_id]
    task._ws_clients.append(websocket)

    # Send current state
    await websocket.send_json({"type": "state", "task": task.to_dict()})

    try:
        while True:
            # Keep connection alive, listen for client messages
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        task._ws_clients.remove(websocket)


# ─── Direct tool endpoints ──────────────────────────────────────────────────

@app.post("/tools/browser/navigate")
async def tool_browser_navigate(req: ToolRequest):
    """Navigate Brave to a URL."""
    cdp = CDP()
    await cdp.connect()
    result = await cdp.navigate(req.args.get("url", ""))
    await cdp.close()
    return {"result": result}


@app.post("/tools/browser/snapshot")
async def tool_browser_snapshot():
    """Get current page elements."""
    cdp = CDP()
    await cdp.connect()
    result = await cdp.snapshot()
    await cdp.close()
    return {"result": result}


@app.post("/tools/browser/click")
async def tool_browser_click(req: ToolRequest):
    """Click a page element by UID."""
    cdp = CDP()
    await cdp.connect()
    result = await cdp.click(str(req.args.get("uid", "")))
    await cdp.close()
    return {"result": result}


@app.post("/tools/browser/js")
async def tool_browser_js(req: ToolRequest):
    """Execute JavaScript on the page."""
    cdp = CDP()
    await cdp.connect()
    result = await cdp.js(req.args.get("code", ""))
    await cdp.close()
    return {"result": result}


@app.post("/tools/macos/open")
async def tool_macos_open(req: ToolRequest):
    """Open a macOS application."""
    result = await MacOS.open_app(req.args.get("path_or_name", req.args.get("name", "")))
    return {"result": result}


@app.post("/tools/macos/close")
async def tool_macos_close(req: ToolRequest):
    """Close a macOS application."""
    result = await MacOS.close_app(req.args.get("name", ""))
    return {"result": result}


@app.post("/tools/macos/applescript")
async def tool_macos_applescript(req: ToolRequest):
    """Run AppleScript."""
    result = await MacOS.applescript(req.args.get("code", ""))
    return {"result": result}


@app.post("/tools/macos/command")
async def tool_macos_command(req: ToolRequest):
    """Run a shell command."""
    result = await MacOS.run_command(req.args.get("command", ""))
    return {"result": result}


@app.post("/tools/record/start")
async def tool_record_start(req: ToolRequest):
    """Start screen recording."""
    duration = req.args.get("duration")
    if duration:
        result = await Recorder.start_timed(int(duration))
    else:
        result = await Recorder.start()
    return {"result": result}


@app.post("/tools/record/stop")
async def tool_record_stop():
    """Stop screen recording."""
    result = await Recorder.stop()
    return {"result": result}


# ─── Main ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    print("╔═══════════════════════════════════════════════╗")
    print("║  Orchestrator API                             ║")
    print("║  Browser + macOS + Recording + Local AI       ║")
    print("║  All tools, one endpoint                      ║")
    print("╚═══════════════════════════════════════════════╝")

    uvicorn.run(app, host="0.0.0.0", port=API_PORT, log_level="info")
