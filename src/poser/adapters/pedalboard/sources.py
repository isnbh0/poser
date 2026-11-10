"""Sound names of a Pedalboard instrument, and the files they name.

A sound name is `<source>/<path>`: a source, then a file's path inside it with `/` separators and
without the file's suffix. No name means the plugin's default state. Configured sources hold
presets; the saved source holds the sounds `save-sound` keeps, directly in
`<sounds dir>/<instrument>/`.

Lookups go through a scan, never `Path.exists`, because a case-insensitive volume would match a
name that differs in case. A configured source's files that no configured format claims are
reported per source, never silently skipped.
"""

import os
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from poser.adapters.pedalboard.config import SAVED_SOURCE, PedalboardConfig
from poser.adapters.pedalboard.formats import PresetError, PresetFormat, SavedFormat
from poser.core.names import Identifier, Suffix

PRESET_DESCRIPTION = "preset"
DEFAULT_DESCRIPTION = "default patch"


@dataclass(frozen=True)
class SourceScan:
    """The files a scan found by name, and what it could not use."""

    files: Mapping[str, tuple[Path, PresetFormat]]
    problems: tuple[str, ...]
    unavailable: frozenset[Identifier]
    # The problem behind each source in `unavailable`, and each name left out of `files`.
    source_problems: Mapping[Identifier, str] = field(default_factory=dict)
    name_problems: Mapping[str, str] = field(default_factory=dict)
    # Per source, the sentence naming the files no configured format claims.
    ignored: Mapping[Identifier, str] = field(default_factory=dict)


def combine(*scans: SourceScan) -> SourceScan:
    """Return one scan holding everything the scans of separate sources found."""
    return SourceScan(
        files={name: found for scan in scans for name, found in scan.files.items()},
        problems=tuple(problem for scan in scans for problem in scan.problems),
        unavailable=frozenset().union(*(scan.unavailable for scan in scans)),
        source_problems={key: text for scan in scans for key, text in scan.source_problems.items()},
        name_problems={key: text for scan in scans for key, text in scan.name_problems.items()},
        ignored={key: text for scan in scans for key, text in scan.ignored.items()},
    )


def walk(directory: Path) -> dict[str, Path]:
    """Map the relative POSIX path of every non-hidden file under `directory` to its path; callers
    bucket the files by suffix. Raises PresetError for a folder it cannot read.

    Symlinked folders count, since a user may link a preset pack in, and link loops stop. Hidden
    files and folders are skipped.
    """

    def unreadable(error: OSError) -> None:
        raise PresetError(f"cannot read presets folder {error.filename}: {error.strerror}")

    files = {}
    seen = set()
    for folder, dirs, names in os.walk(directory, onerror=unreadable, followlinks=True):
        real = os.path.realpath(folder)
        if real in seen:
            dirs.clear()
            continue
        seen.add(real)
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for name in sorted(names):
            path = Path(folder, name)
            if name.startswith(".") or not path.is_file():
                continue
            files[path.relative_to(directory).as_posix()] = path
    return files


def scan_source(
    source: Identifier, directory: Path, formats: Mapping[Suffix, PresetFormat]
) -> SourceScan:
    """Scan one source's directory, which must exist, with `formats`."""
    try:
        found = walk(directory)
    except PresetError as error:
        return unavailable_source(source, str(error))
    readable = {str(fmt.suffix) for fmt in formats.values()}
    claimed = {rel: path for rel, path in found.items() if path.suffix.lower() in readable}
    scan = name_files(source, claimed, formats)
    counts: Counter[str] = Counter(
        path.suffix.lower() or "(no suffix)" for rel, path in found.items() if rel not in claimed
    )
    if not counts:
        return scan
    listed = ", ".join(f"{n} {sfx}" for sfx, n in sorted(counts.items()))
    sentence = (
        f"source {source!r} holds {listed} poser cannot read"
        f" (it reads: {', '.join(sorted(readable))})"
    )
    return SourceScan(
        scan.files,
        scan.problems,
        scan.unavailable,
        scan.source_problems,
        scan.name_problems,
        {source: sentence},
    )


def unavailable_source(source: Identifier, problem: str) -> SourceScan:
    return SourceScan({}, (problem,), frozenset({source}), {source: problem})


