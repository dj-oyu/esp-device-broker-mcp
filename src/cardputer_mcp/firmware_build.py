"""Registered external ESP-IDF 6.0.1 build; no flash or USB operation."""

import asyncio
import hashlib
import json
import os
import re
import shutil
import threading
from pathlib import Path
from typing import Any

from .build_logs import BuildLogs
from .build_process import BuildProcess

ALLOWED_ENV = frozenset({
    "IDF_PATH", "IDF_PYTHON_ENV_PATH", "IDF_TOOLS_PATH", "IDF_TOOLS_EXPORT_CMD",
    "IDF_TOOLS_INSTALL_CMD", "IDF_TARGET", "ESP_IDF_VERSION", "PATH", "PATHEXT",
    "SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "TEMP", "TMP", "COMSPEC",
    "USERPROFILE", "APPDATA", "LOCALAPPDATA", "HOME",
})
OS_ENV = ALLOWED_ENV - {"IDF_PATH", "IDF_PYTHON_ENV_PATH", "IDF_TOOLS_PATH",
                        "IDF_TOOLS_EXPORT_CMD", "IDF_TOOLS_INSTALL_CMD",
                        "IDF_TARGET", "ESP_IDF_VERSION", "PATH"}


def parse_profile(text: str) -> dict[str, str]:
    if len(text.encode("utf-8")) > 131072:
        raise ValueError("profile_output_limit")
    result: dict[str, str] = {}
    seen: set[str] = set()
    for line in text.splitlines():
        if not line:
            continue
        key, separator, value = line.partition("=")
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError("invalid_profile_environment")
        key = key.upper()
        if key in seen or "\0" in value:
            raise ValueError("invalid_profile_environment")
        seen.add(key)
        # Unknown variables never reach the build; their values are not retained.
        if key in ALLOWED_ENV:
            result[key] = value
    if not all(result.get(key) for key in ("IDF_PATH", "IDF_PYTHON_ENV_PATH", "PATH")):
        raise ValueError("profile_missing_environment")
    return result


def source_fingerprint(directory: Path) -> str:
    digest = hashlib.sha256()
    count = total = 0
    for path in sorted(directory.rglob("*")):
        relative = path.relative_to(directory)
        if any(part in (".git", ".state", "__pycache__", "build") or part.startswith("build-")
               for part in relative.parts):
            continue
        if path.is_symlink():
            raise ValueError("linked_project_not_supported")
        if not path.is_file():
            continue
        count += 1
        total += path.stat().st_size
        if count > 100000 or total > 268435456:
            raise ValueError("project_snapshot_limit")
        digest.update(relative.as_posix().encode() + b"\0")
        digest.update(bytes.fromhex(hash_file(path)))
    return digest.hexdigest()


def hash_file(path: Path) -> str:
    if path.is_symlink():
        raise ValueError("linked_file_not_supported")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


