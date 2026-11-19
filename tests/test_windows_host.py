"""The Windows host gate: Windows on Arm is refused, native or emulated, and x64 is unchanged."""

import os
import subprocess
import sys
import sysconfig

import pytest

from poser.adapters import filesystem, isolation, windows_host
from poser.core.ports import PREREQUISITES_URL, UnsupportedPlatformError

ARM64 = 0xAA64
AMD64 = 0x8664


def simulate(monkeypatch: pytest.MonkeyPatch, *, machine: int | None, python_platform: str) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(windows_host, "native_machine", lambda: machine)
    monkeypatch.setattr(sysconfig, "get_platform", lambda: python_platform)


def test_an_arm64_host_is_refused_even_with_x64_python(monkeypatch: pytest.MonkeyPatch) -> None:
    simulate(monkeypatch, machine=ARM64, python_platform="win-amd64")

    with pytest.raises(UnsupportedPlatformError) as raised:
        windows_host.check_windows_host()
    message = str(raised.value)
    assert "Windows on Arm" in message
    assert "emulation" in message
    assert PREREQUISITES_URL in message
    with pytest.raises(UnsupportedPlatformError, match="Windows on Arm"):
        isolation.check_platform()


def test_native_arm64_python_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    simulate(monkeypatch, machine=None, python_platform="win-arm64")

    with pytest.raises(UnsupportedPlatformError, match="Windows on Arm"):
        windows_host.check_windows_host()


def test_an_x64_host_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    simulate(monkeypatch, machine=AMD64, python_platform="win-amd64")

    assert windows_host.check_windows_host() is None
    assert isolation.check_platform() is None


def test_an_unreadable_host_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    simulate(monkeypatch, machine=None, python_platform="win-amd64")

    assert windows_host.check_windows_host() is None


@pytest.mark.skipif(os.name == "nt", reason="simulates win32 on another platform")
def test_a_simulated_win32_does_not_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")

    assert windows_host.native_machine() is None


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_macos_and_linux_never_probe(monkeypatch: pytest.MonkeyPatch, platform: str) -> None:
    def probe() -> int | None:
        raise AssertionError("probed the Windows host off Windows")

    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(windows_host, "native_machine", probe)

    assert isolation.check_platform() is None


def test_the_file_system_gate_refuses_before_loading_the_windows_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    simulate(monkeypatch, machine=ARM64, python_platform="win-amd64")
    monkeypatch.delitem(sys.modules, "poser.adapters.filesystem_windows", raising=False)

    with pytest.raises(UnsupportedPlatformError, match="Windows on Arm"):
        filesystem.native()
    assert "poser.adapters.filesystem_windows" not in sys.modules


@pytest.mark.skipif(sys.platform != "win32", reason="reads the real Windows host")
def test_the_real_probe_reports_an_x64_host() -> None:
    assert windows_host.native_machine() == AMD64
    assert windows_host.check_windows_host() is None


DLL_ERROR = ImportError(
    "DLL load failed while importing pedalboard_native: The specified module could not be found."
)


def test_a_missing_runtime_names_the_missing_dll_and_the_fix() -> None:
    refusal = windows_host.missing_runtime(
        DLL_ERROR, platform="win32", loads=lambda name: name != "MSVCP140.dll"
    )

    assert refusal is not None
    assert "MSVCP140.dll" in refusal
    assert "VCRUNTIME140.dll" not in refusal
    assert "Visual C++ Redistributable" in refusal
    assert "x64" in refusal
    assert "Microsoft.VCRedist.2015+.x64" in refusal
    assert refusal.endswith(f"(original error: {DLL_ERROR})")


def test_a_present_runtime_leaves_the_original_error() -> None:
    assert windows_host.missing_runtime(DLL_ERROR, platform="win32", loads=lambda _: True) is None


def test_other_platforms_never_name_the_runtime() -> None:
    assert windows_host.missing_runtime(DLL_ERROR, platform="darwin", loads=lambda _: False) is None


@pytest.mark.parametrize(
    "message",
    ["No module named 'pedalboard'", "DLL load failed while importing myplugin"],
)
def test_other_import_errors_are_not_the_runtime(message: str) -> None:
    error = ImportError(message)

    assert windows_host.missing_runtime(error, platform="win32", loads=lambda _: False) is None


def test_the_default_probe_reports_everything_loadable_off_windows() -> None:
    if os.name == "nt":
        pytest.skip("off-Windows behaviour")
    assert all(windows_host._loads(name) for name in windows_host.RUNTIME_DLLS)


WAV_IMPORT = """
import sys
from importlib.abc import MetaPathFinder

class Refuse(MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name == "pedalboard.io":
            raise ImportError({message!r})
        return None

sys.meta_path.insert(0, Refuse())
from poser.adapters import windows_host
windows_host.missing_runtime = lambda error: {refusal!r}
try:
    import poser.adapters.wav
except Exception as error:
    print(type(error).__name__, type(error.__cause__).__name__, error, sep="|")
"""


def import_wav(message: str, refusal: str | None) -> list[str]:
    """Import the WAV adapter in a child whose `pedalboard.io` import fails with `message`, with
    `missing_runtime` answering `refusal`; return the error's type, its cause's type, and text."""
    code = WAV_IMPORT.format(message=message, refusal=refusal)
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, encoding="utf-8", check=True
    )
    return result.stdout.strip().split("|")


def test_wav_refuses_a_missing_runtime_as_a_platform_problem() -> None:
    assert import_wav(str(DLL_ERROR), "install the runtime") == [
        "UnsupportedPlatformError",
        "ImportError",
        "install the runtime",
    ]


def test_wav_reraises_an_import_error_that_is_not_the_runtime() -> None:
    assert import_wav("No module named 'pedalboard'", None) == [
        "ImportError",
        "NoneType",
        "No module named 'pedalboard'",
    ]
