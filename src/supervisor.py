#!/usr/bin/env python3
"""Self-supervising process wrapper - runs main() in a child process per camera and restarts
a child if it ever exits abnormally, including a native segfault in the GStreamer pipeline
(seen live under rapid client connect/disconnect/reconnect), which cannot be caught or
recovered from within the same process. The stream must survive that, not go down with it -
and one camera's crash must never take the other cameras down.
"""
from __future__ import annotations

import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional, Sequence

from logutil import log

_BAD_USAGE_EXIT_CODE = 2  # argparse's standard "bad usage" exit code


class _WorkerRunner:
    """Runs one worker process and restarts it after abnormal exits."""

    def __init__(self, label: str, argv: list[str], stop_all: threading.Event) -> None:
        self.label = label
        self.argv = argv
        self._stop_all = stop_all
        self._lock = threading.Lock()
        self._child: Optional[subprocess.Popen] = None
        self.exit_code = 0

    def run(self) -> None:
        backoff = 1.0
        while not self._stop_all.is_set():
            start = time.time()
            with self._lock:
                if self._stop_all.is_set():
                    return
                self._child = subprocess.Popen(self.argv)
            code = self._child.wait()
            if self._stop_all.is_set() or code == 0:
                return
            if code == _BAD_USAGE_EXIT_CODE:
                # A deterministic config error, not a crash. Restarting would just loop
                # forever (every retry hits the same bad args) and look like a hang.
                self.exit_code = code
                self._stop_all.set()
                return
            ran_for = time.time() - start
            log(f"[supervisor] {self.label} worker exited abnormally (code={code}) after {ran_for:.1f}s - restarting")
            # Reset the backoff once the worker has proven it can run for a while - only a
            # crash-loop right at startup should be slowed down, not a rare crash after hours.
            backoff = 1.0 if ran_for > 30 else min(backoff * 2, 30.0)
            self._stop_all.wait(backoff)

    def terminate(self) -> None:
        with self._lock:
            child = self._child
        if child is None or child.poll() is not None:
            return
        child.terminate()
        try:
            child.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            log(f"[supervisor] {self.label} worker did not respond to SIGTERM - issuing hard kill")
            child.kill()
            child.wait()


def supervise(worker_args: Optional[Sequence[tuple[str, list[str]]]] = None) -> int:
    """Run one supervised worker per entry of `worker_args` - (label, extra argv) pairs, each
    appended to this process's own argv - or a single worker with the unchanged argv when
    omitted. Returns 0 on a clean shutdown, or argparse's exit code 2 for a bad-usage worker."""
    entries = list(worker_args) if worker_args else [("camera", [])]
    base_argv = [sys.executable, str(Path(sys.argv[0]).resolve()), "--worker", *sys.argv[1:]]
    stop_all = threading.Event()
    runners = [_WorkerRunner(label, base_argv + extra, stop_all) for label, extra in entries]

    def _shutdown(_signum=None, _frame=None) -> None:
        if not stop_all.is_set():
            log("[supervisor] Received termination signal, shutting down workers...")
        stop_all.set()
        for runner in runners:
            runner.terminate()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, signal.SIG_IGN)

    threads = [threading.Thread(target=runner.run, name=f"supervisor-{runner.label}", daemon=True)
               for runner in runners]
    for thread in threads:
        thread.start()
    # join() with a timeout so the main thread keeps servicing signal handlers (SIGTERM/SIGINT).
    while any(thread.is_alive() for thread in threads):
        for thread in threads:
            thread.join(0.5)
        if stop_all.is_set():
            # Also covers a bad-usage worker stopping the others.
            for runner in runners:
                runner.terminate()

    return next((runner.exit_code for runner in runners if runner.exit_code), 0)
