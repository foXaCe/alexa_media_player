"""Per-ConfigEntry setup helpers for Alexa Media Player.

These submodules hold logic that used to live as nested closures inside the
monolithic ``setup_alexa`` function in ``__init__.py``. Each helper now takes an
explicit :class:`SetupContext` (and, where the ``@_catch_login_errors``
decorator inspects ``args[0]``, a leading ``login_obj``) instead of relying on
nested scope, so the behaviour is unchanged while the code is testable in
isolation.
"""

from __future__ import annotations

from .context import SetupContext

__all__ = ["SetupContext"]
