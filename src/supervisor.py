#!/usr/bin/env python3
"""Self-supervising process wrapper - runs main() in a child process and restarts it if it
ever exits abnormally, including a native segfault in the GStreamer pipeline (seen live
under rapid client connect/disconnect/reconnect), which cannot be caught or recovered from
within the same process. The stream must survive that, not go down with it.
"""
from __future__ import annotations

import signal
import subprocess
import sys
import time
from pathlib import Path

from logutil import log


def supervise() -> int:
    child_argv = [sys.executable, str(Path(sys.argv[0]).resolve()), "--worker", *sys.argv[1:]]
    state = {"child": None, "shutting_down": False}

    def _forward_signal(signum, _frame) -> None:
        state["shutting_down"] = True
        if state["child"] is not None:
            log("[supervisor] Received termination signal, shutting down worker...")

            # 1. Ask the worker process politely to stop via SIGTERM
            state["child"].terminate()

            try:
                # 2. Lower the wait barrier from 5 seconds down to 1 second
                state["child"].wait(timeout=1.0)
                log("[supervisor] Worker shut down cleanly.")
            except subprocess.TimeoutExpired:
                # 3. If it hangs, violently terminate the process immediately
                log("[supervisor] Worker process failed to respond to SIGTERM. Issuing hard SIGKILL...")
                state["child"].kill()
                state["child"].wait()  # Clean up the zombie process state instantly

            # 4. Exit the parent process immediately so Kubernetes sees a clean 0 code
            sys.exit(0)

    signal.signal(signal.SIGTERM, _forward_signal)
    signal.signal(signal.SIGINT, _forward_signal)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, signal.SIG_IGN)

    backoff = 1.0
    while True:
        start = time.time()
        state["child"] = subprocess.Popen(child_argv)
        try:
            code = state["child"].wait()
        except KeyboardInterrupt:
            state["child"].terminate()
            state["child"].wait()
            raise
        if state["shutting_down"] or code == 0:
            return 0
        if code == 2:
            # argparse's standard "bad usage" exit code - a deterministic config error, not
            # a crash. Restarting would just loop forever (every retry hits the same bad
            # args) and look like a hang. Propagate it once instead.
            return code
        ran_for = time.time() - start
        log(f"[supervisor] worker exited abnormally (code={code}) after {ran_for:.1f}s - restarting")
        # Reset the backoff once the worker has proven it can run for a while - only a
        # crash-loop right at startup should be slowed down, not a rare crash after hours.
        backoff = 1.0 if ran_for > 30 else min(backoff * 2, 30.0)
        time.sleep(backoff)