class FirmwareBuild:
    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self.process = BuildProcess()
        self.logs: dict[str, BuildLogs] = {}
        self.manifests: dict[str, tuple[str, dict[str, Any]]] = {}
        self._lock = threading.RLock()

    def prepare(self, project_id: str | None, principal: str) -> dict[str, Any]:
        project = self.settings.build_projects.get(project_id)
        if project is None or not principal:
            raise ValueError("registered_build_project_required")
        if not (project.project_dir / "CMakeLists.txt").is_file():
            raise ValueError("idf_project_required")
        fingerprint = hashlib.sha256(json.dumps({
            "source": source_fingerprint(project.project_dir),
            "profile": hash_file(project.trusted_profile),
            "version": project.expected_idf, "target": project.target,
        }, sort_keys=True).encode()).hexdigest()
        return dict(project_id=project_id, fingerprint=fingerprint,
                    expected_idf=project.expected_idf, target=project.target,
                    build_principal=principal,
                    stages=["idf_profile", "idf_diagnostics", "idf_build"],
                    effects=["Executes registered trusted profile and project build scripts",
                             "Runs external ESP-IDF 6.0.1; no flash, USB or version fallback",
                             "Delivers bounded redacted output to requesting principal"],
                    argv_template=["registered-python", "registered-idf.py", "-B",
                                   "fingerprint-build-directory", "build"])

    async def execute(self, plan: dict[str, Any], stage: Any) -> str:
        self.process.stop_requested.clear()
        loop = asyncio.get_running_loop()
        return await asyncio.to_thread(
            self._execute, plan, lambda name: loop.call_soon_threadsafe(stage, name)
        )

    def _execute(self, plan: dict[str, Any], stage: Any) -> str:
        if os.name != "nt":
            raise ValueError("external_windows_idf_required")
        principal = plan["build_principal"]
        if self.prepare(plan["project_id"], principal)["fingerprint"] != plan["fingerprint"]:
            raise ValueError("build_inputs_changed")
        project = self.settings.build_projects[plan["project_id"]]
        logs = BuildLogs(principal)
        logs.redactions = tuple(value for key, value in os.environ.items()
                                if re.search(r"(?i)token|secret|password|key|bearer", key)
                                and len(value) >= 8)
        with self._lock:
            while len(self.logs) >= 8:
                oldest = next(iter(self.logs))
                del self.logs[oldest]
                self.manifests.pop(oldest, None)
            self.logs[plan["job_id"]] = logs
        env = {key.upper(): value for key, value in os.environ.items() if key.upper() in OS_ENV}
        system = Path(env.get("SYSTEMROOT", ""))
        powershell = system / "System32/WindowsPowerShell/v1.0/powershell.exe"
        if not system.is_absolute() or not powershell.is_file():
            raise ValueError("trusted_powershell_missing")
        stage("idf_profile")
        output: list[str] = []
        output_bytes = 0
        def profile_output(line: str, stream: str) -> None:
            nonlocal output_bytes
            output_bytes += len(line.encode("utf-8")) + 1
            if output_bytes > 131072 or stream != "stdout":
                self.process.stop_requested.set()
                return
            output.append(line)
        code = self.process.run([str(powershell), "-NoProfile", "-File",
                                 str(project.trusted_profile), "-e"], env,
                                project.project_dir, profile_output, timeout=30)
        if code or self.process.stop_requested.is_set():
            raise ValueError("profile_export_failed")
        env.update(parse_profile("\n".join(output)))
        output.clear()
        idf = Path(env["IDF_PATH"])
        python = Path(env["IDF_PYTHON_ENV_PATH"]) / "Scripts/python.exe"
        idf_py = idf / "tools/idf.py"
        if (not idf.is_absolute() or not python.is_absolute()
            or not python.is_file() or not idf_py.is_file()
            or python.is_symlink() or idf_py.is_symlink()):
            raise ValueError("external_idf_environment_invalid")
        env["IDF_TARGET"] = project.target
        stage("idf_diagnostics")
        version: list[str] = []
        def version_output(line: str, stream: str) -> None:
            if len(version) >= 32:
                self.process.stop_requested.set()
            else:
                version.append(line)
        if self.process.run([str(python), str(idf_py), "--version"], env,
                            project.project_dir, version_output, timeout=30):
            raise ValueError("idf_version_diagnostic_failed")
        if not any(re.fullmatch(r"ESP-IDF v6\.0\.1", line.strip()) for line in version):
            raise ValueError("idf_version_mismatch_no_fallback")
        version.clear()
        if self.process.run([str(python), "--version"], env, project.project_dir,
                            version_output, timeout=30):
            raise ValueError("external_python_diagnostic_failed")
        compiler = shutil.which("xtensa-esp32s3-elf-gcc.exe", path=env["PATH"])
        if compiler is None:
            raise ValueError("external_toolchain_missing")
        version.clear()
        if self.process.run([compiler, "--version"], env, project.project_dir,
                            version_output, timeout=30):
            raise ValueError("external_toolchain_diagnostic_failed")
        environment_fingerprint = hashlib.sha256(json.dumps({
            "idf_py": hash_file(idf_py), "python": hash_file(python),
            "compiler": hash_file(Path(compiler)), "environment": env,
        }, sort_keys=True).encode()).hexdigest()
        logs.append("-- External Python, toolchain and ESP-IDF 6.0.1 diagnostics passed")
        build_dir = (self.settings.state_dir.resolve() / "firmware-builds"
                     / plan["fingerprint"] / environment_fingerprint)
        build_dir.mkdir(parents=True, exist_ok=True)
        stage("idf_build")
        def emit(line: str, stream: str) -> None:
            with self._lock:
                logs.append(line, stream)
        if self.process.run([str(python), str(idf_py), "-B", str(build_dir), "build"],
                            env, project.project_dir, emit):
            raise ValueError("idf_build_failed_needs_review")
        if self.prepare(plan["project_id"], principal)["fingerprint"] != plan["fingerprint"]:
            raise ValueError("build_inputs_changed")
        description = json.loads((build_dir / "project_description.json").read_text())
        if description.get("target") != project.target:
            raise ValueError("build_target_mismatch")
        artifacts = []
        for path in sorted(build_dir.rglob("*")):
            if path.suffix in (".bin", ".elf") and path.is_file():
                if len(artifacts) >= 32:
                    raise ValueError("artifact_count_limit")
                artifacts.append(dict(name=path.relative_to(build_dir).as_posix(),
                                      bytes=path.stat().st_size, sha256=hash_file(path)))
        if not artifacts:
            raise ValueError("build_artifacts_missing")
        manifest = dict(schema_version=1, idf_version="6.0.1", target=project.target,
                        fingerprint=plan["fingerprint"],
                        environment_fingerprint=environment_fingerprint,
                        artifacts=artifacts,
                        flash_authorized=False)
        with self._lock:
            self.manifests[plan["job_id"]] = (principal, manifest)
        (build_dir / "broker-manifest.json").write_text(json.dumps(manifest, indent=2))
        logs.append("Build complete; artifact hashes recorded; flash requires separate approval")
        return "firmware_build_succeeded_no_flash"

    def read_logs(self, request: Any, principal: str) -> dict[str, Any]:
        with self._lock:
            logs = self.logs.get(request.job_id)
            if logs is None:
                raise ValueError("build_log_not_available")
            result = logs.read(principal, request)
            if result["expired"]:
                self.manifests.pop(request.job_id, None)
                logs.redactions = ()
            manifest = self.manifests.get(request.job_id)
            if manifest and manifest[0] == principal and not result["expired"]:
                result["manifest"] = manifest[1]
            return result
