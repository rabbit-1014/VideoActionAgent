"""Observe the actual Windows interpreter, including native allocations."""
import ctypes
import json
import os
import threading
from ctypes import wintypes


def memory_metrics():
    if os.name != "nt":
        return {}

    class Counters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD)] + [
            (name, ctypes.c_size_t) for name in (
                "peak_working_set", "working_set", "a", "b", "c", "d",
                "pagefile", "peak_pagefile", "private",
            )
        ]

    class System(ctypes.Structure):
        _fields_ = [("length", wintypes.DWORD), ("load", wintypes.DWORD)] + [
            (name, ctypes.c_ulonglong) for name in (
                "physical", "free_physical", "commit", "free_commit", "virtual",
                "free_virtual", "extended",
            )
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    api = ctypes.WinDLL("psapi", use_last_error=True)
    api.GetProcessMemoryInfo.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD,
    ]
    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    if not api.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
        raise OSError("worker_memory_measurement_failed")
    system = System()
    system.length = ctypes.sizeof(system)
    if not kernel.GlobalMemoryStatusEx(ctypes.byref(system)):
        raise OSError("system_memory_measurement_failed")
    return {"peak_working_set_bytes": counters.peak_working_set,
            "peak_pagefile_bytes": counters.peak_pagefile, "private_bytes": counters.private,
            "available_commit_bytes": system.free_commit}


def start_guard(output, diagnostic, *, private_budget=1536 * 1024 * 1024):
    """Bound this owned worker; stop without a partial transcript on overrun."""
    done = threading.Event()

    def watch():
        first = True
        while not done.is_set():
            reason = None
            try:
                metrics = memory_metrics()
                if metrics.get("private_bytes", 0) > private_budget:
                    reason = "worker_resource_budget"
                elif metrics.get("available_commit_bytes", 768 * 1024 * 1024) < (
                    (768 if first else 256) * 1024 * 1024
                ):
                    reason = "system_resource_budget"
            except OSError:
                metrics, reason = {}, "worker_memory_measurement_failed"
            if reason:
                try:
                    diagnostic.write_text(json.dumps({"reason": reason, **metrics}), encoding="utf-8")
                    output.write_text(json.dumps({"error": "transcription_failed", "rejected_field": reason}), encoding="utf-8")
                finally:
                    os._exit(2)
            first = False
            done.wait(0.1)

    threading.Thread(target=watch, name="asr-resource-bound", daemon=True).start()
    return done
