"""Offline archives and immutable-source receipts; no downloads or SDK installs."""

import base64
import io
import json
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from cardputer_mcp.config import BuildProject, Settings, SourceRepository
from cardputer_mcp.firmware_build import FirmwareBuild
from cardputer_mcp.models import PrepareRequest
from cardputer_mcp.models import TestName as Action
from cardputer_mcp.source_snapshot import (
    SourceError,
    SourceSnapshots,
    extract,
    file_receipt,
    tree_hash,
)

COMMIT = "a" * 40
REPOSITORY = "fixture/project"
FILES = {
    "CMakeLists.txt": b"project(synthetic)\n",
    "sdkconfig.defaults": b"CONFIG_IDF_TARGET=esp32s3\n",
}


def archive(files=FILES, *, mode=stat.S_IFREG | 0o644, repository=REPOSITORY, commit=COMMIT):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as zipped:
        for name, data in files.items():
            entry = zipfile.ZipInfo(repository.split("/")[1] + "-" + commit + "/" + name)
            entry.external_attr = mode << 16
            zipped.writestr(entry, data)
    return stream.getvalue()


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.profile = self.root / "profile.ps1"
        self.profile.write_text("synthetic profile")
        self.settings = Settings(
            state_dir=self.root / "state",
            build_projects={
                "fixture": BuildProject(
                    project_dir=self.root / "user-worktree",
                    trusted_profile=self.profile,
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
        receipts = {name: file_receipt(data, "100644") for name, data in FILES.items()}
        self.tree = tree_hash(receipts)
        self.urls = []

        def transport(url, deadline, live, limit):
            self.urls.append(url)
            if not live():
                raise SourceError("source_fetch_interrupted")
            if url.startswith("https://api.github.com/"):
                return json.dumps({"sha": COMMIT, "tree": {"sha": self.tree}}).encode()
            return archive()

        self.snapshots = SourceSnapshots(self.settings, transport)

    def tearDown(self):
        self.temp.cleanup()

    def fetch(self, principal="owner", job="fetch-one"):
        plan = self.snapshots.prepare("fixture", COMMIT, principal) | {"job_id": job}
        return self.snapshots.fetch(plan, lambda: True, lambda _: None)

    def test_explicit_fetch_and_receipt_build_preserve_user_worktree(self):
        self.assertEqual(self.snapshots.usage("fixture")["bytes"], 0)
        self.assertFalse((self.root / "cache").exists())
        result = self.fetch()
        source, metadata = self.snapshots.resolve("fixture", result["source_receipt"], "owner")
        self.assertEqual(metadata["tree_sha1"], self.tree)
        self.assertEqual(metadata["commit"], COMMIT)
        self.assertFalse((self.root / "user-worktree").exists())
        self.assertEqual((source / "CMakeLists.txt").read_bytes(), FILES["CMakeLists.txt"])
        plan = FirmwareBuild(self.settings).prepare("fixture", "owner", result["source_receipt"])
        self.assertEqual(plan["source_provenance"]["commit"], COMMIT)
        self.assertEqual(plan["source_provenance"]["source_sha256"], result["source_sha256"])
        self.assertEqual(len(self.urls), 2)
        self.assertEqual(self.snapshots.usage("fixture")["snapshots"], 1)

    def test_principal_tampering_and_untracked_files_are_rejected(self):
        result = self.fetch()
        receipt = result["source_receipt"]
        with self.assertRaises(SourceError):
            self.snapshots.resolve("fixture", receipt, "other")
        source, _ = self.snapshots.resolve("fixture", receipt, "owner")
        (source / "unknown.c").write_bytes(b"unknown")
        with self.assertRaises(SourceError):
            self.snapshots.resolve("fixture", receipt, "owner")
        (source / "unknown.c").unlink()
        (source / "CMakeLists.txt").write_bytes(b"tampered")
        with self.assertRaises(SourceError):
            FirmwareBuild(self.settings).prepare("fixture", "owner", receipt)

    def test_git_tree_mismatch_and_revocation_publish_nothing(self):
        self.tree = "b" * 40
        with self.assertRaises(SourceError):
            self.fetch()
        self.assertEqual(self.snapshots.usage("fixture")["snapshots"], 0)
        plan = self.snapshots.prepare("fixture", COMMIT, "owner") | {"job_id": "revoked"}
        with self.assertRaises(SourceError):
            self.snapshots.fetch(plan, lambda: False, lambda _: None)
        self.assertEqual(self.snapshots.usage("fixture")["snapshots"], 0)

    def test_traversal_windows_aliases_symlinks_and_lfs_are_rejected(self):
        for files in (
            {"../outside": b"escape"},
            {"CON.txt": b"alias"},
            {"a": b"one", "A": b"two"},
            {".gitmodules": b"module"},
            {"large.bin": b"version https://git-lfs.github.com/spec/v1\n"},
        ):
            with tempfile.TemporaryDirectory() as folder:
                with self.assertRaises(SourceError):
                    extract(archive(files), Path(folder), "project-" + COMMIT, lambda: True)
        with self.assertRaises(SourceError):
            extract(
                archive(mode=stat.S_IFLNK | 0o777), self.root, "project-" + COMMIT, lambda: True
            )

    def test_expansion_and_cache_limits_fail_closed(self):
        with patch.dict("cardputer_mcp.source_snapshot.LIMITS", expanded_bytes=3):
            with self.assertRaises(SourceError):
                self.fetch()
        cache = self.root / "cache"
        for number in range(16):
            (cache / str(number)).mkdir()
        with self.assertRaisesRegex(SourceError, "source_cache_full"):
            self.fetch()

    def test_snapshot_count_report_and_admission_share_limit(self):
        class ReachedAdmission(Exception):
            pass

        cache = self.root / "cache"
        cache.mkdir(mode=0o700)
        plan = self.snapshots.prepare("fixture", COMMIT, "owner")
        self.assertEqual(plan["limits"]["cache_snapshots"], 16)
        created = 0
        for count in (8, 16, 17):
            for number in range(created, count):
                (cache / str(number)).mkdir()
            created = count
            usage = self.snapshots.usage("fixture")
            self.assertEqual(usage["snapshots"], count)
            self.assertEqual(usage["max_snapshots"], 16)
            self.assertEqual(usage["max_bytes"], 2 * 1024 * 1048576)
            with patch(
                "cardputer_mcp.source_snapshot.shutil.disk_usage", side_effect=ReachedAdmission
            ) as reserve:
                if count < 16:
                    with self.assertRaises(ReachedAdmission):
                        self.fetch()
                    reserve.assert_called_once()
                else:
                    with self.assertRaisesRegex(SourceError, "source_cache_full"):
                        self.fetch()
                    reserve.assert_not_called()

    def test_snapshot_byte_limit_still_rejects_oversized_cache(self):
        cache = self.root / "cache"
        cache.mkdir(mode=0o700)
        snapshot = cache / "one"
        snapshot.mkdir()
        (snapshot / "payload").write_bytes(b"12345")
        with patch.dict("cardputer_mcp.source_snapshot.LIMITS", cache_bytes=4):
            with self.assertRaisesRegex(SourceError, "source_cache_limit"):
                self.snapshots.usage("fixture")
        self.assertEqual(self.snapshots.usage("fixture")["bytes"], 5)
        self.assertEqual(self.snapshots.usage("fixture")["max_bytes"], 2 * 1024 * 1048576)

    def test_snapshot_admission_still_reserves_byte_headroom(self):
        cache = self.root / "cache"
        cache.mkdir(mode=0o700)
        snapshot = cache / "one"
        snapshot.mkdir()
        (snapshot / "payload").write_bytes(b"1234")
        with patch.dict(
            "cardputer_mcp.source_snapshot.LIMITS",
            cache_bytes=32 * 1048576 + 3,
            expanded_bytes=1,
        ):
            self.assertEqual(self.snapshots.usage("fixture")["bytes"], 4)
            with self.assertRaisesRegex(SourceError, "source_cache_full"):
                self.fetch()

    def test_typed_contract_has_no_url_path_or_short_commit(self):
        valid = PrepareRequest(
            session="c" * 64,
            job_id="one",
            project_id="fixture",
            test=Action.SOURCE,
            source_commit=COMMIT,
        )
        self.assertEqual(valid.source_commit, COMMIT)
        for arguments in (
            {"source_commit": "a" * 8},
            {"source_commit": COMMIT, "url": "https://example.test"},
            {"source_commit": COMMIT, "source_receipt": "b" * 64},
        ):
            with self.assertRaises(ValueError):
                PrepareRequest(
                    session="c" * 64,
                    job_id="one",
                    project_id="fixture",
                    test=Action.SOURCE,
                    **arguments,
                )

    def submodule_fixture(self, *, nested=False, modules=None, gitlink=None):
        child_repo, child_commit = "lieff/minimp3", "b" * 40
        nested_repo, nested_commit = "fixture/nested", "c" * 40
        root_files = dict(FILES)
        root_files[".gitmodules"] = modules or (
            b'[submodule "minimp3"]\npath = third_party/minimp3\n'
            b"url = https://github.com/lieff/minimp3.git\n"
        )
        child_files = {"minimp3.h": b"synthetic upstream header"}
        nested_files = {"nested.h": b"nested header"}
        if nested:
            child_files[".gitmodules"] = (
                b'[submodule "nested"]\npath = nested\n'
                b"url = https://github.com/fixture/nested.git\n"
            )
        graph = {
            (REPOSITORY, COMMIT): (root_files, {"third_party/minimp3": gitlink or child_commit}),
            (child_repo, child_commit): (child_files, {"nested": nested_commit} if nested else {}),
            (nested_repo, nested_commit): (nested_files, {}),
        }
        trees = {}
        for identity, (files, links) in graph.items():
            receipts = {name: file_receipt(data, "100644") for name, data in files.items()}
            receipts.update(
                {name: {"mode": "160000", "git_sha1": sha} for name, sha in links.items()}
            )
            trees[identity] = tree_hash(receipts)
        registration = self.settings.build_projects["fixture"].source_repository
        registered = registration.model_copy(
            update={"submodule_repositories": [child_repo, nested_repo]}
        )
        project = self.settings.build_projects["fixture"].model_copy(
            update={"source_repository": registered}
        )
        self.settings = self.settings.model_copy(update={"build_projects": {"fixture": project}})
        self.snapshots.settings = self.settings

        def transport(url, deadline, live, limit):
            self.urls.append(url)
            if "/git/commits/" in url:
                repository, commit = url.removeprefix("https://api.github.com/repos/").split(
                    "/git/commits/"
                )
                return json.dumps(
                    {"sha": commit, "tree": {"sha": trees[repository, commit]}}
                ).encode()
            if "/git/trees/" in url:
                repository, tree = url.removeprefix("https://api.github.com/repos/").split(
                    "/git/trees/"
                )
                tree = tree.removesuffix("?recursive=1")
                identity = next(key for key in graph if key[0] == repository and trees[key] == tree)
                links = graph[identity][1]
                return json.dumps(
                    {
                        "sha": tree,
                        "truncated": False,
                        "tree": [
                            {"path": name, "sha": sha, "type": "commit", "mode": "160000"}
                            for name, sha in links.items()
                        ]
                        + [
                            {
                                "path": name,
                                "sha": file_receipt(data, "100644")["git_sha1"],
                                "mode": "100644",
                                "type": "blob",
                                "size": len(data),
                            }
                            for name, data in graph[identity][0].items()
                        ],
                    }
                ).encode()
            repository, commit = url.removeprefix("https://codeload.github.com/").split("/zip/")
            return archive(graph[repository, commit][0], repository=repository, commit=commit)

        self.snapshots.transport = transport
        return child_commit

    def test_fixed_gitlinks_recursive_receipt_and_build_provenance(self):
        commit = self.submodule_fixture(nested=True)
        result = self.fetch()
        source, metadata = self.snapshots.resolve("fixture", result["source_receipt"], "owner")
        self.assertEqual(metadata["schema_version"], 2)
        self.assertEqual(result["submodules"][0]["commit"], commit)
        self.assertEqual(result["submodules"][0]["path"], "third_party/minimp3")
        self.assertEqual(len(result["submodules"]), 2)
        self.assertEqual(
            (source / "third_party/minimp3/nested/nested.h").read_bytes(), b"nested header"
        )
        plan = FirmwareBuild(self.settings).prepare("fixture", "owner", result["source_receipt"])
        self.assertEqual(plan["source_provenance"]["submodules"], result["submodules"])
        self.assertEqual(
            plan["source_provenance"]["dependency_policy"], "gitlinks_fixed_commits_no_fetch"
        )
        self.assertFalse(any("heads/" in url for url in self.urls))
        (source / "third_party/minimp3/minimp3.h").write_bytes(b"changed")
        with self.assertRaises(SourceError):
            self.snapshots.resolve("fixture", result["source_receipt"], "owner")

    def test_gitmodules_url_commands_paths_and_link_mismatch_rejected(self):
        for extra in (
            b"url = https://example.test/module.git\n",
            b"url = ../minimp3.git\n",
            b"url = https://github.com/unregistered/module.git\n",
            b"url = https://github.com/lieff/minimp3.git\nupdate = !command\n",
            b"url = https://github.com/lieff/minimp3.git\nbranch = main\n",
        ):
            self.submodule_fixture(modules=b'[submodule "m"]\npath = third_party/minimp3\n' + extra)
            with self.assertRaises(SourceError):
                self.fetch()
            self.assertEqual(self.snapshots.usage("fixture")["snapshots"], 0)
        self.submodule_fixture(
            modules=b'[submodule "m"]\npath = ../escape\nurl = https://github.com/lieff/minimp3.git\n'
        )
        with self.assertRaises(SourceError):
            self.fetch()
        self.submodule_fixture(gitlink="d" * 40)
        with self.assertRaises(SourceError):
            self.fetch()

    def test_shared_submodule_limits_and_revocation_publish_nothing(self):
        self.submodule_fixture(nested=True)
        for limits in ({"count": 1}, {"depth": 0}):
            with patch.dict("cardputer_mcp.source_snapshot.SUBMODULE_LIMITS", limits):
                with self.assertRaises(SourceError):
                    self.fetch()
            self.assertEqual(self.snapshots.usage("fixture")["snapshots"], 0)
        with patch.dict("cardputer_mcp.source_snapshot.LIMITS", expanded_bytes=100):
            with self.assertRaises(SourceError):
                self.fetch()
        transport = self.snapshots.transport

        def revoke(url, deadline, live, limit):
            result = transport(url, deadline, live, limit)
            if "lieff/minimp3" in url:
                self.revoked = True
            return result

        self.revoked = False
        self.snapshots.transport = revoke
        plan = self.snapshots.prepare("fixture", COMMIT, "owner") | {"job_id": "revoked"}
        with self.assertRaises(SourceError):
            self.snapshots.fetch(plan, lambda: not self.revoked, lambda _: None)
        self.assertEqual(self.snapshots.usage("fixture")["snapshots"], 0)

    def test_old_receipt_survives_allowlist_addition_without_mutation(self):
        result = self.fetch()
        receipt = result["source_receipt"]
        raw = (self.root / "cache" / receipt / "receipt.json").read_bytes()
        self.submodule_fixture()
        _, metadata = self.snapshots.resolve("fixture", receipt, "owner")
        self.assertEqual(metadata["schema_version"], 1)
        self.assertEqual((self.root / "cache" / receipt / "receipt.json").read_bytes(), raw)

    def archive_restore_fixture(self, *, corrupt=None, tree_edit=None, archive_extra=False):
        truth = dict(FILES) | {"missing.dat": b"binary\x00data", "scripts/build.cmd": b"line\n"}
        receipts = {name: file_receipt(data, "100644") for name, data in truth.items()}
        tree = tree_hash(receipts)
        blobs = {receipts[name]["git_sha1"]: data for name, data in truth.items()}
        entries = [
            {
                "path": name,
                "mode": "100644",
                "type": "blob",
                "sha": value["git_sha1"],
                "size": value["bytes"],
            }
            for name, value in receipts.items()
        ]
        if tree_edit:
            tree_edit(entries)
        exported = {
            "sdkconfig.defaults": FILES["sdkconfig.defaults"],
            "scripts/build.cmd": b"line\r\n",
        }
        if archive_extra:
            exported["not-in-git.txt"] = b"extra"

        def transport(url, deadline, live, limit):
            self.urls.append(url)
            if "/git/commits/" in url:
                return json.dumps({"sha": COMMIT, "tree": {"sha": tree}}).encode()
            if "/git/trees/" in url:
                return json.dumps({"sha": tree, "truncated": False, "tree": entries}).encode()
            if "/git/blobs/" in url:
                sha = url.rsplit("/", 1)[1]
                data = blobs[sha]
                response = {
                    "sha": sha,
                    "size": len(data),
                    "encoding": "base64",
                    "content": base64.b64encode(data).decode(),
                    "url": "https://untrusted.test/secret?token=private",
                }
                if corrupt:
                    corrupt(response)
                return json.dumps(response).encode()
            return archive(exported)

        self.snapshots.transport = transport
        return truth, tree

    def test_archive_missing_and_transformed_files_restore_exact_tree(self):
        truth, tree = self.archive_restore_fixture()
        result = self.fetch()
        source, metadata = self.snapshots.resolve("fixture", result["source_receipt"], "owner")
        self.assertEqual(metadata["tree_sha1"], tree)
        for name, data in truth.items():
            self.assertEqual((source / name).read_bytes(), data)
        self.assertEqual(len([url for url in self.urls if "/git/blobs/" in url]), 3)
        self.assertFalse(any("untrusted.test" in url or "token=" in url for url in self.urls))

    def test_blob_hash_size_encoding_and_untrusted_tree_fail_closed(self):
        for corrupt in (
            lambda item: item.update(content=base64.b64encode(b"secret-value").decode()),
            lambda item: item.update(sha="d" * 40),
            lambda item: item.update(size=True),
            lambda item: item.update(encoding="utf-8"),
            lambda item: item.update(content="not!base64"),
        ):
            self.archive_restore_fixture(corrupt=corrupt)
            with self.assertRaises(SourceError) as failure:
                self.fetch()
            self.assertNotIn("secret-value", str(failure.exception))
            self.assertEqual(self.snapshots.usage("fixture")["snapshots"], 0)
        for edit in (
            lambda items: items[0].update(path="../escape"),
            lambda items: items[0].update(sha="../arbitrary-url"),
            lambda items: items[0].update(mode="120000"),
            lambda items: items.append(dict(items[0])),
        ):
            self.archive_restore_fixture(tree_edit=edit)
            with self.assertRaises(SourceError):
                self.fetch()
            self.assertEqual(self.snapshots.usage("fixture")["snapshots"], 0)
        self.archive_restore_fixture(archive_extra=True)
        with self.assertRaisesRegex(SourceError, "source_archive_untracked_file"):
            self.fetch()

    def test_restored_files_share_limits_and_revocation(self):
        self.archive_restore_fixture()
        for limits in ({"files": 2}, {"expanded_bytes": 60}, {"download_bytes": 100}):
            with patch.dict("cardputer_mcp.source_snapshot.LIMITS", limits):
                with self.assertRaises(SourceError):
                    self.fetch()
            self.assertEqual(self.snapshots.usage("fixture")["snapshots"], 0)
        self.archive_restore_fixture()
        transport = self.snapshots.transport
        self.revoked = False

        def revoke(url, deadline, live, limit):
            response = transport(url, deadline, live, limit)
            if "/git/blobs/" in url:
                self.revoked = True
            return response

        self.snapshots.transport = revoke
        plan = self.snapshots.prepare("fixture", COMMIT, "owner") | {"job_id": "revoked"}
        with self.assertRaises(SourceError):
            self.snapshots.fetch(plan, lambda: not self.revoked, lambda _: None)
        self.assertEqual(self.snapshots.usage("fixture")["snapshots"], 0)


if __name__ == "__main__":
    unittest.main()
