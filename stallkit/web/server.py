"""A local web front end for stallkit, on the seller's own machine.

Same idea as the desktop window and the same relationship to the commands: a browser
page builds the argument list a person would have typed, and `desktop.runner.run_cli`
runs it. What a command validates, refuses and prints is therefore identical in the
terminal, the window and the browser — one implementation, three surfaces.

WHY THIS IS NOT A HOSTED SERVICE
--------------------------------
It binds to 127.0.0.1 only, and it is the same process that already holds the seller's
Etsy keys. That makes it a browser-reachable surface in front of live credentials, so it
is defended like one:

* **A token in the URL.** Printed once at startup and required on every request. Without
  it the server answers 403 and serves nothing — so another program on the machine, or a
  page in another tab, cannot drive it.
* **A custom header on every API call**, which a cross-site page cannot send without a
  CORS preflight this server never approves. That is what closes CSRF: the token cookie
  alone would let any page POST to it.
* **A Host check.** A DNS name pointed at 127.0.0.1 is how a remote page reaches a local
  server; only `localhost` and `127.0.0.1` are answered.

Nothing is sent anywhere except Etsy, exactly as with the terminal.
"""

from __future__ import annotations

import http.server
import json
import mimetypes
import os
import secrets
import socket
import socketserver
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .. import __version__
from ..desktop import i18n, settings
from ..errors import StallKitError
from .jobs import JobRunner

STATIC_DIR = Path(__file__).parent / "static"

# Deliberately not 8000, 8080 or 3000: this is a long-lived local service and a port
# collision with whatever else the seller runs is a support question nobody wants.
DEFAULT_PORT = 8479

TOKEN_HEADER = "X-Stallkit-Token"
COOKIE_NAME = "stallkit_web"

# A browser holds a request open waiting for the next line of output. Long under a
# command that is thinking, but short enough that a proxy or a sleeping laptop does not
# leave the page looking stalled.
POLL_SECONDS = 25.0

ALLOWED_HOSTS = {"localhost", "127.0.0.1", "[::1]"}


@dataclass
class WebApp:
    """The state one running server owns."""

    token: str
    runner: JobRunner
    port: int = DEFAULT_PORT

    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/?token={self.token}"


# --- the API ---------------------------------------------------------------------


def _state(*, busy: bool = False) -> dict[str, Any]:
    """Everything the page needs to draw itself, with no command run.

    Read-only and cheap on purpose: it touches files, never Etsy. Whether the shop is
    actually connected is what `auth status` answers, and that is a job like any other —
    guessing it here would mean two answers to one question.
    """
    from .. import shops
    from ..config import home_dir
    from ..design.providers import DesignConfig
    from ..drop import workspace as workspace_mod

    prefs = settings.load_shop_prefs()
    app_prefs = settings.load_app_prefs()

    keystring = settings.current("ETSY_KEYSTRING")
    try:
        design = DesignConfig.load()
        provider = design.provider
    except StallKitError:
        # Two provider keys and no choice between them. The design tab says so when the
        # command runs; the page must still draw.
        provider = "ambiguous"

    return {
        "version": __version__,
        "language": app_prefs.get("language") or i18n.detect_language(),
        "anonymise": bool(app_prefs.get("anonymise", False)),
        # `name` is the Etsy shop name and is empty until the shop has been connected
        # once, so the page falls back to "Shop 1" the same way the window does.
        "shops": [
            {"id": shop.id, "name": shop.name, "index": index + 1, "connected": shop.connected}
            for index, shop in enumerate(shops.all_shops())
        ],
        "shop": shops.current().id,
        "shop_name": shops.current().name,
        "connected": shops.current().connected,
        "home": str(home_dir()),
        "env_path": str(settings.env_path()),
        "keys": {
            # Never the values. A page that echoes a shared secret back into the DOM
            # puts it in every screenshot of this window.
            "keystring": _masked(keystring),
            "has_keystring": bool(keystring),
            "has_secret": bool(settings.current("ETSY_SHARED_SECRET")),
            "redirect_uri": settings.current(
                "ETSY_REDIRECT_URI", settings.ETSY_REDIRECT_DEFAULT
            ),
            "has_pinterest": bool(settings.current("PINTEREST_APP_ID")),
        },
        "design_provider": provider,
        "workspace": prefs.get("workspace") or str(workspace_mod.default_root()),
        "prefs": {
            key: prefs.get(key, "")
            for key in ("template_listing", "push_csv", "ship_csv", "country", "inventory_from")
        },
        "busy": busy,
    }


def _masked(value: str) -> str:
    """Enough of the keystring to recognise it, never enough to use it."""
    if not value:
        return ""
    return f"{value[:6]}…" if len(value) > 8 else "…"


