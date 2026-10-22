"""Native named fonts and quiet, resizable hierarchy for the live operator view."""

import os
from typing import Any


def prepare_display() -> None:
    """Set source launches to system DPI awareness before Tk creates windows.

    Packaged launches declare this in the application manifest. Windows refuses
    an already-set process default, which must remain untouched. Monitor-specific
    scaling/visual acceptance is a separate native Windows check.
    """
    if os.name != "nt":
        return
    import ctypes

    try:
        loader = getattr(ctypes, "windll", None)
        if loader is not None:
            loader.user32.SetProcessDPIAware()
    except (AttributeError, OSError):
        pass


def apply_theme(root: Any) -> None:
    from tkinter import font, ttk

    default = font.nametofont("TkDefaultFont", root=root)
    heading = default.copy()
    heading.configure(weight="bold")
    root._cardputer_heading_font = heading
    style = ttk.Style(root)
    style.configure("App.TLabel", font=default)
    style.configure("Section.TLabel", font=heading)
    style.configure("Treeview", rowheight=max(24, default.metrics("linespace") + 10))
    style.configure("TNotebook.Tab", padding=(14, 7))

    style.map(
        "Danger.TButton",
        foreground=[("disabled", "#808080"), ("!disabled", "#b42318")],
    )
