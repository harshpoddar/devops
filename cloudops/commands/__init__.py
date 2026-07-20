"""Subcommand implementations behind the unified `cloudops` CLI.

Each module exposes ``main(argv=None) -> int`` — it parses its own ``argv``
(default ``sys.argv[1:]``) so it works both as a `cloudops <sub>` subcommand
(dispatched from ``cloudops.cli``) and when run directly. The legacy
``scripts/<name>/<name>.py`` files are thin shims that call these.
"""