def _save_keys(payload: dict[str, Any]) -> dict[str, Any]:
    """Write the credentials the page was given into the current shop's .env.

    A half credential is refused here rather than saved: Etsy needs both halves in the
    x-api-key header, and a file holding only the keystring fails every call with a 403
    whose text does not obviously mean "you forgot the other half".
    """
    keystring = str(payload.get("keystring") or "").strip()
    secret = str(payload.get("shared_secret") or "").strip()
    redirect = str(payload.get("redirect_uri") or "").strip()

    from ..config import split_credential

    keystring, secret = split_credential(keystring, secret)
    updates: dict[str, str] = {}
    if keystring or secret:
        if not (keystring and secret):
            raise ValueError(
                "Etsy needs both halves of the app credential. Paste the keystring and "
                "the shared secret together."
            )
        updates["ETSY_KEYSTRING"] = keystring
        updates["ETSY_SHARED_SECRET"] = secret
    if redirect:
        updates["ETSY_REDIRECT_URI"] = redirect
    for key in ("PINTEREST_APP_ID", "PINTEREST_APP_SECRET"):
        if key.lower() in payload:
            updates[key] = str(payload[key.lower()] or "").strip()
    if not updates:
        raise ValueError("Nothing to save.")
    path = settings.save(updates)
    return {"saved": str(path)}


def _save_prefs(payload: dict[str, Any]) -> dict[str, Any]:
    """Remember a folder or a file the page was pointed at. Nothing secret lives here."""
    shop_keys = {
        "workspace", "template_listing", "push_csv", "ship_csv", "country", "inventory_from",
    }
    app_keys = {"language", "anonymise"}

    shop_prefs = settings.load_shop_prefs()
    app_prefs = settings.load_app_prefs()
    for key, value in payload.items():
        if key in shop_keys:
            shop_prefs[key] = str(value or "")
        elif key in app_keys:
            app_prefs[key] = value
    settings.save_shop_prefs(shop_prefs)
    settings.save_app_prefs(app_prefs)
    return {"ok": True}


def _browse(payload: dict[str, Any]) -> dict[str, Any]:
    """List one directory, so the page can pick a folder without a native dialog.

    A browser cannot open a file picker that returns a *path* — only file contents — and
    every command here takes paths. So the page gets a directory listing instead, from
    this process, which already reads and writes those files anyway.
    """
    raw = str(payload.get("path") or "").strip()
    base = Path(raw).expanduser() if raw else Path.home()
    try:
        base = base.resolve()
    except OSError:
        base = Path.home()
    if not base.is_dir():
        base = base.parent if base.parent.is_dir() else Path.home()

    entries = []
    try:
        for child in sorted(base.iterdir(), key=lambda p: p.name.casefold()):
            if child.name.startswith("."):
                continue
            try:
                is_dir = child.is_dir()
            except OSError:
                continue
            entries.append({"name": child.name, "path": str(child), "dir": is_dir})
    except OSError as exc:
        return {"path": str(base), "error": str(exc), "entries": []}
    return {
        "path": str(base),
        "parent": str(base.parent) if base.parent != base else "",
        "entries": entries[:400],
    }


# --- the HTTP layer ---------------------------------------------------------------


