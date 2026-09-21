"""Command-domain mixins split out of tui/app.py (P4-1).

Each mixin moves verbatim `cmd_*` methods behind a plain class; OasetApp
inherits them all, so dispatch, palette and tests are untouched. Bodies
reference each other through `self` exactly as before.
"""
