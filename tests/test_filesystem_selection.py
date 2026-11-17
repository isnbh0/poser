"""Which platforms have a file-system backend. These cases change whenever a backend is added."""

import sys

import pytest

from poser.adapters.filesystem import native
from poser.core.ports import ConfigError, UnsupportedPlatformError


@pytest.mark.parametrize("platform", ["freebsd14", "cygwin"])
def test_a_platform_without_a_backend_is_refused_by_name(
    monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    monkeypatch.setattr(sys, "platform", platform)
    with pytest.raises(UnsupportedPlatformError) as raised:
        native()
    assert isinstance(raised.value, ConfigError)
    message = str(raised.value)
    for part in (platform, "macOS", "Linux", "Windows", "docs/setup.md#prerequisites"):
        assert part in message, part


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_macos_and_linux_get_the_posix_backend(
    monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    pytest.importorskip("fcntl")
    from poser.adapters.filesystem_posix import PosixFileSystem

    monkeypatch.setattr(sys, "platform", platform)
    assert isinstance(native(), PosixFileSystem)


def test_windows_gets_the_windows_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("msvcrt")
    from poser.adapters.filesystem_windows import WindowsFileSystem

    monkeypatch.setattr(sys, "platform", "win32")
    assert isinstance(native(), WindowsFileSystem)
