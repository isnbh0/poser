"""The check-instruments command, with its composition faked."""

from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from fakes import FakeInstrument, FakeSound

from poser.cli import check
from poser.cli.common import EXIT_BAD_CONFIG
from poser.core.names import Identifier
from poser.core.ports import UnsupportedPlatformError


class RefusingScratch:
    @contextmanager
    def directory(self, *, parent: Path | None = None, prefix: str = "poser-") -> Generator[Path]:
        raise UnsupportedPlatformError("poser does not support this platform")
        yield Path()


def test_a_platform_the_scratch_port_refuses_is_reported_not_raised(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    name = Identifier("synth")
    composition = SimpleNamespace(
        instruments=lambda: {name: FakeInstrument(name, {None: FakeSound()})},
        context=SimpleNamespace(scratch=RefusingScratch()),
    )
    monkeypatch.setattr(check, "fill_path_options", lambda args: args)
    monkeypatch.setattr(check, "load_composition", lambda args: composition)

    assert check.main([]) == EXIT_BAD_CONFIG
    assert "does not support this platform" in capsys.readouterr().err
