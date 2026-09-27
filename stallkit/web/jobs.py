"""Running stallkit commands for the browser, one at a time.

The third front end over the same commands, after the terminal and the window. Nothing
here re-implements a command: a request arrives with the argument list a person would
have typed, `desktop.runner.run_cli` executes it in-process, and its output is handed to
the browser as it appears.

SERIAL, FOR TWO SEPARATE REASONS
--------------------------------
`run_cli` redirects `sys.stdout` and swaps the CLI's Rich consoles process-wide, so two
commands overlapping would interleave their output into each other. And the commands
themselves share a token file a refresh rewrites and an upload lock a second `drop auto`
would trip over. The window solved this with one worker thread; so does this.

A browser tab can also be closed mid-command, which a terminal cannot. So a job outlives
its listener: the output is buffered, and reconnecting replays it from the start rather
than showing a command that appears to have done nothing.
"""

from __future__ import annotations

import queue
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from ..desktop import runner

# How long a command waits for an answer to a question nobody may be there to read.
# `auth login`'s paste step is the one that asks, and a person needs time to approve in
# Etsy first. On timeout the stream reports end-of-file, which every prompt treats as
# "cancel" — better than a worker thread wedged forever behind a closed tab.
ANSWER_TIMEOUT = 600.0

# Finished jobs are kept so a reconnecting tab can still read the outcome, and dropped
# oldest-first so a long-lived server does not grow without bound.
KEEP_FINISHED = 40


@dataclass
class Job:
    id: str
    args: list[str]
    # Every line this job has produced, in order. The replay buffer *is* the log: a
    # browser that reconnects gets the whole thing rather than the tail.
    lines: list[dict[str, str]] = field(default_factory=list)
    question: str = ""
    exit_code: int | None = None
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    cancelled: bool = False

    _answers: queue.Queue = field(default_factory=queue.Queue, repr=False)
    _changed: threading.Condition = field(
        default_factory=lambda: threading.Condition(threading.Lock()), repr=False
    )

    @property
    def done(self) -> bool:
        return self.exit_code is not None

    def snapshot(self, *, since: int = 0) -> dict[str, Any]:
        """Everything a browser needs to draw this job, from `since` lines onwards."""
        with self._changed:
            return {
                "id": self.id,
                "args": list(self.args),
                "lines": self.lines[since:],
                "total": len(self.lines),
                "question": self.question,
                "done": self.done,
                "exit_code": self.exit_code,
                "cancelled": self.cancelled,
            }

    def wait_for_change(self, *, since: int, timeout: float) -> None:
        """Block until there is something new, or the timeout expires."""
        with self._changed:
            if len(self.lines) > since or self.done:
                return
            self._changed.wait(timeout)

    def answer(self, text: str) -> None:
        self._answers.put(text)

    # --- used by the worker thread ------------------------------------------------

    def _emit(self, kind: str, text: str) -> None:
        with self._changed:
            self.lines.append({"kind": kind, "text": text})
            self._changed.notify_all()

    def _set(self, **fields: Any) -> None:
        with self._changed:
            for key, value in fields.items():
                setattr(self, key, value)
            self._changed.notify_all()

    def _ask(self, question: str) -> str | None:
        """Show a question in the browser and wait for the answer it types."""
        self._set(question=question or "Answer:")
        try:
            return self._answers.get(timeout=ANSWER_TIMEOUT)
        except queue.Empty:
            return None
        finally:
            self._set(question="")


class _Sink:
    """A write-only text stream that files each write under one kind, for the browser.

    `runner.LogStream` would do, but it writes onto a queue keyed for Tk's event loop.
    This keeps the job's own ordered buffer instead, which is what replay needs.
    """

    def __init__(self, job: Job, kind: str) -> None:
        self._job = job
        self._kind = kind
        self._partial = ""
        self._last_line = ""
        self._lock = threading.Lock()

    # Rich and click inspect these to decide how to write.
    @property
    def encoding(self) -> str:
        return "utf-8"

    @property
    def errors(self) -> str:
        return "replace"

    def writable(self) -> bool:
        return True

    def readable(self) -> bool:
        return False

    def seekable(self) -> bool:
        return False

    def isatty(self) -> bool:
        return False

    def fileno(self) -> int:
        raise OSError("not a real file")

    def write(self, text: str) -> int:
        # Refusing bytes is load-bearing: click probes a stream with write(b"") and, if
        # that succeeds, treats it as binary and wraps it in a second encoder.
        if not isinstance(text, str):
            raise TypeError(f"write() argument must be str, not {type(text).__name__}")
        if not text:
            return 0
        with self._lock:
            pending = self._partial + text
            complete, newline, self._partial = pending.rpartition("\n")
            if newline:
                for line in complete.split("\n"):
                    if line.strip():
                        self._last_line = line
                    self._job._emit(self._kind, line)
        return len(text)

    def flush(self) -> None:
        """Send a line a command left unterminated — that is where a prompt sits."""
        with self._lock:
            if self._partial.strip():
                self._job._emit(self._kind, self._partial)
                self._last_line = self._partial
                self._partial = ""

    def question(self) -> str:
        with self._lock:
            return self._partial.strip() or self._last_line.strip()


class JobRunner:
    """One worker thread, running submitted commands in the order they arrive."""

    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._pending: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._stopping = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="stallkit-web", daemon=True)
        self._thread.start()

    def submit(self, args: list[str], *, anonymise: bool = False) -> Job:
        job = Job(id=secrets.token_urlsafe(8), args=list(args))
        with self._lock:
            self.jobs[job.id] = job
            self._order.append(job.id)
            self._forget_old()
        self._pending.put((job, anonymise))
        return job

    def _forget_old(self) -> None:
        finished = [i for i in self._order if self.jobs[i].done]
        for job_id in finished[: max(0, len(finished) - KEEP_FINISHED)]:
            self.jobs.pop(job_id, None)
            self._order.remove(job_id)

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self.jobs.get(job_id)

    def busy(self) -> bool:
        with self._lock:
            return any(not job.done for job in self.jobs.values())

    def stop(self, wait: float = 0.0) -> None:
        self._stopping.set()
        self._pending.put(None)
        if wait:
            self._thread.join(wait)

    def _loop(self) -> None:
        while True:
            item = self._pending.get()
            if item is None:
                return
            job, anonymise = item
            if self._stopping.is_set():
                job._set(exit_code=130, cancelled=True, finished_at=time.time())
                continue
            self._run(job, anonymise=anonymise)

    def _run(self, job: Job, *, anonymise: bool) -> None:
        out, err = _Sink(job, "out"), _Sink(job, "err")
        stdin = runner.PromptStream(job._ask, out.question)
        import sys

        saved_stdin = sys.stdin
        sys.stdin = stdin  # type: ignore[assignment]
        try:
            code = runner.run_cli(job.args, out=out, err=err, anonymise=anonymise)
        except BaseException as exc:  # noqa: BLE001 — reported to the browser, not lost
            err.write(f"{type(exc).__name__}: {exc}\n")
            code = 1
        finally:
            sys.stdin = saved_stdin
            out.flush()
            err.flush()
            job._set(exit_code=code, finished_at=time.time(), question="")
