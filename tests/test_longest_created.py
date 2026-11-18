"""`longest_created` finds the longest path a system makes, whatever its earlier attempts left."""

from pathlib import Path

import pytest
from paths import longest_created, make_file, path_of_length

from poser.core.paths import PathUnit


@pytest.mark.parametrize("fill", ["d", "\U0001d11e"], ids=["ascii", "g-clef"])
def test_the_longest_file_is_found_for_every_limit_across_two_hundred_lengths(
    tmp_path: Path, fill: str
) -> None:
    """A system that refuses a file path past `limit` UTF-16 units makes paths up to it, and the
    search lands on the longest. Some attempt across these limits ends its file on a whole part,
    whose name a longer attempt needs for a folder."""
    base = tmp_path / "f"
    start = PathUnit.UTF16_UNITS.length(str(base.absolute()))
    for limit in range(start + 60, start + 260):

        def create(path: Path, limit: int = limit) -> None:
            if PathUnit.UTF16_UNITS.length(str(path)) > limit:
                raise OSError(f"{path} is past {limit}")
            make_file(path)

        longest = longest_created(base, create, limit + 100, fill)
        lengths = [
            PathUnit.UTF16_UNITS.length(str(path_of_length(base, length, fill)))
            for length in (longest, longest + 1)
        ]
        assert lengths[0] <= limit < lengths[1], (limit, lengths)
