"""Pin how the two source receipt readers treat receipt.json shapes (schema 1 and 2).

``SourceSnapshots._resolve`` indexes with ``[]`` and maps every non-SourceError to
``source_snapshot_unavailable``; ``source_payloads.metadata`` uses ``.get`` and returns
None. The readers differ on purpose, and the order of their checks decides the reason
code, so these tests pin outcomes for crafted receipts rather than only the happy path.
Every receipt here is planted offline under a name equal to the sha256 of its bytes.
"""

import io
import json
import shutil
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path
from typing import Any

from cardputer_mcp.config import BuildProject, Settings, SourceRepository
from cardputer_mcp.source_payloads import metadata
from cardputer_mcp.source_snapshot import (
    SourceError,
    SourceSnapshots,
    canonical,
    digest,
    file_receipt,
    tree_hash,
)

COMMIT = "a" * 40
REPOSITORY = "fixture/project"
FILES = {
    "CMakeLists.txt": b"project(synthetic)\n",
    "sdkconfig.defaults": b"CONFIG_IDF_TARGET=esp32s3\n",
}


def archive() -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as zipped:
        for name, data in FILES.items():
            entry = zipfile.ZipInfo("project-" + COMMIT + "/" + name)
            entry.external_attr = (stat.S_IFREG | 0o644) << 16
            zipped.writestr(entry, data)
    return stream.getvalue()


SUBMODULES = [
    {
        "path": "third_party/lib",
        "repository": "fixture/lib",
        "commit": "b" * 40,
        "tree_sha1": "c" * 40,
        "archive_sha256": "d" * 64,
        "source_sha256": "e" * 64,
    }
]


class SourceReceiptReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        profile = self.root / "profile.ps1"
        profile.write_text("synthetic profile")
        self.settings = Settings(
            state_dir=self.root / "state",
            build_projects={
                "fixture": BuildProject(
                    project_dir=self.root / "user-worktree",
                    trusted_profile=profile,
                    build_root=self.root / "build-output",
                    build_min_free_bytes=0,
                    source_repository=SourceRepository(
                        github_repository=REPOSITORY,
                        cache_root=self.root / "cache",
                        private_acl_confirmed=True,
                    ),
                )
            },
        )
        tree = tree_hash({name: file_receipt(data, "100644") for name, data in FILES.items()})

        def transport(url, deadline, live, limit):
            if url.startswith("https://api.github.com/"):
                return json.dumps({"sha": COMMIT, "tree": {"sha": tree}}).encode()
            return archive()

        self.snapshots = SourceSnapshots(self.settings, transport)
        self.plan = self.snapshots.prepare("fixture", COMMIT, "owner") | {"job_id": "fetch-one"}
        self.receipt = self.snapshots.fetch(self.plan, lambda: True, lambda _: None)[
            "source_receipt"
        ]
        self.cache = self.root / "cache"
        self.raw = (self.cache / self.receipt / "receipt.json").read_bytes()
        self.value: dict[str, Any] = json.loads(self.raw)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def plant(self, value: Any = None, *, raw: bytes | None = None, source: str = "copy") -> str:
        encoded = canonical(value) if raw is None else raw
        name = digest(encoded)
        folder = self.cache / name
        folder.mkdir()
        (folder / "receipt.json").write_bytes(encoded)
        if source == "copy":
            shutil.copytree(self.cache / self.receipt / "source", folder / "source")
        elif source == "empty":
            (folder / "source").mkdir()
        return name

    def changed(self, **update: Any) -> dict[str, Any]:
        return {**self.value, **update}

    def without(self, *keys: str) -> dict[str, Any]:
        return {key: item for key, item in self.value.items() if key not in keys}

    def resolve_code(self, name: str, principal: str = "owner") -> str | None:
        try:
            self.snapshots.resolve("fixture", name, principal)
        except SourceError as error:
            return str(error)
        return None

    def evidence(self, name: str, job_id: Any = "fetch-one") -> dict[str, Any]:
        return {
            "sources": {name: [{"job_id": job_id, "plan": self.plan, "summary": {}}]},
            "jobs": {},
            "publications": {},
        }

    def listed(self, name: str) -> dict[str, Any] | None:
        return metadata(self.snapshots, self.plan, name, self.evidence(name))

    def assert_unchanged(self, name: str, encoded: bytes) -> None:
        self.assertEqual((self.cache / name / "receipt.json").read_bytes(), encoded)

    # Both readers accept the stored v1 receipt and hand back the parsed dict as is.
    def test_v1_receipt_reads_in_both_readers_without_rewriting_bytes(self) -> None:
        self.assertEqual(self.value["schema_version"], 1)
        source, value = self.snapshots.resolve("fixture", self.receipt, "owner")
        self.assertIs(type(value), dict)
        self.assertEqual(value, self.value)
        self.assertEqual(source, self.cache / self.receipt / "source")
        listed = self.listed(self.receipt)
        self.assertIs(type(listed), dict)
        self.assertEqual(listed, self.value)
        self.assert_unchanged(self.receipt, self.raw)

    def test_v2_receipt_reads_in_both_readers_without_rewriting_bytes(self) -> None:
        value = self.changed(
            schema_version=2, submodules=SUBMODULES, root_source_sha256=self.value["source_sha256"]
        )
        name = self.plant(value)
        _, resolved = self.snapshots.resolve("fixture", name, "owner")
        self.assertEqual(resolved, value)
        self.assertEqual(self.listed(name), value)
        self.assert_unchanged(name, canonical(value))

    def test_v2_without_valid_submodules_differs_between_readers(self) -> None:
        for submodules in (None, [], "x"):
            with self.subTest(submodules=submodules):
                value = self.changed(schema_version=2)
                if submodules is not None:
                    value["submodules"] = submodules
                name = self.plant(value)
                self.assertEqual(self.resolve_code(name), "source_submodule_provenance_rejected")
                self.assertEqual(self.listed(name), value)  # metadata never checks submodules

    def test_extra_key_is_accepted_and_kept_by_both_readers(self) -> None:
        value = self.changed(future_field={"nested": [1, 2]})
        name = self.plant(value)
        _, resolved = self.snapshots.resolve("fixture", name, "owner")
        self.assertEqual(resolved["future_field"], {"nested": [1, 2]})
        self.assertEqual(self.listed(name), value)
        self.assert_unchanged(name, canonical(value))

    def test_bool_and_float_schema_versions_compare_equal_today(self) -> None:
        for version in (True, 1.0):
            with self.subTest(version=version):
                value = self.changed(schema_version=version)
                name = self.plant(value)
                _, resolved = self.snapshots.resolve("fixture", name, "owner")
                self.assertEqual(resolved["schema_version"], version)
                self.assertEqual(self.listed(name), value)

    def test_unsupported_schema_version(self) -> None:
        for version in (0, 3, "1", None, [1]):
            with self.subTest(version=version):
                name = self.plant(self.changed(schema_version=version))
                self.assertEqual(self.resolve_code(name), "source_receipt_not_owned")
                self.assertIsNone(self.listed(name))

    def test_missing_key_in_resolve_is_unavailable(self) -> None:
        for key in (
            "schema_version",
            "principal",
            "project_id",
            "registration_sha256",
            "files",
            "source_sha256",
        ):
            with self.subTest(key=key):
                name = self.plant(self.without(key))
                self.assertEqual(self.resolve_code(name), "source_snapshot_unavailable")

    def test_keys_resolve_never_reads_are_not_required_by_it(self) -> None:
        for key in (
            "fetch_job_id",
            "repository",
            "commit",
            "tree_sha1",
            "archive_sha256",
        ):
            with self.subTest(key=key):
                value = self.without(key)
                _, resolved = self.snapshots.resolve("fixture", self.plant(value), "owner")
                self.assertEqual(resolved, value)

    def test_missing_key_in_metadata_returns_none_or_the_receipt(self) -> None:
        for key in (
            "schema_version",
            "principal",
            "project_id",
            "registration_sha256",
            "repository",
            "commit",
            "fetch_job_id",
        ):
            with self.subTest(key=key):
                self.assertIsNone(self.listed(self.plant(self.without(key))))
        for key in ("files", "source_sha256", "tree_sha1", "archive_sha256"):
            with self.subTest(key=key):
                value = self.without(key)
                self.assertEqual(self.listed(self.plant(value)), value)

    def test_resolve_check_order_decides_the_reason_code(self) -> None:
        cases = [
            # The version check short-circuits before the principal is indexed.
            (self.without("principal") | {"schema_version": 3}, "copy", "source_receipt_not_owned"),
            # A foreign principal short-circuits before the project_id is indexed.
            (
                self.without("project_id") | {"principal": "other"},
                "copy",
                "source_receipt_not_owned",
            ),
            # An unhashable digest is a TypeError inside the ownership check.
            (self.changed(registration_sha256=["x"]), "copy", "source_snapshot_unavailable"),
            # Eviction is decided before the file table is read.
            (self.without("files"), "none", "source_snapshot_evicted_fetch_required"),
            # A non-dict file table only fails on lookup; with no files it is a mismatch.
            (self.changed(files=[]), "empty", "source_snapshot_changed"),
            (self.changed(files=[]), "copy", "source_snapshot_unavailable"),
            (self.changed(files={}), "copy", "source_snapshot_changed"),
            (self.changed(source_sha256="0" * 64), "copy", "source_snapshot_changed"),
        ]
        for index, (value, source, code) in enumerate(cases):
            with self.subTest(code=code, source=source):
                # The extra key only keeps the planted names distinct.
                name = self.plant(value | {"case": index}, source=source)
                self.assertEqual(self.resolve_code(name), code)

    def test_metadata_compares_instead_of_hashing_unhashable_values(self) -> None:
        self.assertIsNone(self.listed(self.plant(self.changed(registration_sha256=["x"]))))

    def test_non_object_json(self) -> None:
        for document in ([], [1], "receipt", 1, None):
            with self.subTest(document=document):
                name = self.plant(document)
                self.assertEqual(self.resolve_code(name), "source_snapshot_unavailable")
                with self.assertRaises(AttributeError):
                    self.listed(name)
                self.assert_unchanged(name, canonical(document))

    def test_invalid_json_bytes(self) -> None:
        name = self.plant(raw=b"{not json")
        self.assertEqual(self.resolve_code(name), "source_snapshot_unavailable")
        with self.assertRaises(json.JSONDecodeError):
            self.listed(name)
        self.assert_unchanged(name, b"{not json")

    def test_wrong_owner_project_and_digest(self) -> None:
        self.assertEqual(self.resolve_code(self.receipt, "other"), "source_receipt_not_owned")
        for update in ({"project_id": "other"}, {"registration_sha256": "0" * 64}):
            with self.subTest(update=update):
                name = self.plant(self.changed(**update))
                self.assertEqual(self.resolve_code(name), "source_receipt_not_owned")
                self.assertIsNone(self.listed(name))
        name = self.plant(self.changed(repository="fixture/other"))
        self.assertIsNone(self.listed(name))
        self.assertIsNotNone(self.snapshots.resolve("fixture", name, "owner"))


if __name__ == "__main__":
    unittest.main()
