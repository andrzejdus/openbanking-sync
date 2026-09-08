"""Consent (SCA) flow.

PSD2 requires the account holder to authenticate at their own bank in a
browser; there is no headless path to a first consent. This module reduces
that to one click: it prints the bank's authorization URL, catches the
redirect on a local listener, and exchanges the code for an API session that
then works without a browser until the consent expires.
"""

from __future__ import annotations

import http.server
import socket
import threading
import webbrowser
from datetime import datetime
from urllib.parse import parse_qs, urlparse

CALLBACK_PAGE = b"""<!doctype html>
<meta charset="utf-8">
<title>Bank linked</title>
<style>
  body { font: 16px system-ui, sans-serif; margin: 4rem auto; max-width: 30rem;
         color: #1a1a1a; background: #fafafa; }
  code { background: #eee; padding: .1em .3em; border-radius: 3px; }
</style>
<h1>Consent captured</h1>
<p>You can close this tab and return to the terminal.</p>
"""


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    result: dict[str, str] = {}

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        query = parse_qs(urlparse(self.path).query)
        for key in ("code", "state", "error", "error_description"):
            if key in query:
                type(self).result[key] = query[key][0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(CALLBACK_PAGE)))
        self.end_headers()
        self.wfile.write(CALLBACK_PAGE)

    def log_message(self, *args: object) -> None:
        """Silence the default stderr access log."""


def is_local(redirect_url: str) -> bool:
    host = urlparse(redirect_url).hostname
    return host in {"localhost", "127.0.0.1", "::1"}


def await_redirect(redirect_url: str, timeout: int = 300) -> dict[str, str]:
    """Serve the redirect URL's path locally until the bank redirects to it."""
    parsed = urlparse(redirect_url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    _CallbackHandler.result = {}

    try:
        server = http.server.HTTPServer(("127.0.0.1", port), _CallbackHandler)
    except OSError as exc:
        raise RuntimeError(
            f"Cannot listen on 127.0.0.1:{port} for the redirect ({exc}). "
            f"Free the port, or pass --manual to paste the redirect URL instead."
        ) from exc

    server.timeout = 1
    deadline = threading.Event()

    def serve() -> None:
        while not deadline.is_set() and not _CallbackHandler.result:
            server.handle_request()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    thread.join(timeout)
    deadline.set()
    server.server_close()

    if not _CallbackHandler.result:
        raise TimeoutError(
            f"No redirect received on {redirect_url} within {timeout}s. "
            f"Re-run with --manual to paste the URL by hand."
        )
    return _CallbackHandler.result


def code_from_pasted_url(url: str) -> str:
    query = parse_qs(urlparse(url.strip()).query)
    if "error" in query:
        raise RuntimeError(f"Bank returned an error: {query['error'][0]}")
    if "code" not in query:
        raise RuntimeError(f"No `code` parameter in {url!r}")
    return query["code"][0]


def open_in_browser(url: str) -> bool:
    """Best effort — headless sessions simply print the URL instead."""
    try:
        return webbrowser.open(url)
    except Exception:
        return False


def port_is_free(redirect_url: str) -> bool:
    parsed = urlparse(redirect_url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) != 0


def describe_window(valid_until: datetime) -> str:
    days = (valid_until - datetime.now(valid_until.tzinfo)).days
    return f"{valid_until.date().isoformat()} ({days} days)"
