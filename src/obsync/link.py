"""Consent (SCA) flow.

PSD2 requires the account holder to authenticate at their own bank in a
browser; there is no headless path to a first consent. This module reduces
that to one click: it prints the bank's authorization URL, catches the
redirect on a local listener, and exchanges the code for an API session that
then works without a browser until the consent expires.
"""

from __future__ import annotations

import datetime as dt
import http.server
import ipaddress
import socket
import ssl
import threading
import webbrowser
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

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


def ensure_tls_cert(config_dir: Path) -> tuple[Path, Path]:
    """Return (cert, key) for the local HTTPS callback, generating them once.

    Enable Banking rejects `http://` redirect URLs on production applications,
    so the loopback listener has to speak TLS. The certificate is self-signed
    and reused for its whole lifetime on purpose: the browser trusts it only
    after the user clicks through the warning once, and regenerating it would
    make them do that again at every consent renewal.
    """
    cert_path = config_dir / "callback-cert.pem"
    key_path = config_dir / "callback-key.pem"
    if cert_path.exists() and key_path.exists():
        return cert_path, key_path

    config_dir.mkdir(parents=True, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = dt.datetime.now(dt.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=3650))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )

    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o600)
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    cert_path.chmod(0o600)
    return cert_path, key_path


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


def is_https(redirect_url: str) -> bool:
    return urlparse(redirect_url).scheme == "https"


def is_local(redirect_url: str) -> bool:
    host = urlparse(redirect_url).hostname
    return host in {"localhost", "127.0.0.1", "::1"}


def await_redirect(
    redirect_url: str, timeout: int = 300, config_dir: Path | None = None
) -> dict[str, str]:
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

    if parsed.scheme == "https":
        cert_path, key_path = ensure_tls_cert(config_dir or Path.home())
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert_path, key_path)
        server.socket = context.wrap_socket(server.socket, server_side=True)

    server.timeout = 1
    deadline = threading.Event()

    def serve() -> None:
        while not deadline.is_set() and not _CallbackHandler.result:
            try:
                server.handle_request()
            except (ssl.SSLError, OSError):
                # The browser aborts the handshake until the user accepts the
                # self-signed certificate. Keep waiting rather than giving up.
                continue

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
