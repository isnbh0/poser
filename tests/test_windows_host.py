"""The Windows host gate: Windows on Arm is refused, native or emulated, and x64 is unchanged."""

import os
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
