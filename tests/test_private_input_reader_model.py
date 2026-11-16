"""Synthetic namespace model, NOT a Windows filesystem/security test."""

import hashlib
import unittest
from dataclasses import dataclass, field


@dataclass
class Node:
    children: dict[str, "Node"] = field(default_factory=dict)
    payload: bytes | None = None
    reparse: "Node | None" = None


def relative_open(parent, name):
    node = parent.children[name]
    if node.reparse is not None:
        raise ValueError("reparse")
    return node


def pathname_open(root, parts):
    """Models final-component-only no-follow, with ancestor traversal."""
    node = root
    for index, part in enumerate(parts):
        node = node.children[part]
        if node.reparse is not None:
            if index == len(parts) - 1:
                raise ValueError("reparse")
            node = node.reparse
    return node


def bounded_verified_read(node, expected):
    payload = node.payload
    if payload is None:
        raise ValueError("not_regular")
    captured = payload[: len(expected) + 1]
    if len(captured) != len(expected):
        raise ValueError("size_changed")
    if hashlib.sha256(captured).digest() != hashlib.sha256(expected).digest():
        raise ValueError("bytes_changed")
    return captured


class ReaderRaceModelTests(unittest.TestCase):
    def fixture(self):
        private = Node(children={"asset.bin": Node(payload=b"good")})
        outside = Node(children={"asset.bin": Node(payload=b"evil")})
        return Node(children={"private": private}), private, outside

    def test_final_nofollow_does_not_prevent_ancestor_escape(self):
        root, held, outside = self.fixture()
        root.children["private"] = Node(reparse=outside)
        self.assertEqual(pathname_open(root, ["private", "asset.bin"]).payload, b"evil")
        self.assertEqual(relative_open(held, "asset.bin").payload, b"good")

    def test_swap_before_ancestor_open_is_rejected(self):
        root, _, outside = self.fixture()
        root.children["private"] = Node(reparse=outside)
        with self.assertRaisesRegex(ValueError, "reparse"):
            relative_open(root, "private")

    def test_leaf_reparse_is_rejected(self):
        _, held, outside = self.fixture()
        held.children["asset.bin"] = Node(reparse=outside.children["asset.bin"])
        with self.assertRaisesRegex(ValueError, "reparse"):
            relative_open(held, "asset.bin")

    def test_replacement_after_open_reads_pinned_object(self):
        _, held, outside = self.fixture()
        opened = relative_open(held, "asset.bin")
        held.children["asset.bin"] = outside.children["asset.bin"]
        self.assertEqual(bounded_verified_read(opened, b"good"), b"good")

    def test_in_place_mutation_rejected_even_with_pinned_handle(self):
        for content in (b"evil", b"go", b"goodmore"):
            with self.subTest(content=content):
                node = Node(payload=content)
                with self.assertRaises(ValueError):
                    bounded_verified_read(node, b"good")

    def test_containment_required_even_if_outside_hash_matches(self):
        root, _, outside = self.fixture()
        outside.children["asset.bin"].payload = b"good"
        root.children["private"] = Node(reparse=outside)
        escaped = pathname_open(root, ["private", "asset.bin"])
        self.assertEqual(bounded_verified_read(escaped, b"good"), b"good")
        with self.assertRaisesRegex(ValueError, "reparse"):
            relative_open(root, "private")


if __name__ == "__main__":
    unittest.main()
