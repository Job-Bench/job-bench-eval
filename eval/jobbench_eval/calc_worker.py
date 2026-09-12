"""Isolated LibreOffice UNO worker; input/output are JSON, never judge credentials.

Run with the pinned runtime's /usr/bin/python3 (which owns the UNO bindings).
Only supplied workbooks are opened. No notebook, macro, or external-link execution.
"""
from __future__ import annotations

import ctypes
import errno
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import uuid

ENGINE = "libreoffice-24.2.7.2-ubuntu24.04.6-snapshot20260911"


def become_child_subreaper() -> None:
    """Keep orphaned LibreOffice helpers owned by this isolated worker."""
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong,
                           ctypes.c_ulong, ctypes.c_ulong]
    libc.prctl.restype = ctypes.c_int
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        raise OSError(ctypes.get_errno(), "cannot enable calculation child subreaper")


def reap_orphaned_children(timeout: float = 3) -> None:
    """Reap adopted helper processes, allowing a bounded grace period to exit.

    waitpid(-1) is restricted by the kernel to this worker's own children. It
    neither signals unrelated processes nor touches another worker's children.
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError(f"calculation helpers did not exit within {timeout:g} seconds of cleanup")
        if not pid:
            time.sleep(0.01)


def disable_network() -> None:
    """Allow UNO's local Unix sockets, deny new IPv4/IPv6/other sockets.

    The filter is inherited by LibreOffice. In Harbor this keeps spreadsheet
    calculation offline even though the separate judge needs provider access.
    Failure to install the filter is fatal rather than silently running online.
    """
    class ArgCmp(ctypes.Structure):
        _fields_ = [("arg", ctypes.c_uint), ("op", ctypes.c_int),
                    ("datum_a", ctypes.c_uint64), ("datum_b", ctypes.c_uint64)]

    lib = ctypes.CDLL("libseccomp.so.2", use_errno=True)
    lib.seccomp_init.argtypes = [ctypes.c_uint32]
    lib.seccomp_init.restype = ctypes.c_void_p
    lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    lib.seccomp_syscall_resolve_name.restype = ctypes.c_int
    lib.seccomp_rule_add_array.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
                                         ctypes.c_int, ctypes.c_uint, ctypes.POINTER(ArgCmp)]
    lib.seccomp_load.argtypes = [ctypes.c_void_p]
    lib.seccomp_release.argtypes = [ctypes.c_void_p]
    context = lib.seccomp_init(0x7FFF0000)  # SCMP_ACT_ALLOW
    if not context:
        raise RuntimeError("cannot allocate calculation network filter")
    try:
        # SCMP_CMP_NE: only AF_UNIX=1 may be created. No inherited network FDs
        # are passed to the worker or to the LibreOffice subprocess.
        condition = ArgCmp(0, 1, 1, 0)
        syscall = lib.seccomp_syscall_resolve_name(b"socket")
        if syscall < 0 or lib.seccomp_rule_add_array(
            context, 0x00050000 | errno.EPERM, syscall, 1, ctypes.byref(condition)
        ) != 0 or lib.seccomp_load(context) != 0:
            raise RuntimeError("cannot disable calculation network access")
    finally:
        lib.seccomp_release(context)


def calculate(path: Path, request: dict) -> dict:
    marker = Path("/opt/jobbench-calc-runtime")
    if not marker.is_file() or marker.read_text().strip() != ENGINE:
        raise RuntimeError("not running in the pinned JobBench calculation runtime")
    import uno
    from com.sun.star.beans import PropertyValue

    disable_network()
    version = subprocess.check_output(["/usr/bin/libreoffice", "--version"], text=True).strip()
    if not version.startswith("LibreOffice 24.2.7.2 "):
        raise RuntimeError(f"unexpected calculation engine: {version}")

    def prop(name, value):
        item = PropertyValue()
        item.Name, item.Value = name, value
        return item

    with tempfile.TemporaryDirectory(prefix="jobbench-calc-") as temporary:
        profile = Path(temporary) / "profile"
        pipe = "jobbench_" + uuid.uuid4().hex
        process = subprocess.Popen(
            ["/usr/bin/libreoffice", "-env:UserInstallation=" + profile.as_uri(),
             "--headless", "--norestore", "--nodefault",
             "--accept=pipe,name=" + pipe + ";urp;StarOffice.ComponentContext"],
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "TMPDIR": temporary,
                 "MAX_CONCURRENCY": "1", "SAL_DISABLE_OPENCL": "true"},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        desktop = document = None
        try:
            context = uno.getComponentContext()
            resolver = context.ServiceManager.createInstanceWithContext(
                "com.sun.star.bridge.UnoUrlResolver", context)
            deadline = time.monotonic() + 20
            while True:
                try:
                    remote = resolver.resolve(
                        "uno:pipe,name=" + pipe + ";urp;StarOffice.ComponentContext")
                    break
                except Exception:
                    if process.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError("LibreOffice failed to start")
                    time.sleep(0.05)
            desktop = remote.ServiceManager.createInstanceWithContext(
                "com.sun.star.frame.Desktop", remote)
            document = desktop.loadComponentFromURL(
                path.resolve().as_uri(), "_blank", 0,
                (prop("Hidden", True), prop("ReadOnly", True),
                 prop("UpdateDocMode", uno.getConstantByName("com.sun.star.document.UpdateDocMode.NO_UPDATE")),
                 prop("MacroExecutionMode", uno.getConstantByName("com.sun.star.document.MacroExecMode.NEVER_EXECUTE"))),
            )
            if document is None:
                raise RuntimeError("LibreOffice could not open the workbook")
            # Never replace this with conversion or merely enableAutomaticCalculation:
            # zero/stale caches can otherwise survive, including cross-sheet formulas.
            document.calculateAll()
            results = {}
            for sheet_name, addresses in request["cells"].items():
                sheet = document.Sheets.getByName(sheet_name)
                sheet_values = {}
                for address in addresses:
                    cell = sheet.getCellRangeByName(address)
                    sheet_values[address] = {
                        "value": cell.getDataArray()[0][0],
                        "display": cell.getString(),
                        "error_code": cell.getError(),
                    }
                results[sheet_name] = sheet_values
            return {"engine": ENGINE, "version": version, "network": "disabled",
                    "status": "recalculated", "cells": results}
        finally:
            try:
                if document is not None:
                    document.close(True)
            finally:
                try:
                    if desktop is not None:
                        desktop.terminate()
                finally:
                    if process.poll() is None:
                        process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=3)


def main() -> int:
    try:
        become_child_subreaper()
        try:
            request = json.load(sys.stdin)
            result = calculate(Path(sys.argv[1]), request)
        finally:
            reap_orphaned_children()
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "error", "error": f"{type(exc).__name__}: {exc}"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