class _Handler(http.server.BaseHTTPRequestHandler):
    server_version = f"stallkit/{__version__}"
    protocol_version = "HTTP/1.1"
    app: WebApp

    # A browser opens speculative connections to localhost and sends nothing on some of
    # them. Without a timeout each one holds a thread until the tab is closed.
    timeout = 60

    def log_message(self, *_args: Any) -> None:
        """Silence the default stderr access log; the page shows what ran."""

    # --- helpers ------------------------------------------------------------------

    def _json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _bytes(self, status: int, body: bytes, content_type: str, *, cookie: str = "") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # This page holds credentials and runs commands; nothing may frame or embed it.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def _host_ok(self) -> bool:
        """Reject a request that arrived under any name but this machine's own.

        A public DNS name resolving to 127.0.0.1 is how a remote page reaches a local
        server, and the browser sends its own Host, not ours.
        """
        host = (self.headers.get("Host") or "").strip()
        name = host.rsplit(":", 1)[0] if host.count(":") == 1 or host.startswith("[") else host
        return name.lower() in ALLOWED_HOSTS

    def _cookie_token(self) -> str:
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            key, _, value = part.strip().partition("=")
            if key == COOKIE_NAME:
                return value
        return ""

    def _authorised(self, query: dict[str, list[str]], *, api: bool) -> bool:
        wanted = self.app.token
        if api:
            # The header is the CSRF defence: a cross-origin page cannot set it without
            # a preflight this server never answers, so the cookie alone is not enough.
            return secrets.compare_digest(self.headers.get(TOKEN_HEADER) or "", wanted)
        given = (query.get("token") or [""])[0] or self._cookie_token()
        return secrets.compare_digest(given, wanted)

    def _body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0:
            return {}
        raw = self.rfile.read(min(length, 1 << 20))
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    # --- routing ------------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 — name fixed by BaseHTTPRequestHandler
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        path = parsed.path.rstrip("/") or "/"

        if not self._host_ok():
            self._json(421, {"error": "This server only answers on localhost."})
            return

        is_api = path.startswith("/api/")
        if not self._authorised(query, api=is_api):
            self._deny(is_api)
            return

        if path == "/":
            self._serve_page()
            return
        if path.startswith("/static/"):
            self._serve_static(path[len("/static/") :])
            return
        if path == "/api/state":
            self._json(200, _state(busy=self.app.runner.busy()))
            return
        if path == "/api/i18n":
            self._json(200, {"languages": i18n.LANGUAGES, "strings": i18n.STRINGS})
            return
        if path.startswith("/api/jobs/"):
            self._serve_job(path[len("/api/jobs/") :], query)
            return
        self._json(404, {"error": "No such path."})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if not self._host_ok():
            self._json(421, {"error": "This server only answers on localhost."})
            return
        if not self._authorised({}, api=True):
            self._deny(True)
            return

        payload = self._body()
        try:
            if path == "/api/jobs":
                self._start_job(payload)
                return
            if path.startswith("/api/jobs/") and path.endswith("/answer"):
                job_id = path[len("/api/jobs/") : -len("/answer")]
                job = self.app.runner.get(job_id)
                if job is None:
                    self._json(404, {"error": "That command is no longer here."})
                    return
                job.answer(str(payload.get("text") or ""))
                self._json(200, job.snapshot())
                return
            if path == "/api/keys":
                self._json(200, _save_keys(payload))
                return
            if path == "/api/prefs":
                self._json(200, _save_prefs(payload))
                return
            if path == "/api/shop":
                shop = settings.use_shop(str(payload.get("shop") or ""))
                app_prefs = settings.load_app_prefs()
                app_prefs["shop"] = shop.id
                settings.save_app_prefs(app_prefs)
                self._json(200, _state(busy=self.app.runner.busy()))
                return
            if path == "/api/browse":
                self._json(200, _browse(payload))
                return
        except (ValueError, StallKitError) as exc:
            self._json(400, {"error": str(exc)})
            return
        except OSError as exc:
            self._json(500, {"error": str(exc)})
            return
        self._json(404, {"error": "No such path."})

    def _deny(self, is_api: bool) -> None:
        if is_api:
            self._json(403, {"error": "Missing or wrong token."})
            return
        body = (
            b"<!doctype html><meta charset=utf-8><title>stallkit</title>"
            b"<div style='font:16px/1.6 system-ui,sans-serif;max-width:32rem;"
            b"margin:14vh auto;padding:0 1.5rem'>"
            b"<h1 style='font-size:1.3rem'>Open the address stallkit printed</h1>"
            b"<p style='color:#555'>This page needs the one-time token from the terminal "
            b"where <code>stallkit web</code> is running. Copy the whole address, "
            b"including <code>?token=</code>.</p>"
            b"<p style='color:#555'>Bu sayfa, <code>stallkit web</code> komutunun "
            b"yazd\xc4\xb1\xc4\x9f\xc4\xb1 tek kullan\xc4\xb1ml\xc4\xb1k anahtara "
            b"ihtiya\xc3\xa7 duyar. Adresin tamam\xc4\xb1n\xc4\xb1, "
            b"<code>?token=</code> dahil, kopyala.</p></div>"
        )
        self._bytes(403, body, "text/html; charset=utf-8")

    def _serve_page(self) -> None:
        try:
            html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        except OSError:
            self._json(500, {"error": "The web assets are missing from this install."})
            return
        # The token reaches the page's JavaScript here, and only here. It is also set as
        # a strict same-site cookie so a reload without the query string still works.
        html = html.replace("__STALLKIT_TOKEN__", self.app.token)
        cookie = (
            f"{COOKIE_NAME}={self.app.token}; Path=/; HttpOnly; SameSite=Strict"
        )
        self._bytes(200, html.encode("utf-8"), "text/html; charset=utf-8", cookie=cookie)

    def _serve_static(self, name: str) -> None:
        # Decode first, then confine. A browser normalises `../` out of a path before
        # sending it, but `%2e%2e%2f` arrives intact — so the escape has to be caught
        # after unquoting rather than prevented by not unquoting.
        target = (STATIC_DIR / urllib.parse.unquote(name)).resolve()
        try:
            target.relative_to(STATIC_DIR.resolve())
        except ValueError:
            self._json(403, {"error": "Not in the asset folder."})
            return
        if not target.is_file():
            self._json(404, {"error": "No such asset."})
            return
        kind = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if kind.startswith("text/") or kind == "application/javascript":
            kind = f"{kind}; charset=utf-8"
        self._bytes(200, target.read_bytes(), kind)

    def _start_job(self, payload: dict[str, Any]) -> None:
        args = payload.get("args")
        if not isinstance(args, list) or not args or not all(isinstance(a, str) for a in args):
            self._json(400, {"error": "A command is a non-empty list of strings."})
            return
        if self.app.runner.busy():
            # Serial by design; see jobs.py. Saying so beats queueing silently, because
            # the person is watching a log and would read the wrong command's output.
            self._json(
                409, {"error": "Something is still running. Wait for it to finish."}
            )
            return
        job = self.app.runner.submit(args, anonymise=bool(payload.get("anonymise")))
        self._json(202, job.snapshot())

    def _serve_job(self, job_id: str, query: dict[str, list[str]]) -> None:
        job = self.app.runner.get(job_id)
        if job is None:
            self._json(404, {"error": "That command is no longer here."})
            return
        try:
            since = max(0, int((query.get("since") or ["0"])[0]))
        except ValueError:
            since = 0
        if (query.get("wait") or [""])[0] in ("1", "true"):
            job.wait_for_change(since=since, timeout=POLL_SECONDS)
        self._json(200, job.snapshot(since=since))


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    # Bind without a reverse DNS lookup: HTTPServer.server_bind calls socket.getfqdn,
    # which on some Macs stalls ~30s before the port even opens. The same reason
    # auth.LoopbackServer exists.
    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


