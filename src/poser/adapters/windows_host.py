"""Refuse Windows hosts other than x64, whatever architecture Python itself was built for.

pedalboard publishes no Windows ARM64 build, and poser is tested only on x64 Windows. x64 Python
running under emulation on an ARM64 host reports `win32` and `win-amd64` like a real x64 host, so
the gate asks Windows for the host's native machine type through `IsWow64Process2`.
"""

import os
import sys
import sysconfig

from poser.core.ports import PREREQUISITES_URL, UnsupportedPlatformError

IMAGE_FILE_MACHINE_ARM64 = 0xAA64


def native_machine() -> int | None:
    """Return the host's `IMAGE_FILE_MACHINE_*` value, or None when it cannot be read.

    Returns None off Windows (judged by `os.name`, so a test that patches `sys.platform` does not
    reach Windows-only code), when `IsWow64Process2` is missing (Windows before 10 1709), or when
    the call fails. Makes one Win32 call about the current process; starts no process and reads no
    file.
    """
    if sys.platform == "win32":
        if os.name != "nt":
            return None
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        is_wow64_process2 = getattr(kernel32, "IsWow64Process2", None)
        if is_wow64_process2 is None:
            return None
        is_wow64_process2.restype = wintypes.BOOL
        is_wow64_process2.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.USHORT),
            ctypes.POINTER(wintypes.USHORT),
        )
        get_current_process = kernel32.GetCurrentProcess
        get_current_process.restype = wintypes.HANDLE
        get_current_process.argtypes = ()
        process_machine = wintypes.USHORT()
        host_machine = wintypes.USHORT()
        if not is_wow64_process2(
            get_current_process(), ctypes.byref(process_machine), ctypes.byref(host_machine)
        ):
            return None
        return host_machine.value
    return None


def check_windows_host() -> None:
    """Raise UnsupportedPlatformError on a Windows ARM64 host, native or emulated Python alike.

    Callers run it only when `sys.platform` is `win32`. An unreadable host type is allowed, so a
    failed probe never blocks an x64 user.
    """
    if sysconfig.get_platform() == "win-arm64" or native_machine() == IMAGE_FILE_MACHINE_ARM64:
        raise UnsupportedPlatformError(
            "poser does not support Windows on Arm, including x64 Python running under "
            "emulation: pedalboard has no Windows ARM64 build and poser is tested only on x64 "
            f"Windows. See {PREREQUISITES_URL}"
        )
