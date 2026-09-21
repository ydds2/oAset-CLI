"""oAset CLI — an open-source terminal TUI coding agent.

OpenAI-compatible providers · streaming tool calling · session persistence ·
permission gating · Textual-powered TUI.
"""

__version__ = "0.10.0"
__all__ = ["__version__", "Oaset", "SessionHost"]


def __getattr__(name: str):
    if name == "Oaset":
        from oaset.sdk import Oaset
        return Oaset
    if name == "SessionHost":
        from oaset.host import SessionHost
        return SessionHost
    raise AttributeError(name)
