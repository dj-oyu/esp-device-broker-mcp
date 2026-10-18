"""Create and verify an allowlisted Windows ZIP using only the standard library."""

import argparse
import hashlib
import json
import platform
import re
import struct
import subprocess
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAYLOAD = (
    "cardputer.exe",
    "LICENSE",
    "NOTICE",
    "WINDOWS-DISTRIBUTION.md",
    "examples/mock-safe.toml",
)
MEMBERS = (*PAYLOAD, "build-runtime.json", "manifest.json", "SHA256SUMS.txt")
RUNTIME_KEYS = {
    "schema_version",
    "python_version",
    "uv_version",
    "source_commit",
    "uv_lock_sha256",
    "build_requirements_sha256",
    "exe_sha256",
}


def record_build_runtime(exe: Path, root: Path = ROOT) -> Path:
    """Called by the actual PyInstaller interpreter after producing the EXE."""
    if subprocess.check_output(
        ["git", "status", "--porcelain", "--untracked-files=no"], cwd=root, text=True
    ).strip():
        raise ValueError("build_source_must_be_clean")
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    uv_version = subprocess.check_output(["uv", "--version"], text=True).split()[1]
    runtime = {
        "schema_version": 1,
        "python_version": platform.python_version(),
        "uv_version": uv_version,
        "source_commit": commit,
        "uv_lock_sha256": digest((root / "uv.lock").read_bytes()),
        "build_requirements_sha256": digest(
            (root / "packaging/requirements-windows-build.txt").read_bytes()
        ),
        "exe_sha256": digest(exe.read_bytes()),
    }
    target = Path(str(exe) + ".build.json")
    target.write_text(json.dumps(runtime, sort_keys=True) + "\n", encoding="utf-8")
    return target


def verify_build_runtime(data: bytes, executable: bytes, commit: str, root: Path) -> dict:
    """Verify producer metadata and the EXE's ABI, never the verifier's runtime."""
    if len(data) > 4096:
        raise ValueError("build_runtime_invalid")
    runtime = json.loads(data)
    if not isinstance(runtime, dict) or runtime.keys() != RUNTIME_KEYS:
        raise ValueError("build_runtime_invalid")
    if (
        type(runtime["schema_version"]) is not int
        or runtime["schema_version"] != 1
        or any(type(runtime[key]) is not str for key in RUNTIME_KEYS - {"schema_version"})
        or re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", runtime["python_version"]) is None
        or re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", runtime["uv_version"]) is None
        or runtime["source_commit"] != commit
        or runtime["exe_sha256"] != digest(executable)
        or runtime["uv_lock_sha256"] != digest((root / "uv.lock").read_bytes())
        or runtime["build_requirements_sha256"]
        != digest((root / "packaging/requirements-windows-build.txt").read_bytes())
    ):
        raise ValueError("build_runtime_binding_mismatch")
    # PyInstaller 6's CArchive cookie records the embedded Python ABI/library.
    # This prevents a 3.14 EXE from being described as a 3.13 build even if the
    # sidecar version was edited. Full patch version comes from the producer.
    magic, cookie_format = b"MEI\014\013\012\013\016", "!8sIIII64s"
    at = executable.rfind(magic)
    length = struct.calcsize(cookie_format)
    if at < 0 or at + length > len(executable):
        raise ValueError("build_runtime_cookie_missing")
    _, package, toc, toc_length, abi, library = struct.unpack_from(cookie_format, executable, at)
    major, minor, _ = map(int, runtime["python_version"].split("."))
    if (
        abi != major * 100 + minor
        or library.rstrip(b"\0") != f"python{major}{minor}.dll".encode()
        or package < length
        or package > at + length
        or toc + toc_length > package - length
    ):
        raise ValueError("build_runtime_abi_mismatch")
    return runtime


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def release_version(ref: str, root: Path = ROOT) -> str:
    version = tomllib.loads((root / "pyproject.toml").read_text("utf-8"))["project"]["version"]
    if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version) is None:
        raise ValueError("release_version_must_be_three_numeric_components")
    if ref.startswith("refs/tags/") and ref != f"refs/tags/v{version}":
        raise ValueError("tag_does_not_match_project_version")
    # These wire/display versions are currently explicit application constants.
    for path, pattern in (
        ("src/cardputer_mcp/server.py", r'version="([^"]+)"'),
        ("src/cardputer_mcp/mcp_diagnostics.py", r'GUI_CODE_VERSION = "([^"]+)"'),
    ):
        match = re.search(pattern, (root / path).read_text("utf-8"))
        if match is None or match[1] != version:
            raise ValueError("application_version_mismatch")
    locked = tomllib.loads((root / "uv.lock").read_text("utf-8"))
    project = next(p for p in locked["package"] if p["name"] == "cardputer-device-mcp")
    if project["version"] != version:
        raise ValueError("locked_project_version_mismatch")
    return version


