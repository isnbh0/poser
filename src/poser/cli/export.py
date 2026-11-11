"""`export-song`: load once, render, analyze, and publish a verified new bundle."""

import argparse
import json
from importlib.metadata import version
from pathlib import Path

from poser.cli.common import RENDER_TIMEOUT_S, fail
from poser.cli.workspace import WorkspaceError, fill_path_options, song_id
from poser.compose import Composition, delivery_writer, parse_config
from poser.core.delivery import (
    DeliveryFailure,
    DeliveryIOFailed,
    DeliveryResult,
    DestinationError,
    DestinationRefused,
    InputSnapshot,
    InvalidInput,
    capture_inputs,
    export_song,
    result_to_data,
)
from poser.core.ports import AdapterContext, ConfigError

PROG = "export-song"


def _run(args: argparse.Namespace) -> DeliveryResult:
    try:
        base = fill_path_options(args).resolve()
        config_path = args.config.resolve()
        snapshot = InputSnapshot.capture(config_path)
        if snapshot.data is None:
            return InvalidInput(f"missing configuration: {config_path}")
        config = parse_config(snapshot.data.decode("utf-8"), config_path)
        context = AdapterContext(
            config_path.parent, args.sounds_dir.resolve(), args.output_dir.resolve()
        )
        composition = Composition(config, context)
        identifier = song_id(base, args.song)
        renders = composition.render_store()
        try:
            inputs = capture_inputs(
                base, snapshot, args.song, args.include, args.destination, renders
            )
            renders.song(identifier)  # Reject unsupported IDs before executing code.
        except DestinationError as error:
            return DestinationRefused(str(error))
        except (ValueError, OSError) as error:
            return InvalidInput(str(error))
        formats = composition.song_formats()
        instruments = composition.instruments()
        effects = composition.effects()
        audio_files = composition.audio_files()
        record_writer = composition.record_writer()
        tools = composition.analysis_tools()
        writer = delivery_writer()
    except (WorkspaceError, ConfigError, ValueError, UnicodeError) as error:
        return InvalidInput(str(error))
    except OSError as error:
        return DeliveryIOFailed(str(error))
    return export_song(
        inputs,
        identifier,
        formats,
        renders,
        instruments,
        effects,
        audio_files,
        record_writer,
        tools,
        writer,
        poser_version=version("poser"),
        timeout_s=RENDER_TIMEOUT_S,
        force=args.force,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=PROG, description=__doc__)
    parser.add_argument("song", type=Path)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--include", type=Path, action="append", default=[])
    parser.add_argument("--config", type=Path)
    parser.add_argument("--sounds-dir", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    result = _run(args)
    if args.json:
        print(json.dumps(result_to_data(result), indent=2))
    elif isinstance(result, DeliveryFailure):
        fail(PROG, result.detail)
    else:
        print(result.destination)
    return result.exit_code
