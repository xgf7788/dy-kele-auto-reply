"""Lightweight health-check HTTP server for the Auto-Reply Bot.

Run alongside the main bot to expose monitoring endpoints.
Usage:
    from core.health_server import HealthServer
    health = HealthServer(bot, port=8899)
    await health.start()
    ...
    await health.stop()
"""
import asyncio
import json
import time
from datetime import datetime
from typing import Optional

from utils.logger import get_logger

logger = get_logger(__name__)

HEALTH_TEMPLATE = """<!DOCTYPE html>
<html lang="zh">
<head>
    <meta charset="utf-8">
    <title>DY Kele Bot – Status</title>
    <meta http-equiv="refresh" content="30">
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
               max-width: 720px; margin: 40px auto; padding: 0 20px;
               background: #f5f5f5; color: #333; }
        h1 { color: #1a1a1a; }
        .card { background: #fff; border-radius: 8px; padding: 16px 20px;
                margin: 12px 0; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
        .ok { color: #22c55e; } .warn { color: #f59e0b; } .err { color: #ef4444; }
        .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 12px; }
        .stat { text-align: center; }
        .stat .num { font-size: 2rem; font-weight: 700; }
        .stat .label { font-size: 0.85rem; color: #666; }
        table { width: 100%%; border-collapse: collapse; }
        th, td { padding: 8px 12px; text-align: left; border-bottom: 1px solid #eee; }
        th { color: #666; font-weight: 600; font-size: 0.85rem; }
    </style>
</head>
<body>
    <h1>🤖 DY Kele Auto-Reply Bot</h1>
    <p>Uptime: {uptime} &nbsp;|&nbsp; Last update: {now}</p>

    <div class="card">
        <h3>Stores</h3>
        <div class="grid">
            <div class="stat"><div class="num {online_cls}">{online}</div><div class="label">Online</div></div>
            <div class="stat"><div class="num">{offline}</div><div class="label">Offline</div></div>
            <div class="stat"><div class="num {error_cls}">{error}</div><div class="label">Error</div></div>
        </div>
    </div>

    <div class="card">
        <h3>Messages (Session)</h3>
        <div class="grid">
            <div class="stat"><div class="num">{received}</div><div class="label">Received</div></div>
            <div class="stat"><div class="num">{replied}</div><div class="label">Replied</div></div>
            <div class="stat"><div class="num">{failed}</div><div class="label">Failed</div></div>
            <div class="stat"><div class="num">{skipped}</div><div class="label">Skipped</div></div>
        </div>
    </div>

    <div class="card">
        <h3>Stores Detail</h3>
        <table>
            <tr><th>Store</th><th>Status</th><th>Msgs</th><th>Replies</th></tr>
            {store_rows}
        </table>
    </div>
</body>
</html>"""


