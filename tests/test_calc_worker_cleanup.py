"""Verify calculator descendants are reaped at the isolated worker boundary."""

import json
from pathlib import Path
import subprocess
import sys
import unittest


EVAL = Path(__file__).resolve().parents[1] / "eval"

# The outer process is itself a subreaper so a broken worker cannot leak test
# grandchildren to the host's PID 1. Any descendants it adopts are a regression.
ORPHAN_PROBE = r'''
import ctypes
import io
import json
import os
import sys
import time

sys.path.insert(0, sys.argv[1])
from jobbench_eval import calc_worker

libc = ctypes.CDLL(None, use_errno=True)
if libc.prctl(36, 1, 0, 0, 0) != 0:
    raise OSError(ctypes.get_errno(), "cannot establish test subreaper")

fail = sys.argv[2] == "failure"
linger = sys.argv[2] == "linger"
worker_pid = os.fork()
if worker_pid == 0:
    def fake_calculate(path, request):
        launcher = os.fork()
        if launcher == 0:
            for delay in ((0.2,) if linger else (0, 0.02, 0.05)):
                if os.fork() == 0:
                    time.sleep(delay)
                    os._exit(0)
            os._exit(0)
        os.waitpid(launcher, 0)
        if fail:
            raise RuntimeError("fixture calculation failed after creating descendants")
        return {"status": "recalculated", "cells": {}}

    calc_worker.calculate = fake_calculate
    if linger:
        original_reap = calc_worker.reap_orphaned_children
        calc_worker.reap_orphaned_children = lambda: original_reap(timeout=0.03)
    sys.stdin = io.StringIO("{}")
    sys.stdout = io.StringIO()
    sys.argv = ["calc_worker.py", "unused.xlsx"]
    os._exit(calc_worker.main())

_, status = os.waitpid(worker_pid, 0)
adopted = []
while True:
    try:
        pid, _ = os.waitpid(-1, 0)
        adopted.append(pid)
    except ChildProcessError:
        break
print(json.dumps({"worker_exit": os.waitstatus_to_exitcode(status), "adopted": adopted}))
'''


@unittest.skipUnless(sys.platform.startswith("linux"), "requires Linux child subreapers")
class CalcWorkerCleanupTests(unittest.TestCase):
    def probe(self, mode):
        process = subprocess.run(
            [sys.executable, "-c", ORPHAN_PROBE, str(EVAL), mode],
            text=True, capture_output=True, timeout=10,
        )
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        return json.loads(process.stdout)

    def test_successful_worker_reaps_orphaned_descendants_before_exit(self):
        result = self.probe("success")
        self.assertEqual(result["worker_exit"], 0)
        self.assertEqual(result["adopted"], [], "worker leaked descendants to its parent")

    def test_failed_worker_reaps_orphaned_descendants_before_exit(self):
        result = self.probe("failure")
        self.assertEqual(result["worker_exit"], 2)
        self.assertEqual(result["adopted"], [], "failed worker leaked descendants to its parent")

    def test_worker_reports_failure_when_helpers_outlive_cleanup_deadline(self):
        result = self.probe("linger")
        self.assertEqual(result["worker_exit"], 2, "cleanup timeout must not report success")


if __name__ == "__main__":
    unittest.main()
