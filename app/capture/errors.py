"""The capture configuration error, importable without PyYAML.

:class:`CaptureConfigError` lives in its own module rather than in
:mod:`app.capture.config` because the capture CLI has to be able to *name* it
before anything touches YAML: ``scripts/guardianctl.py`` imports it at module
scope, and importing :mod:`app.capture.config` pulls in PyYAML — which turned
``guardianctl --help`` into a ``ModuleNotFoundError`` and hid the CLI's own
friendly "PyYAML not installed" message on a machine without PyYAML (#135).

:mod:`app.capture.config` re-exports the class, so every existing import
(``app.capture.config``, ``app.capture``) keeps working and there is one class,
never two.  This module deliberately imports nothing.
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
