"""Create and verify an allowlisted Linux tarball; version and runtime checks are shared."""

import argparse
import gzip
import io
import json
import re
import sys
import tarfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import release_windows as common  # noqa: E402

ROOT = common.ROOT
REQUIREMENTS = "packaging/requirements-linux-build.txt"
PAYLOAD = (
    "cardputer",
    "LICENSE",
    "NOTICE",
    "LINUX-DISTRIBUTION.md",
    "start-cardputer.sh",
    "start-cardputer-stack.sh",
    "examples/mock-safe.toml",
)
MEMBERS = (*PAYLOAD, "build-runtime.json", "manifest.json", "SHA256SUMS.txt")
digest = common.digest
release_version = common.release_version


def record_build_runtime(exe: Path, root: Path = ROOT) -> Path:
    """Called by the actual PyInstaller interpreter after producing the executable."""
    return common.record_build_runtime(exe, root, REQUIREMENTS)


def linux_library(name: bytes, major: int, minor: int) -> bool:
    return name.startswith(f"libpython{major}.{minor}".encode())


def verify_build_runtime(data: bytes, executable: bytes, commit: str, root: Path) -> dict:
    return common.verify_build_runtime(data, executable, commit, root, REQUIREMENTS, linux_library)


def check_elf(data: bytes) -> None:
    # ELF64, little endian, EM_X86_64.
    if len(data) < 64 or data[:4] != b"\x7fELF" or data[4] != 2 or data[5] != 1:
        raise ValueError("expected_linux_executable")
    if int.from_bytes(data[18:20], "little") != 0x3E:
        raise ValueError("expected_linux_x64_executable")


def archive_name(version: str) -> str:
    return f"cardputer-v{version}-linux-x64.tar.gz"


def write_tar(path: Path, payload: dict[str, bytes]) -> None:
    # Fixed owner, time and modes so the same inputs give the same member bytes.
    with (
        path.open("wb") as raw,
        gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed,
        tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as handle,
    ):
        for name, data in payload.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = 1577836800
            info.mode = 0o755 if name == "cardputer" else 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            handle.addfile(info, io.BytesIO(data))


def read_tar(archive: Path) -> dict[str, bytes]:
    payload: dict[str, bytes] = {}
    with tarfile.open(archive, "r:gz") as handle:
        for info in handle.getmembers():
            if not info.isreg() or info.name in payload or info.name not in MEMBERS:
                raise ValueError("unexpected_or_corrupt_archive_members")
            extracted = handle.extractfile(info)
            if extracted is None:
                raise ValueError("unexpected_or_corrupt_archive_members")
            payload[info.name] = extracted.read()
    if sorted(payload) != sorted(MEMBERS):
        raise ValueError("unexpected_or_corrupt_archive_members")
    return payload


def create_archive(exe: Path, output: Path, ref: str, commit: str, root: Path = ROOT) -> Path:
    version = release_version(ref, root)
    if re.fullmatch(r"[a-f0-9]{40}", commit) is None:
        raise ValueError("expected_full_source_commit_sha")
    payload = {name: (exe if name == "cardputer" else root / name).read_bytes() for name in PAYLOAD}
    check_elf(payload["cardputer"])
    runtime_bytes = Path(str(exe) + ".build.json").read_bytes()
    runtime = verify_build_runtime(runtime_bytes, payload["cardputer"], commit, root)
    payload["build-runtime.json"] = runtime_bytes
    manifest = {
        "schema_version": 1,
        "version": version,
        "source_commit": commit,
        "source_ref": ref,
        "platform": "linux-x64",
        "python_version": runtime["python_version"],
        "uv_version": runtime["uv_version"],
        "build_runtime_sha256": digest(runtime_bytes),
        "uv_lock_sha256": digest((root / "uv.lock").read_bytes()),
        "build_requirements_sha256": digest((root / REQUIREMENTS).read_bytes()),
        "files": {name: digest(payload[name]) for name in PAYLOAD},
    }
    payload["manifest.json"] = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    payload["SHA256SUMS.txt"] = "".join(
        f"{digest(data)}  {name}\n" for name, data in sorted(payload.items())
    ).encode()
    output.mkdir(parents=True, exist_ok=True)
    archive = output / archive_name(version)
    write_tar(archive, payload)
    verify_archive(archive, ref, commit, root)
    sums = output / (archive.name + ".sha256")
    sums.write_text(f"{digest(archive.read_bytes())}  {archive.name}\n", encoding="utf-8")
    return archive


def verify_archive(archive: Path, ref: str, commit: str, root: Path = ROOT) -> dict:
    version = release_version(ref, root)
    payload = read_tar(archive)
    check_elf(payload["cardputer"])
    manifest = json.loads(payload["manifest.json"])
    if (
        manifest["version"] != version
        or manifest["source_commit"] != commit
        or manifest["source_ref"] != ref
        or manifest["platform"] != "linux-x64"
        or manifest["build_runtime_sha256"] != digest(payload["build-runtime.json"])
        or manifest["uv_lock_sha256"] != digest((root / "uv.lock").read_bytes())
        or manifest["build_requirements_sha256"] != digest((root / REQUIREMENTS).read_bytes())
        or set(manifest["files"]) != set(PAYLOAD)
    ):
        raise ValueError("archive_manifest_mismatch")
    if any(digest(payload[name]) != manifest["files"][name] for name in PAYLOAD):
        raise ValueError("archive_payload_hash_mismatch")
    runtime = verify_build_runtime(
        payload["build-runtime.json"], payload["cardputer"], commit, root
    )
    if (
        manifest["python_version"] != runtime["python_version"]
        or manifest["uv_version"] != runtime["uv_version"]
    ):
        raise ValueError("archive_manifest_mismatch")
    expected = "".join(
        f"{digest(data)}  {name}\n"
        for name, data in sorted(payload.items())
        if name != "SHA256SUMS.txt"
    ).encode()
    if payload["SHA256SUMS.txt"] != expected:
        raise ValueError("archive_checksums_mismatch")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--exe", type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    parser.add_argument("--check-version", action="store_true")
    args = parser.parse_args()
    if args.check_version:
        print(release_version(args.ref))
    elif args.exe is None:
        parser.error("--exe is required unless --check-version is used")
    else:
        print(create_archive(args.exe, args.output, args.ref, args.commit))


if __name__ == "__main__":
    main()
