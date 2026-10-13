"""Fetch the real pinned public target in CI; no SDK build, USB, or flash."""

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from cardputer_mcp.config import BuildProject, RegisteredBuildTool, Settings, SourceRepository
from cardputer_mcp.firmware_build import FirmwareBuild, hash_file
from cardputer_mcp.source_snapshot import SourceSnapshots

COMMIT = "6a139780b7eeececfaa17cf2ddee576a10a027f5"


def main():
    # A short Windows root is required for the target's existing long source names.
    parent = os.environ.get("SYSTEMDRIVE", "C:") + "\\" if os.name == "nt" else None
    root = Path(tempfile.mkdtemp(prefix="ss-", dir=parent))
    try:
        profile = root / "profile.ps1"
        profile.write_text("CI preflight only; never executed")
        node = shutil.which("node.exe") if os.name == "nt" else None
        registered = None
        if node is not None:
            version = subprocess.check_output([node, "--version"], text=True).strip()
            registered = RegisteredBuildTool(executable=Path(node), sha256=hash_file(Path(node)), version=version)
        settings = Settings(state_dir=root / "state", build_projects={"cardputer": BuildProject(
            project_dir=root / "worktree", trusted_profile=profile, build_root=root / "out",
            node_tool=registered, build_min_free_bytes=0,
            source_repository=SourceRepository(
                github_repository="dj-oyu/cardputer-adv-pocketjs", cache_root=root / "cache",
                private_acl_confirmed=True,
            ),
        )})
        sources = SourceSnapshots(settings)
        plan = sources.prepare("cardputer", COMMIT, "ci-owner") | {"job_id": "public-fetch"}
        result = sources.fetch(plan, lambda: True, lambda _: None)
        source, metadata = sources.resolve("cardputer", result["source_receipt"], "ci-owner")
        for required in ("components/quickjs-ng/CMakeLists.txt", "components/pocketjs_guest/CMakeLists.txt", "tools/make_app_chunks.py", "apps/derby/derby_play.js"):
            assert (source / required).is_file(), required
        assert metadata["tree_sha1"] == "f3e2d2a5496800dd705b7a02f35a4b9e66801f72"
        assert not (root / "worktree").exists()
        if os.name == "nt":
            build = FirmwareBuild(settings).prepare("cardputer", "ci-owner", result["source_receipt"])
            assert build["source_provenance"]["commit"] == COMMIT
        print(json.dumps(result | {"source_cache": sources.usage("cardputer"), "sdk_build": False, "usb": False}, sort_keys=True))
    finally:
        shutil.rmtree(root)


if __name__ == "__main__":
    main()
