"""The browser front end: the same commands, reachable from a page on this machine.

Third surface after the terminal and the desktop window, and deliberately the thinnest
of the three. A request carries the argument list a person would have typed,
`desktop.runner.run_cli` runs it, and the page shows what came back. No command logic
lives here, so nothing can drift out of step with the other two.

It serves from 127.0.0.1 behind a one-time token, with no build step and no CDN — the
page renders identically with the network unplugged, which matters because the thing a
seller is most often doing here is fixing credentials.
"""

from __future__ import annotations

from .server import DEFAULT_PORT, WebApp, build, default_port, port_is_free, serve

__all__ = ["DEFAULT_PORT", "WebApp", "build", "default_port", "port_is_free", "serve"]