class HealthServer:
    """Tiny async HTTP server for health checks and monitoring.

    Uses only asyncio (no extra deps) — starts a raw TCP server
    that responds to GET /health and GET / requests.
    """

    def __init__(self, bot, port: int = 8899, host: str = "0.0.0.0"):
        self._bot = bot
        self._port = port
        self._host = host
        self._server: Optional[asyncio.AbstractServer] = None
        self._start_time: Optional[float] = None

    async def start(self) -> None:
        """Start the health server."""
        self._start_time = time.time()
        self._server = await asyncio.start_server(
            self._handle_request, self._host, self._port
        )
        logger.info(f"Health server listening on http://{self._host}:{self._port}")

    async def stop(self) -> None:
        """Stop the health server."""
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
            logger.info("Health server stopped")

    async def _handle_request(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Handle a single HTTP request."""
        try:
            request = await asyncio.wait_for(reader.read(4096), timeout=5.0)
            request_text = request.decode('utf-8', errors='replace')

            if 'GET /health' in request_text or 'GET /api/health' in request_text:
                await self._send_json(writer, self._health_json())
            elif 'GET /stats' in request_text or 'GET /api/stats' in request_text:
                await self._send_json(writer, self._stats_json())
            elif 'GET / ' in request_text or 'GET /status' in request_text:
                await self._send_html(writer, self._status_html())
            else:
                await self._send_response(writer, 404, "Not Found", content_type="text/plain")
        except asyncio.TimeoutError:
            pass
        except Exception as e:
            logger.warning(f"Health server request error: {e}")
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    def _health_json(self) -> dict:
        store_health = {"online": 0, "offline": 0, "error": 0}
        bot = self._bot
        if bot and bot.store_manager:
            for store in bot.store_manager.get_all_stores():
                if store.is_online:
                    store_health["online"] += 1
                elif store.status.value == "offline":
                    store_health["offline"] += 1
                else:
                    store_health["error"] += 1

        return {
            "status": "ok" if store_health["online"] > 0 else "degraded",
            "uptime_seconds": int(time.time() - self._start_time) if self._start_time else 0,
            "stores": store_health,
            "timestamp": datetime.now().isoformat(),
        }

    def _stats_json(self) -> dict:
        result = self._health_json()
        bot = self._bot
        if bot and bot.message_handler:
            hs = bot.message_handler.get_stats()
            result["messages"] = {
                "received": hs.get("total_received", 0),
                "replied": hs.get("total_replied", 0),
                "failed": hs.get("total_failed", 0),
                "skipped": hs.get("total_skipped", 0),
                "avg_response_ms": hs.get("avg_response_time_ms", 0),
                "active_workers": hs.get("active_user_workers", 0),
            }
        return result

    def _status_html(self) -> str:
        bot = self._bot
        uptime = ""
        if self._start_time:
            secs = int(time.time() - self._start_time)
            h, m, s = secs // 3600, (secs % 3600) // 60, secs % 60
            uptime = f"{h}h {m}m {s}s"

        stores = []
        online = offline = error = 0
        if bot and bot.store_manager:
            for store in bot.store_manager.get_all_stores():
                s = {
                    "status": store.status.value,
                    "messages": store.total_messages,
                    "replies": store.total_replies,
                }
                stores.append((store.config.name, s))
                if store.is_online:
                    online += 1
                elif store.status.value == "offline":
                    offline += 1
                else:
                    error += 1

        hs = {}
        if bot and bot.message_handler:
            hs = bot.message_handler.get_stats()

        online_cls = "ok" if online > 0 else "err"
        error_cls = "err" if error > 0 else "ok"

        store_rows = ""
        for name, s in stores:
            cls = "ok" if s["status"] == "online" else ("warn" if s["status"] == "connecting" else "err")
            store_rows += (
                f'<tr><td>{name}</td>'
                f'<td class="{cls}">{s["status"]}</td>'
                f'<td>{s["messages"]}</td>'
                f'<td>{s["replies"]}</td></tr>\n'
            )

        return HEALTH_TEMPLATE.format(
            uptime=uptime,
            now=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            online=online, offline=offline, error=error,
            online_cls=online_cls, error_cls=error_cls,
            received=hs.get("total_received", 0),
            replied=hs.get("total_replied", 0),
            failed=hs.get("total_failed", 0),
            skipped=hs.get("total_skipped", 0),
            store_rows=store_rows,
        )

    @staticmethod
    async def _send_response(writer, status: int, body: str, content_type: str = "text/plain"):
        resp = (
            f"HTTP/1.1 {status} OK\r\n"
            f"Content-Type: {content_type}; charset=utf-8\r\n"
            f"Content-Length: {len(body.encode('utf-8'))}\r\n"
            f"Connection: close\r\n"
            f"\r\n"
            f"{body}"
        )
        writer.write(resp.encode('utf-8'))
        await writer.drain()

    async def _send_json(self, writer, data: dict):
        body = json.dumps(data, ensure_ascii=False, indent=2)
        await self._send_response(writer, 200, body, "application/json")

    async def _send_html(self, writer, html: str):
        await self._send_response(writer, 200, html, "text/html")
