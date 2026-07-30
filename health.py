"""
health.py — tiny HTTP health endpoint for smuHBLogs.

Render's free tier only runs web services, so the bot must listen on the
PORT it provides; the same endpoint answers the external keep-alive pings
that stop the service from idling. Does nothing when PORT is unset (local
dev, VM deployments).
"""

import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logger = logging.getLogger(__name__)


class _HealthHandler(BaseHTTPRequestHandler):
    def _respond(self) -> None:
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 (http.server naming)
        self._respond()

    def do_HEAD(self) -> None:  # noqa: N802
        self.send_response(200)
        self.end_headers()

    def log_message(self, *_args) -> None:
        pass  # keep ping traffic out of the bot logs


def start_health_server() -> None:
    """Serve 200 OK on 0.0.0.0:$PORT in a daemon thread. No-op if PORT unset."""
    port_raw = os.getenv("PORT", "")
    if not port_raw:
        logger.info("PORT not set — health endpoint disabled.")
        return
    try:
        port = int(port_raw)
        server = ThreadingHTTPServer(("0.0.0.0", port), _HealthHandler)
    except (ValueError, OSError) as e:
        logger.error("Could not start health endpoint on PORT=%r: %s", port_raw, e)
        return
    threading.Thread(target=server.serve_forever, daemon=True).start()
    logger.info("Health endpoint listening on port %d.", port)