def check_pe(data: bytes) -> None:
    if len(data) < 64 or data[:2] != b"MZ":
        raise ValueError("expected_windows_executable")
    offset = struct.unpack_from("<I", data, 60)[0]
    if (
        offset + 6 > len(data)
        or data[offset : offset + 4] != b"PE\0\0"
        or struct.unpack_from("<H", data, offset + 4)[0] != 0x8664
    ):
        raise ValueError("expected_windows_x64_executable")


def create_archive(exe: Path, output: Path, ref: str, commit: str, root: Path = ROOT) -> Path:
    version = release_version(ref, root)
    if re.fullmatch(r"[a-f0-9]{40}", commit) is None:
        raise ValueError("expected_full_source_commit_sha")
    payload = {
        name: (exe if name == "cardputer.exe" else root / name).read_bytes() for name in PAYLOAD
    }
    check_pe(payload["cardputer.exe"])
    runtime_bytes = Path(str(exe) + ".build.json").read_bytes()
    runtime = verify_build_runtime(runtime_bytes, payload["cardputer.exe"], commit, root)
    payload["build-runtime.json"] = runtime_bytes
    manifest = {
        "schema_version": 1,
        "version": version,
        "source_commit": commit,
        "source_ref": ref,
        "platform": "windows-x64",
        "python_version": runtime["python_version"],
        "uv_version": runtime["uv_version"],
        "build_runtime_sha256": digest(runtime_bytes),
        "uv_lock_sha256": digest((root / "uv.lock").read_bytes()),
        "build_requirements_sha256": digest(
            (root / "packaging/requirements-windows-build.txt").read_bytes()
        ),
        "files": {name: digest(payload[name]) for name in PAYLOAD},
    }
    payload["manifest.json"] = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    payload["SHA256SUMS.txt"] = "".join(
        f"{digest(data)}  {name}\n" for name, data in sorted(payload.items())
    ).encode()
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f"cardputer-v{version}-windows-x64.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as handle:
        for name, data in payload.items():
            info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            handle.writestr(info, data)
    verify_archive(archive, ref, commit, root)
    sums = output / (archive.name + ".sha256")
    sums.write_text(f"{digest(archive.read_bytes())}  {archive.name}\n", encoding="utf-8")
    return archive


def verify_archive(archive: Path, ref: str, commit: str, root: Path = ROOT) -> dict:
    version = release_version(ref, root)
    with zipfile.ZipFile(archive) as handle:
        if sorted(handle.namelist()) != sorted(MEMBERS) or handle.testzip() is not None:
            raise ValueError("unexpected_or_corrupt_zip_members")
        payload = {name: handle.read(name) for name in MEMBERS}
    check_pe(payload["cardputer.exe"])
    manifest = json.loads(payload["manifest.json"])
    if (
        manifest["version"] != version
        or manifest["source_commit"] != commit
        or manifest["source_ref"] != ref
        or manifest["platform"] != "windows-x64"
        or manifest["build_runtime_sha256"] != digest(payload["build-runtime.json"])
        or manifest["uv_lock_sha256"] != digest((root / "uv.lock").read_bytes())
        or manifest["build_requirements_sha256"]
        != digest((root / "packaging/requirements-windows-build.txt").read_bytes())
        or set(manifest["files"]) != set(PAYLOAD)
    ):
        raise ValueError("archive_manifest_mismatch")
    if any(digest(payload[name]) != manifest["files"][name] for name in PAYLOAD):
        raise ValueError("archive_payload_hash_mismatch")
    runtime = verify_build_runtime(
        payload["build-runtime.json"], payload["cardputer.exe"], commit, root
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
