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
    title = default.copy()
    size = int(default.actual("size"))
    title.configure(weight="bold", size=size + 3 if size > 0 else size - 4)
    root._cardputer_heading_font = heading
    root._cardputer_title_font = title
    style = ttk.Style(root)
    style.configure("App.TLabel", font=title)
    style.configure("Section.TLabel", font=heading)
    style.configure("TButton", padding=(10, 5))
    style.configure("Treeview", rowheight=max(28, default.metrics("linespace") + 12))
    style.configure("Treeview.Heading", font=heading, padding=(8, 6))
    style.configure("TNotebook.Tab", padding=(16, 8))

    style.map(
        "Danger.TButton",
        foreground=[("disabled", "#808080"), ("!disabled", "#b42318")],
    )
