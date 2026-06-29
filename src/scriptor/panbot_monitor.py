"""Panbot monitoring dashboard — WebSocket server + HTML UI."""

import asyncio
import json
import threading
import time
import logging
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
from aiohttp import web

logger = logging.getLogger("panbot.monitor")

DEFAULT_PORT = 8417


class PanbotMonitor:
    """WebSocket server that broadcasts daemon state to browser clients."""

    def __init__(self, port: int = DEFAULT_PORT):
        self.port = port
        self._clients: set[web.WebSocketResponse] = set()
        self._state = {
            "status": "starting",
            "uptime_start": time.time(),
            "last_change": None,
            "last_ocr": [],
            "last_instruction": "",
            "versions": [],
            "processing_steps": [],
            "errors": [],
            "poll_interval": None,
            "rate_limited": False,
            "rate_limit_backoff": 0,
            "project_name": None,
            "folder_path": None,
        }
        self._app = web.Application()
        self._app.router.add_get("/", self._serve_dashboard)
        self._app.router.add_get("/ws", self._websocket_handler)
        self._app.router.add_get("/api/state", self._api_state)
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._process_callback = None  # callable(text: str) -> str | None

    def _load_html(self) -> str:
        try:
            tmpl = Path(__file__).parent / "templates" / "panbot-monitor.html"
            return tmpl.read_text(encoding="utf-8")
        except Exception:
            return "<html><body><h1>Panbot Monitor</h1><p>Template not found</p></body></html>"

    async def _serve_dashboard(self, request: web.Request) -> web.Response:
        return web.Response(text=self._load_html(), content_type="text/html")

    async def _api_state(self, request: web.Request) -> web.Response:
        state = dict(self._state)
        state["uptime"] = int(time.time() - state["uptime_start"])
        state["connected_clients"] = len(self._clients)
        return web.json_response(state)

    async def _websocket_handler(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._clients.add(ws)
        logger.info(f"Monitor client connected ({len(self._clients)} total)")

        # Send current state on connect
        await ws.send_json({
            "type": "state",
            **self._state,
            "uptime": int(time.time() - self._state["uptime_start"]),
        })

        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except Exception:
                        continue
                    if data.get("type") == "chat":
                        text = str(data.get("text", "")).strip()
                        if text:
                            await self._handle_chat(ws, text)
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    break
        finally:
            self._clients.discard(ws)
            logger.info(f"Monitor client disconnected ({len(self._clients)} total)")

        return ws

    async def _handle_chat(self, ws: web.WebSocketResponse, text: str) -> None:
        """Process a chat message from a connected client."""
        await self._broadcast({"type": "chat_echo", "text": text,
                               "timestamp": datetime.now(timezone.utc).strftime("%H:%M:%S")})
        if self._process_callback is not None:
            try:
                loop = asyncio.get_event_loop()
                response = await loop.run_in_executor(None, self._process_callback, text)
                reply = response if response else "Done."
            except Exception as e:
                reply = f"Error: {e}"
        else:
            reply = "No processing callback registered."
        await self._broadcast({"type": "chat_response", "text": reply,
                               "timestamp": datetime.now(timezone.utc).strftime("%H:%M:%S")})

    def emit(self, event: dict) -> None:
        """Broadcast an event to all connected WebSocket clients.

        Thread-safe — can be called from the daemon's sync polling thread.
        """
        event.setdefault("timestamp", datetime.now(timezone.utc).strftime("%H:%M:%S"))

        # Update internal state
        etype = event.get("type")
        if etype == "status":
            self._state["status"] = event.get("state", "unknown")
        elif etype == "change_detected":
            self._state["last_change"] = event
            self._state["status"] = "change_detected"
        elif etype == "ocr_result":
            self._state["last_ocr"] = event.get("pages", [])
            self._state["last_instruction"] = event.get("instruction", "")
        elif etype == "processing":
            step = event.get("step", "")
            if step == "start":
                self._state["processing_steps"] = []
                self._state["status"] = "processing"
            elif step == "done":
                self._state["status"] = "idle"
            self._state["processing_steps"].append(event)
        elif etype == "version":
            self._state["versions"].append(event)
            # Keep last 10
            self._state["versions"] = self._state["versions"][-10:]
        elif etype == "error":
            self._state["errors"].append(event)
            self._state["errors"] = self._state["errors"][-10:]
        elif etype == "poll_interval":
            self._state["poll_interval"] = event.get("interval")
        elif etype == "rate_limited":
            self._state["rate_limited"] = True
            self._state["rate_limit_backoff"] = event.get("backoff", 0)
        elif etype == "rate_limit_cleared":
            self._state["rate_limited"] = False
            self._state["rate_limit_backoff"] = 0
        elif etype == "config":
            if "project_name" in event:
                self._state["project_name"] = event["project_name"]
            if "folder_path" in event:
                self._state["folder_path"] = event["folder_path"]

        # Broadcast to clients
        if self._loop and self._clients:
            asyncio.run_coroutine_threadsafe(
                self._broadcast(event), self._loop
            )

    async def _broadcast(self, event: dict) -> None:
        dead = set()
        for ws in self._clients:
            try:
                await ws.send_json(event)
            except Exception:
                dead.add(ws)
        self._clients -= dead

    def start_background(self) -> None:
        """Start the monitor server in a background thread. Survives port conflicts."""
        def run():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            runner = web.AppRunner(self._app)
            self._loop.run_until_complete(runner.setup())

            # Try to bind, retry if port in use
            for attempt in range(5):
                try:
                    site = web.TCPSite(runner, "0.0.0.0", self.port)
                    self._loop.run_until_complete(site.start())
                    logger.info(f"Monitor dashboard: http://localhost:{self.port}")
                    self._loop.run_forever()
                    return
                except OSError as e:
                    if "address already in use" in str(e).lower():
                        logger.warning(f"Port {self.port} busy, retrying in {5*(attempt+1)}s")
                        time.sleep(5 * (attempt + 1))
                    else:
                        raise
            logger.error(f"Could not bind port {self.port} after 5 attempts")

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)
