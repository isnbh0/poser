"""Loading objects named by import path, `module:attribute`.

An import path names a module that must be importable in the workspace's environment, which
isolation workers share. This module changes no import path.
"""

import importlib
from collections.abc import Mapping

from poser.core.jsonvalue import JsonValue
from poser.core.ports import ConfigError


def load_object(path: str) -> object:
    """Return the object an import path `module:attribute` names, where `attribute` may be dotted.

    Raises ConfigError, with the import error's message, when the path is malformed, the module
    cannot be imported, or the attribute does not exist.
    """
    module_name, colon, attribute = path.partition(":")
    if not (colon and module_name and attribute):
        raise ConfigError(f"{path!r} is not an import path module:attribute")
    try:
        target: object = importlib.import_module(module_name)
    except ImportError as error:
        raise ConfigError(f"cannot import {path!r}: {error}") from error
    for part in attribute.split("."):
        try:
            target = getattr(target, part)
        except AttributeError as error:
            raise ConfigError(f"cannot import {path!r}: {error}") from error
    return target


def refuse_options(options: Mapping[str, JsonValue]) -> None:
    """Raise ConfigError for any option, for an adapter that takes none."""
    if options:
        raise ConfigError(f"unknown option {min(options)!r}; this adapter takes no options")