def build(port: int = DEFAULT_PORT, *, token: str = "") -> tuple[_Server, WebApp]:
    """Bind the server and return it unstarted, so a caller can print the URL first."""
    app = WebApp(token=token or secrets.token_urlsafe(24), runner=JobRunner(), port=port)
    handler = type("_BoundHandler", (_Handler,), {"app": app})
    try:
        server = _Server(("127.0.0.1", port), handler)
    except OSError as exc:
        app.runner.stop()
        # The likeliest holder of this port is another `stallkit web` from an earlier
        # terminal tab, and that one is still working — so say so before suggesting a
        # second copy. Starting one on another port is fine, but two servers driving one
        # shop is not what anybody wanted.
        raise StallKitError(
            f"Cannot listen on 127.0.0.1:{port} ({exc}).\n"
            "If you already have `stallkit web` running in another window, that one is "
            "still serving — switch to it and use the address it printed.\n"
            f"Otherwise something else holds the port: `stallkit web --port {port + 1}`."
        ) from exc
    app.port = server.server_address[1]
    return server, app


def serve(
    port: int = DEFAULT_PORT,
    *,
    open_browser: bool = True,
    announce: Callable[[WebApp], None] | None = None,
) -> None:
    """Run until interrupted. Blocks; the caller is a CLI command.

    `announce` runs only after the port is actually bound, and it is given the app so it
    can report the port that was taken rather than the one that was asked for. Nothing
    may claim to be serving before the bind: a green "serving on 8479" followed by
    "address already in use" is a worse first five minutes than the error alone.
    """
    server, app = build(port)
    # Everything past the bind is inside the cleanup, including announcing and opening
    # the browser: a failure there would otherwise leave the port held and the worker
    # thread alive, and the next `stallkit web` would report the port as taken by a
    # server nobody can reach.
    serving = False
    try:
        if announce:
            announce(app)
        if open_browser:
            import webbrowser

            try:
                webbrowser.open(app.url())
            except Exception:  # noqa: BLE001 — headless box: the printed URL is the fallback
                pass
        serving = True
        server.serve_forever(poll_interval=0.4)
    except KeyboardInterrupt:
        pass
    finally:
        # Only if serve_forever actually started. shutdown() waits on an event that
        # serve_forever sets, so calling it on a server that never served blocks forever.
        if serving:
            server.shutdown()
        server.server_close()
        app.runner.stop(wait=2.0)


def port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) != 0


def default_port() -> int:
    """STALLKIT_WEB_PORT wins, so a seller who moved it does not retype --port."""
    raw = (os.environ.get("STALLKIT_WEB_PORT") or "").strip()
    try:
        return int(raw) if raw else DEFAULT_PORT
    except ValueError:
        return DEFAULT_PORT


__all__ = ["DEFAULT_PORT", "WebApp", "build", "default_port", "port_is_free", "serve"]