def name_files(
    source: Identifier, found: Mapping[str, Path], formats: Mapping[Suffix, PresetFormat]
) -> SourceScan:
    """Name each found file `<source>/<relative path without suffix>`. Two files sharing a name,
    which differ only in their suffix's case, leave the name out as ambiguous."""
    by_name = group_by_name(found)
    files: dict[str, tuple[Path, PresetFormat]] = {}
    problems: list[str] = []
    name_problems: dict[str, str] = {}
    for relative, paths in sorted(by_name.items()):
        name = f"{source}/{relative}"
        if len(paths) > 1:
            problem = ambiguity(name, paths)
            problems.append(problem)
            name_problems[name] = problem
            continue
        path = paths[0]
        files[name] = (path, formats[Suffix(path.suffix.lower())])
    return SourceScan(files, tuple(problems), frozenset(), {}, name_problems)


def group_by_name(found: Mapping[str, Path]) -> dict[str, list[Path]]:
    """Group files keyed by relative path by that path without its suffix."""
    by_name: defaultdict[str, list[Path]] = defaultdict(list)
    for relative, path in found.items():
        by_name[relative[: -len(path.suffix)]].append(path)
    return dict(by_name)


def ambiguity(name: str, paths: list[Path]) -> str:
    return f"{name} is ambiguous: {' and '.join(str(path) for path in paths)}"


def scan_configured(config: PedalboardConfig, formats: Mapping[Suffix, PresetFormat]) -> SourceScan:
    """Scan the configured sources. A missing one is a problem and unavailable."""
    scans = []
    for source, directory in sorted(config.sources.items()):
        if not directory.is_dir():
            reason = "does not exist" if not directory.exists() else "is not a directory"
            problem = f"source {source!r} directory {directory} {reason}"
            scans.append(SourceScan({}, (problem,), frozenset({source}), {source: problem}))
            continue
        scans.append(scan_source(source, directory, formats))
    return combine(*scans)


def saved_dir(config: PedalboardConfig, sounds_dir: Path) -> Path:
    return sounds_dir / config.name


def list_saved(directory: Path) -> dict[str, Path]:
    """Map the name of each saved sound file directly in `directory`, an existing folder, to its
    path. Raises PresetError when the folder cannot be read.

    `store` writes only flat names, so subfolders are ignored, and one that cannot be read does
    not hide the saved sounds beside it. Hidden files, such as the store's lock, are skipped.
    """
    suffix = SavedFormat().suffix
    try:
        with os.scandir(directory) as entries:
            listed = sorted(entries, key=lambda entry: entry.name)
    except OSError as error:
        raise PresetError(
            f"cannot read saved sounds folder {directory}: {error.strerror}"
        ) from error
    files = {}
    for entry in listed:
        path = Path(entry.path)
        if entry.name.startswith(".") or path.suffix.lower() != suffix:
            continue
        try:
            is_file = entry.is_file()  # Follows a symlink, as a read would.
        except OSError:
            continue
        if is_file:
            files[entry.name] = path
    return files


def scan_saved(config: PedalboardConfig, sounds_dir: Path) -> SourceScan:
    """Scan the saved source with SavedFormat alone. A missing directory holds no sounds."""
    directory = saved_dir(config, sounds_dir)
    if not directory.is_dir():
        return SourceScan({}, (), frozenset())
    source = Identifier(SAVED_SOURCE)
    try:
        found = list_saved(directory)
    except PresetError as error:
        return unavailable_source(source, str(error))
    saved = SavedFormat()
    return name_files(source, found, {saved.suffix: saved})


def scan(
    config: PedalboardConfig, sounds_dir: Path, formats: Mapping[Suffix, PresetFormat]
) -> SourceScan:
    """Scan every source: the configured ones with `formats`, the saved one with SavedFormat."""
    return combine(scan_configured(config, formats), scan_saved(config, sounds_dir))


def describe(name: str, path: Path, fmt: PresetFormat) -> str:
    """Describe a sound: the format's description, or else the file's folder inside its source,
    so `presets/Bass (Hard)/X` is `Bass (Hard) preset` and `presets/X` is `preset`.

    Raises PresetError when the format cannot read the file.
    """
    described = fmt.describe(path)
    if described is not None:
        return described
    folder = name.split("/", 1)[1].rpartition("/")[0]
    return f"{folder} {PRESET_DESCRIPTION}" if folder else PRESET_DESCRIPTION
