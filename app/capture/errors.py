"""The capture configuration error, importable without PyYAML.

:class:`CaptureConfigError` lives in its own module rather than in
:mod:`app.capture.config` because ``scripts/guardianctl.py`` has to be able to
*name* it at module scope, and naming it from :mod:`app.capture.config` once
pulled PyYAML in before argument parsing: ``guardianctl --help`` died with a
``ModuleNotFoundError`` and the CLI's own "PyYAML not installed" message was
hidden on a machine without PyYAML (#135).

What actually keeps PyYAML out of the CLI's import path is two facts together,
and they are what a future change must preserve:

1. no module reachable from ``app/capture/__init__.py`` imports PyYAML at module
   scope — :func:`app.capture.config.load_capture_config` imports it inside the
   function, the only place that parses YAML;
2. ``app.capture.errors`` itself imports nothing.

Importing ``app.capture.errors`` still executes ``app/capture/__init__.py``, so
point 1 is not a detail about this file — ``tests/unit/test_capture_import_path.py``
pins both points so neither can be broken silently.

:mod:`app.capture.config` re-exports the class, so every existing import
(``app.capture.config``, ``app.capture``) keeps working and there is one class,
never two.
"""

from __future__ import annotations


class CaptureConfigError(ValueError):
    """The capture configuration is invalid; a bad value is never guessed around.

    Raised where a configured value cannot be read as the setting it names —
    a malformed ``capture.file_mode``/``capture.directory_mode`` is the case it
    exists for.  Callers that want to treat "the configuration is broken"
    separately from an unrelated failure catch this type instead of matching on
    message text.

    It subclasses :class:`ValueError` because that is what
    :mod:`app.capture.config` raises for every other misconfiguration, so an
    existing ``except ValueError`` around configuration loading keeps catching
    it.  The refusal of a world-accessible mode in
    :meth:`app.capture.config.CaptureConfig._validate` stays a plain
    ``ValueError`` with its own message: it rejects a value that was read
    correctly, while this error is about a value that cannot be read at all.
    """
