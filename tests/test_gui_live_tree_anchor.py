"""Actual Tk evidence for reading position across keyed history changes."""

import tkinter as tk
from tkinter import ttk

import pytest
from test_gui_live_presentation import Tree

from cardputer_mcp.gui_live_presentation import sync_rows


@pytest.fixture
def native_tree():
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("native Tk display unavailable")
    root.geometry("460x260")
    tree = ttk.Treeview(root, columns=("state",), show="tree headings", height=8)
    tree.pack(fill="both", expand=True)
    for number in range(100):
        tree.insert("", "end", iid=f"job-{number}", text=f"Job {number}", values=("running",))
    root.update()
    tree.yview_moveto(0.4)
    tree.selection_set("job-75")
    tree.focus("job-75")
    root.update()
    try:
        yield root, tree
    finally:
        root.destroy()


def first_visible(tree):
    # Independent of identify_row: Tk 8.6 can report an offscreen item over
    # headings. bbox excludes rows above the viewport on both Tk 8.6 and 9.
    for identifier in tree.get_children():
        bounds = tree.bbox(identifier)
        if bounds and bounds[3] > 0 and 0 <= bounds[1] < tree.winfo_height():
            return identifier
    return ""


def rows(identifiers):
    return [(identifier, identifier, ("running",)) for identifier in identifiers]


def test_legacy_heading_identification_cannot_replace_visible_anchor():
    tree = Tree()
    tree.vertical = (0.5, 1.0)
    tree.winfo_height = lambda: 30
    tree.identify_row = lambda y: "a" if y < 10 else "b"
    tree.bbox = lambda identifier: () if identifier == "a" else (0, 10, 100, 20)
    sync_rows(tree, [("a", "A", ("running",)), ("b", "B", ("queued",))])
    assert tree.vertical == (0.5, 1.0)
    assert tree.changed == []


@pytest.mark.parametrize("change", ["prepend", "reorder", "remove-above"])
def test_native_keyed_update_preserves_top_visible_job(native_tree, change):
    root, tree = native_tree
    anchor = first_visible(tree)
    assert anchor and anchor != tree.focus()
    identifiers = list(tree.get_children())
    if change == "prepend":
        identifiers = [f"new-{number}" for number in range(12)] + identifiers
    elif change == "reorder":
        identifiers = identifiers[:10] + identifiers[50:60] + identifiers[10:50] + identifiers[60:]
    else:
        identifiers = identifiers[12:]
    sync_rows(tree, rows(identifiers))
    root.update()
    assert first_visible(tree) == anchor
    assert tree.selection() == ("job-75",) and tree.focus() == "job-75"


def test_native_removed_anchor_uses_next_surviving_visible_job(native_tree):
    root, tree = native_tree
    anchor = first_visible(tree)
    identifiers = list(tree.get_children())
    successor = identifiers[identifiers.index(anchor) + 1]
    identifiers.remove(anchor)
    sync_rows(tree, rows(identifiers))
    root.update()
    assert first_visible(tree) == successor
    assert tree.selection() == ("job-75",) and tree.focus() == "job-75"


def test_native_unchanged_rows_keep_reading_position(native_tree):
    root, tree = native_tree
    anchor, fraction = first_visible(tree), tree.yview()
    sync_rows(tree, rows(tree.get_children()))
    root.update()
    assert first_visible(tree) == anchor and tree.yview() == fraction
