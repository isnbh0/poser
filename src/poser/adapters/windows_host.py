"""Refuse Windows hosts poser cannot run on, and name a missing runtime pedalboard needs.

pedalboard publishes no Windows ARM64 build, and poser is tested only on x64 Windows. x64 Python
running under emulation on an ARM64 host reports `win32` and `win-amd64` like a real x64 host, so
the gate asks Windows for the host's native machine type through `IsWow64Process2`.

pedalboard's native module links the Microsoft Visual C++ runtime, which Windows does not ship.
Without it, importing pedalboard fails with a bare "DLL load failed"; `missing_runtime` turns that
into the fix.
"""

import os
import sys
import sysconfig
from collections.abc import Callable

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


RUNTIME_DLLS = ("MSVCP140.dll", "VCRUNTIME140.dll", "VCRUNTIME140_1.dll")
"""The DLLs `pedalboard_native` imports that Windows 10 and 11 do not ship.

Verified against the pedalboard 0.9.26 `cp313-win_amd64` wheel, which bundles no DLL and has no
delay imports; every other import is an OS DLL. Re-check this list when bumping pedalboard.
"""

VC_REDIST_URL = "https://learn.microsoft.com/cpp/windows/latest-supported-vc-redist"


def _loads(name: str) -> bool:
    """Return whether Windows can load the DLL `name`; True off Windows, where nothing is probed."""
    if os.name != "nt":
        return True
    import ctypes

    try:
        ctypes.WinDLL(name)
    except OSError:
        return False
    return True


def missing_runtime(
    error: ImportError,
    *,
    platform: str = sys.platform,
    loads: Callable[[str], bool] = _loads,
) -> str | None:
    """Return the refusal for an import that failed because the VC++ runtime is missing, or None.

    The refusal applies only on win32, to an error whose message has CPython's "DLL load failed"
    prefix and names pedalboard, and only when `loads` reports at least one of `RUNTIME_DLLS`
    missing. Anything else (application control, a quarantined module, a plugin's own DLL) returns
    None, so the original error stands. The tail of CPython's message is localized by Windows, so
    only the prefix is matched. Starts no process and writes no file.
    """
    message = str(error)
    if platform != "win32" or "DLL load failed" not in message or "pedalboard" not in message:
        return None
    missing = [name for name in RUNTIME_DLLS if not loads(name)]
    if not missing:
        return None
    return (
        f"pedalboard needs the Microsoft Visual C++ Redistributable (x64), and this machine lacks "
        f"{', '.join(missing)}. Install it from {VC_REDIST_URL} or with "
        f"`winget install --id Microsoft.VCRedist.2015+.x64 -e`, then rerun the command. "
        f"(original error: {message})"
    )
