"""Registered external ESP-IDF 6.0.1 build; no flash or USB operation."""

import asyncio
import hashlib
import json
import os
import re
import shutil
import stat
import threading
from collections.abc import Callable
from functools import partial
from pathlib import Path, PurePosixPath
from typing import Any

from .build_artifacts import BuildArtifacts
from .build_locations import (
    BuildLocationError,
    build_directory,
    build_location,
    check_storage_before_write,
    check_windows_path,
    claim_build_directory,
    resolve_build_manifest,
    slot_lock_path,
    storage_snapshot,
)
from .build_logs import BuildLogs
from .build_process import BuildProcess
from .build_security import BuildSecurityConfigError, interpret_idf601_security

ALLOWED_ENV = frozenset(
    {
        "IDF_PATH",
        "IDF_PYTHON_ENV_PATH",
        "IDF_TOOLS_PATH",
        "IDF_TOOLS_EXPORT_CMD",
        "IDF_TOOLS_INSTALL_CMD",
        "IDF_TARGET",
        "ESP_IDF_VERSION",
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "WINDIR",
        "SYSTEMDRIVE",
        "TEMP",
        "TMP",
        "COMSPEC",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "HOME",
        "PROCESSOR_ARCHITECTURE",
        "PROCESSOR_ARCHITEW6432",
    }
)
OS_ENV = ALLOWED_ENV - {
    "IDF_PATH",
    "IDF_PYTHON_ENV_PATH",
    "IDF_TOOLS_PATH",
    "IDF_TOOLS_EXPORT_CMD",
    "IDF_TOOLS_INSTALL_CMD",
    "IDF_TARGET",
    "ESP_IDF_VERSION",
    "PATH",
}


class BuildPreflightFailure(ValueError):
    """Base for fixed preflight diagnostics, without native output."""

    code: str


class FixedDiagnosticFailure(BuildPreflightFailure):
    """Allowlisted public diagnostic; never accepts native exception text."""

    def __init__(self, code: str) -> None:
        if code not in {
            "idf_version_diagnostic_failed",
            "idf_version_mismatch_no_fallback",
            "external_python_diagnostic_failed",
            "external_toolchain_missing",
            "registered_tool_diagnostic_failed",
            "registered_tool_version_mismatch",
            "registered_tool_resolution_ambiguous",
            "external_toolchain_resolution_changed",
            "node_requires_explicit_local_registration",
            "build_inputs_changed",
        }:
            raise ValueError("invalid_build_diagnostic_code")
        self.code = code
        super().__init__(code)


class ProfileExportFailure(BuildPreflightFailure):
    """Fixed public diagnostic; no PowerShell stderr or profile path is reflected."""

    code = "profile_export_rejected_or_failed"

    def __init__(self) -> None:
        super().__init__(self.code)


class ToolchainDiagnosticFailure(BuildPreflightFailure):
    code = "external_toolchain_diagnostic_failed"

    def __init__(self) -> None:
        super().__init__(self.code)


class BuildFlashLayoutFailure(BuildPreflightFailure):
    code = "build_flash_layout_invalid"

    def __init__(self) -> None:
        super().__init__(self.code)


def _checked_build_artifact(root: Path, name: str) -> Path:
    if (
        not isinstance(name, str)
        or not name
        or "\\" in name
        or ":" in name
        or "\0" in name
        or any(part in ("", ".", "..") for part in name.split("/"))
    ):
        raise BuildFlashLayoutFailure()
    relative = PurePosixPath(name)
    if relative.is_absolute():
        raise BuildFlashLayoutFailure()
    path = root
    for part in (None, *relative.parts):
        if part is not None:
            path = path / part
        try:
            info = path.lstat()
        except OSError:
            raise BuildFlashLayoutFailure() from None
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise BuildFlashLayoutFailure()
    if not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
        raise BuildFlashLayoutFailure()
    return path


def _unique_layout_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise BuildFlashLayoutFailure()
        result[key] = value
    return result


def build_flash_layout_receipt(
    root: Path, target: str, artifacts: list[dict[str, Any]]
) -> dict[str, Any]:
    """Attest generated offsets' source; does not grant flash/device permission."""
    path = _checked_build_artifact(root, "flasher_args.json")
    size = path.stat().st_size
    if not 0 < size <= 65536:
        raise BuildFlashLayoutFailure()
    with path.open("rb") as handle:
        raw = handle.read(65537)
    if len(raw) != size:
        raise BuildFlashLayoutFailure()
    try:
        layout = json.loads(raw, object_pairs_hook=_unique_layout_pairs)
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise BuildFlashLayoutFailure() from None
    if not isinstance(layout, dict):
        raise BuildFlashLayoutFailure()
    extra = layout.get("extra_esptool_args")
    files = layout.get("flash_files")
    if (
        not isinstance(extra, dict)
        or extra.get("chip") != target
        or not isinstance(files, dict)
        or not 1 <= len(files) <= 16
    ):
        raise BuildFlashLayoutFailure()
    indexed: dict[str, dict[str, Any]] = {}
    for artifact in artifacts:
        name = artifact.get("name")
        if not isinstance(name, str) or name in indexed:
            raise BuildFlashLayoutFailure()
        indexed[name] = artifact
    offsets: set[int] = set()
    for offset, name in files.items():
        if (
            not isinstance(offset, str)
            or re.fullmatch(r"0x[0-9a-fA-F]{1,8}", offset) is None
            or int(offset, 16) in offsets
            or not isinstance(name, str)
            or not name.endswith(".bin")
            or name not in indexed
        ):
            raise BuildFlashLayoutFailure()
        offsets.add(int(offset, 16))
        artifact_path = _checked_build_artifact(root, name)
        receipt = indexed[name]
        if (
            type(receipt.get("bytes")) is not int
            or not 0 < receipt["bytes"] <= 32 * 1024 * 1024
            or artifact_path.stat().st_size != receipt["bytes"]
            or hash_file(artifact_path) != receipt.get("sha256")
        ):
            raise BuildFlashLayoutFailure()
    # Bind the exact generated bytes, not a parsed/reserialized approximation.
    return dict(name="flasher_args.json", bytes=size, sha256=hashlib.sha256(raw).hexdigest())


class BuildFlashSecurityFailure(BuildPreflightFailure):
    code = "build_flash_security_invalid"

    def __init__(self) -> None:
        super().__init__(self.code)


def build_flash_security_receipt(root: Path, target: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Attest generated config, exposing only fixed boolean security categories."""
    try:
        path = _checked_build_artifact(root, "config/sdkconfig.json")
        size = path.stat().st_size
        if not 0 < size <= 1048576:
            raise BuildFlashSecurityFailure()
        with path.open("rb") as handle:
            raw = handle.read(1048577)
        if len(raw) != size:
            raise BuildFlashSecurityFailure()
        config = json.loads(raw, object_pairs_hook=_unique_layout_pairs)
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError, BuildFlashLayoutFailure):
        raise BuildFlashSecurityFailure() from None
    if not isinstance(config, dict) or config.get("IDF_TARGET") != target:
        raise BuildFlashSecurityFailure()
    try:
        summary = interpret_idf601_security(config)
    except BuildSecurityConfigError:
        raise BuildFlashSecurityFailure() from None
    receipt = dict(name="config/sdkconfig.json", bytes=size, sha256=hashlib.sha256(raw).hexdigest())
    return receipt, summary


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


def source_fingerprint(directory: Path, *, excluded_roots: tuple[Path, ...] = ()) -> str:
    digest = hashlib.sha256()
    count = total = 0
    for path in sorted(directory.rglob("*")):
        relative = path.relative_to(directory)
        # Only repository metadata and explicitly identified broker output roots
        # are excluded. A source component named build-helper is still executable input.
        if relative.parts[0] == ".git" or any(
            path == output or path.is_relative_to(output) for output in excluded_roots
        ):
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
        self.artifacts = BuildArtifacts()
        self.process = BuildProcess()
        self.logs: dict[str, BuildLogs] = {}
        self.manifests: dict[str, tuple[str, dict[str, Any]]] = {}
        self._lock = threading.RLock()
        self._committed_job: str | None = None
        self._build_lock: Any = None
        self._pending: dict[str, tuple[str, Path, dict[str, Any]]] = {}
        self.output_uncertain = False
        self.authority: Callable[[], bool] | None = None
        self._authority_monitor: Callable[[], None] | None = None

    @staticmethod
    def _registered_tools(project: Any) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for name in ("node", "git"):
            tool = getattr(project, name + "_tool", None)
            if tool is None:
                continue
            executable = tool.executable
            if (
                not executable.is_absolute()
                or executable.name.casefold() != name + ".exe"
                or executable.is_symlink()
                or hash_file(executable) != tool.sha256
            ):
                raise ValueError("registered_tool_not_verified")
            if name == "node" and re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", tool.version) is None:
                raise ValueError("registered_node_version_required")
            if name == "git" and not tool.version.startswith("git version "):
                raise ValueError("registered_git_version_required")
            result[name] = tool
        return result

    def _project(
        self, project_id: str | None, principal: str, source_receipt: str | None = None
    ) -> tuple[Any, dict[str, Any] | None]:
        project = self.settings.build_projects.get(project_id)
        if project is None or project_id is None or not principal:
            raise ValueError("registered_build_project_required")
        if source_receipt is None:
            return project, None
        from .source_snapshot import SourceSnapshots

        root, metadata = SourceSnapshots(self.settings).resolve(
            project_id, source_receipt, principal
        )
        provenance = dict(
            source_receipt=source_receipt,
            repository=metadata["repository"],
            commit=metadata["commit"],
            tree_sha1=metadata["tree_sha1"],
            source_sha256=metadata["source_sha256"],
            dependency_policy="vendored_components_no_fetch",
        )
        if metadata["schema_version"] == 2:
            provenance["submodules"] = metadata["submodules"]
            provenance["dependency_policy"] = "gitlinks_fixed_commits_no_fetch"
        return project.model_copy(update={"project_dir": root}), provenance

    def prepare(
        self, project_id: str | None, principal: str, source_receipt: str | None = None
    ) -> dict[str, Any]:
        project, provenance = self._project(project_id, principal, source_receipt)
        if hasattr(self.settings, "state_dir") and project.project_dir.resolve().is_relative_to(
            self.settings.state_dir.resolve()
        ):
            raise ValueError("project_must_not_be_inside_broker_state")
        if not (project.project_dir / "CMakeLists.txt").is_file():
            raise ValueError("idf_project_required")
        registered_tools = self._registered_tools(project)
        if (
            provenance is not None
            and provenance["repository"].casefold() == "dj-oyu/cardputer-adv-pocketjs"
        ):
            node = registered_tools.get("node")
            if node is None or int(node.version.split(".")[0][1:]) < 16:
                raise ValueError("registered_node_16_required_for_source_project")
        build_root = getattr(project, "build_root", None)
        if build_root is not None:
            source = project.project_dir.resolve()
            output = build_root.resolve()
            if output.is_relative_to(source) or source.is_relative_to(output):
                raise BuildLocationError("build_output_root_invalid")
        if hasattr(self.settings, "state_dir"):
            prospective = build_directory(self.settings.state_dir, build_root, "f" * 64, "e" * 64)
            check_windows_path(prospective, windows=os.name == "nt")
            check_storage_before_write(
                self.settings.state_dir,
                build_root,
                getattr(project, "build_min_free_bytes", 512 * 1024 * 1024),
            )
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "source": source_fingerprint(
                        project.project_dir,
                        excluded_roots=(self.settings.state_dir.resolve(),)
                        if hasattr(self.settings, "state_dir")
                        else (),
                    ),
                    "project_id": project_id,
                    **({"source_receipt": source_receipt} if source_receipt is not None else {}),
                    "canonical_project": str(project.project_dir.resolve(strict=True)).casefold(),
                    "profile": hash_file(project.trusted_profile),
                    "version": project.expected_idf,
                    "target": project.target,
                    "profile_policy": getattr(project, "profile_execution_policy", "inherit"),
                    "build_min_free_bytes": getattr(
                        project, "build_min_free_bytes", 512 * 1024 * 1024
                    ),
                    "build_root_id": (
                        hashlib.sha256(str(build_root.resolve()).casefold().encode()).hexdigest()
                        if build_root is not None
                        else None
                    ),
                    "registered_tools": {
                        name: dict(
                            path=str(tool.executable.resolve(strict=True)).casefold(),
                            sha256=tool.sha256,
                            version=tool.version,
                        )
                        for name, tool in registered_tools.items()
                    },
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        return dict(
            project_id=project_id,
            **(
                {"source_receipt": source_receipt, "source_provenance": provenance}
                if provenance
                else {}
            ),
            fingerprint=fingerprint,
            expected_idf=project.expected_idf,
            target=project.target,
            profile_execution_policy=getattr(project, "profile_execution_policy", "inherit"),
            build_principal=principal,
            stages=["idf_profile", "idf_diagnostics", "idf_build"],
            effects=[
                "Executes registered trusted profile and project build scripts",
                "Runs external ESP-IDF 6.0.1; no flash, USB or version fallback",
                "Delivers bounded redacted output to requesting principal",
            ],
            argv_template=[
                "registered-python",
                "registered-idf.py",
                "-B",
                "fingerprint-build-directory",
                *(["-D", "SDKCONFIG=build-output-sdkconfig"] if provenance is not None else []),
                "build",
            ],
        )

    def request_stop(self, job_id: str | None = None) -> bool:
        """Serialize accepted cancellation against the final success commit."""
        with self._lock:
            if job_id is not None and self._committed_job == job_id:
                return False
            self.process.stop_requested.set()
            return True

    def _check_cancelled(self) -> None:
        if self.process.stop_requested.is_set():
            raise ValueError("build_interrupted_needs_review")

    def begin(self) -> None:
        """Only the broker's callback-free dispatch starts a fresh operation."""
        with self._lock:
            self._committed_job = None
            self.process.stop_requested.clear()

    async def execute(
        self, plan: dict[str, Any], stage: Any, *, defer_publish: bool = False
    ) -> str:
        self._check_cancelled()
        loop = asyncio.get_running_loop()

        async def authorized() -> bool:
            return self.authority is not None and self.authority() is True

        def monitor() -> None:
            if asyncio.run_coroutine_threadsafe(authorized(), loop).result(timeout=5) is not True:
                self.process.stop_requested.set()
                raise ValueError("profile_or_principal_no_longer_valid")

        self._authority_monitor = monitor if defer_publish else None
        try:
            return await asyncio.to_thread(
                self._execute,
                plan,
                lambda name: loop.call_soon_threadsafe(stage, name),
                defer_publish,
            )
        finally:
            self._authority_monitor = None

    def _execute(self, plan: dict[str, Any], stage: Any, defer_publish: bool = False) -> str:
        if self._build_lock is not None:
            raise BuildLocationError("build_output_in_use")
        try:
            return self._execute_inner(plan, stage, defer_publish)
        finally:
            if (
                self._build_lock is not None
                and not self._pending
                and not (
                    getattr(self.process, "active", False)
                    or getattr(self.process, "uncertain", False)
                )
            ):
                try:
                    self._release_output_lock()
                except BuildLocationError:
                    with self._lock:
                        self.manifests.pop(plan.get("job_id", ""), None)
                        self._committed_job = None
                    raise

    def _execute_inner(self, plan: dict[str, Any], stage: Any, defer_publish: bool = False) -> str:
        self._check_cancelled()
        if os.name != "nt":
            raise ValueError("external_windows_idf_required")
        principal = plan["build_principal"]
        if (
            self.prepare(plan["project_id"], principal, plan.get("source_receipt"))["fingerprint"]
            != plan["fingerprint"]
        ):
            raise ValueError("build_inputs_changed")
        project, provenance = self._project(
            plan["project_id"], principal, plan.get("source_receipt")
        )
        logs = BuildLogs(principal, job_id=plan["job_id"])
        logs.redactions = tuple(
            value
            for key, value in os.environ.items()
            if re.search(r"(?i)token|secret|password|key|bearer", key) and len(value) >= 8
        )
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
        run = (
            partial(self.process.run, monitor=self._authority_monitor)
            if self._authority_monitor is not None
            else self.process.run
        )
        output: list[str] = []
        output_bytes = 0
        stderr_seen = False

        def profile_output(line: str, stream: str) -> None:
            nonlocal output_bytes, stderr_seen
            output_bytes += len(line.encode("utf-8")) + 1
            if output_bytes > 131072:
                self.process.stop_requested.set()
                return
            if stream != "stdout":
                stderr_seen = True
                return
            output.append(line)

        profile_args = [str(powershell), "-NoProfile"]
        if getattr(project, "profile_execution_policy", "inherit") == "RemoteSigned":
            profile_args.extend(["-ExecutionPolicy", "RemoteSigned"])
        profile_args.extend(["-File", str(project.trusted_profile), "-e"])
        try:
            code = run(profile_args, env, project.project_dir, profile_output, timeout=30)
        except ValueError:
            if stderr_seen:
                logs.append("error: trusted profile export rejected or failed")
                raise ProfileExportFailure() from None
            raise
        self._check_cancelled()
        if code or stderr_seen:
            logs.append("error: trusted profile export rejected or failed")
            raise ProfileExportFailure()
        env.update(parse_profile("\n".join(output)))
        output.clear()
        idf = Path(env["IDF_PATH"])
        python = Path(env["IDF_PYTHON_ENV_PATH"]) / "Scripts/python.exe"
        idf_py = idf / "tools/idf.py"
        if (
            not idf.is_absolute()
            or not python.is_absolute()
            or not python.is_file()
            or not idf_py.is_file()
            or python.is_symlink()
            or idf_py.is_symlink()
        ):
            raise ValueError("external_idf_environment_invalid")
        env["IDF_TARGET"] = project.target
        if provenance is not None:
            env["IDF_COMPONENT_MANAGER"] = "0"
        logs.redactions += tuple(
            str(path)
            for path in (project.project_dir, project.trusted_profile, idf, python.parent.parent)
        )
        stage("idf_diagnostics")
        version: list[str] = []

        def version_output(line: str, stream: str) -> None:
            logs.append(line, stream)
            if len(version) >= 32:
                self.process.stop_requested.set()
            else:
                version.append(line[:4096])

        if run(
            [str(python), str(idf_py), "--version"],
            env,
            project.project_dir,
            version_output,
            timeout=30,
        ):
            raise FixedDiagnosticFailure("idf_version_diagnostic_failed")
        if not any(re.fullmatch(r"ESP-IDF v6\.0\.1", line.strip()) for line in version):
            raise FixedDiagnosticFailure("idf_version_mismatch_no_fallback")
        version.clear()
        if run([str(python), "--version"], env, project.project_dir, version_output, timeout=30):
            raise FixedDiagnosticFailure("external_python_diagnostic_failed")
        compiler = shutil.which("xtensa-esp32s3-elf-gcc.exe", path=env["PATH"])
        if compiler is None:
            raise FixedDiagnosticFailure("external_toolchain_missing")
        version.clear()
        if run([compiler, "--version"], env, project.project_dir, version_output, timeout=30):
            logs.append("error: external toolchain diagnostic failed; build not started")
            raise ToolchainDiagnosticFailure()
        registered_versions: dict[str, dict[str, str]] = {}
        registered_tools = self._registered_tools(project)
        search_directories: list[str] = []
        for name, tool in registered_tools.items():
            version.clear()
            if run(
                [str(tool.executable), "--version"],
                env,
                project.project_dir,
                version_output,
                timeout=30,
            ):
                raise FixedDiagnosticFailure("registered_tool_diagnostic_failed")
            self._check_cancelled()
            if version != [tool.version]:
                raise FixedDiagnosticFailure("registered_tool_version_mismatch")
            search_directories.append(str(tool.executable.parent))
            registered_versions[name] = dict(version=tool.version, sha256=tool.sha256)
            logs.append(f"-- Registered {name} version: {tool.version} (SHA-256 verified)")
        if search_directories:
            # Never merge SYSTEM_PATH or ambient PATH. Only hash-verified local
            # registrations supplement the trusted IDF export environment.
            env["PATH"] = ";".join([*search_directories, env["PATH"]])
        for name, tool in registered_tools.items():
            resolved = shutil.which(name + ".exe", path=env["PATH"])
            if resolved is None or Path(resolved).resolve() != tool.executable.resolve():
                raise FixedDiagnosticFailure("registered_tool_resolution_ambiguous")
        resolved_compiler = shutil.which("xtensa-esp32s3-elf-gcc.exe", path=env["PATH"])
        if (
            resolved_compiler is None
            or Path(resolved_compiler).resolve() != Path(compiler).resolve()
        ):
            raise FixedDiagnosticFailure("external_toolchain_resolution_changed")
        if "node" not in registered_tools and shutil.which("node.exe", path=env["PATH"]):
            raise FixedDiagnosticFailure("node_requires_explicit_local_registration")
        environment_fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "idf_py": hash_file(idf_py),
                    "python": hash_file(python),
                    "compiler": hash_file(Path(compiler)),
                    "environment": env,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        logs.append("-- External Python, toolchain and ESP-IDF 6.0.1 diagnostics passed")
        from filelock import FileLock, Timeout

        build_root = getattr(project, "build_root", None)
        check_storage_before_write(
            self.settings.state_dir,
            build_root,
            getattr(project, "build_min_free_bytes", 512 * 1024 * 1024),
        )
        lock_path = slot_lock_path(
            self.settings.state_dir, build_root, plan["fingerprint"], environment_fingerprint
        )
        lock_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        # The worker acquires; deferred success/discard releases on the broker loop.
        lock = FileLock(str(lock_path), timeout=0, thread_local=False)
        try:
            lock.acquire()
        except Timeout:
            raise BuildLocationError("build_output_in_use") from None
        self._build_lock = lock
        build_dir = claim_build_directory(
            self.settings.state_dir, build_root, plan["fingerprint"], environment_fingerprint
        )
        stage("idf_build")

        def emit(line: str, stream: str) -> None:
            with self._lock:
                logs.append(line, stream)

        # Diagnostics can take time. Recheck live project/profile/lock inputs
        # immediately before launching build; a changed tree needs a new plan.
        self._check_cancelled()
        if (
            self.prepare(plan["project_id"], principal, plan.get("source_receipt"))["fingerprint"]
            != plan["fingerprint"]
        ):
            logs.append("error: build inputs changed before launch; prepare a new plan")
            raise ValueError("build_inputs_changed")
        self._check_cancelled()
        build_args = [str(python), str(idf_py), "-B", str(build_dir)]
        if provenance is not None:
            build_args += ["-D", "SDKCONFIG=" + str(build_dir / "sdkconfig")]
        build_args.append("build")
        if run(
            build_args,
            env,
            project.project_dir,
            emit,
        ):
            raise ValueError("idf_build_failed_needs_review")
        if (
            self.prepare(plan["project_id"], principal, plan.get("source_receipt"))["fingerprint"]
            != plan["fingerprint"]
        ):
            raise ValueError("build_inputs_changed")
        self._check_cancelled()
        description = json.loads((build_dir / "project_description.json").read_text())
        if description.get("target") != project.target:
            raise ValueError("build_target_mismatch")
        artifacts: list[dict[str, Any]] = []
        for path in sorted(build_dir.rglob("*")):
            self._check_cancelled()
            if path.suffix in (".bin", ".elf") and path.is_file():
                if len(artifacts) >= 32:
                    raise ValueError("artifact_count_limit")
                _checked_build_artifact(build_dir, path.relative_to(build_dir).as_posix())
                artifacts.append(
                    dict(
                        name=path.relative_to(build_dir).as_posix(),
                        bytes=path.stat().st_size,
                        sha256=hash_file(path),
                    )
                )
        self._check_cancelled()
        if not artifacts:
            raise ValueError("build_artifacts_missing")
        flash_layout = build_flash_layout_receipt(build_dir, project.target, artifacts)
        self._check_cancelled()
        flash_security, security_summary = build_flash_security_receipt(build_dir, project.target)
        self._check_cancelled()
        manifest = dict(
            schema_version=1,
            **({"source_provenance": provenance} if provenance is not None else {}),
            build_job_id=plan["job_id"],
            build_plan_hash=hashlib.sha256(
                json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            idf_version="6.0.1",
            target=project.target,
            fingerprint=plan["fingerprint"],
            environment_fingerprint=environment_fingerprint,
            artifacts=artifacts,
            registered_tool_versions=registered_versions,
            flash_layout=flash_layout,
            flash_security=flash_security,
            flash_security_summary=security_summary,
            build_location=build_location(plan["fingerprint"], environment_fingerprint, build_root),
            flash_authorized=False,
        )
        # Write an inert candidate first. Accepted cancellation must never publish
        # a success manifest, including cancellation during hashing or serialization.
        candidate = build_dir / "broker-manifest.pending.json"
        if defer_publish:
            # Register cleanup ownership before the candidate write can fail or
            # cancellation can win; the broker loop confirms cleanup and unlock.
            with self._lock:
                self._pending[plan["job_id"]] = (principal, build_dir, manifest)
        candidate.write_text(json.dumps(manifest, indent=2))
        if defer_publish:
            self._check_cancelled()
            return "firmware_build_succeeded_no_flash"
        try:
            with self._lock:
                self._check_cancelled()
                candidate.replace(build_dir / "broker-manifest.json")
                self.artifacts.publish(plan["job_id"], principal, build_dir, manifest)
                self.manifests[plan["job_id"]] = (principal, manifest)
                self._committed_job = plan["job_id"]
                logs.append(
                    "Build complete; artifact hashes recorded; flash requires separate approval"
                )
        finally:
            candidate.unlink(missing_ok=True)
        return "firmware_build_succeeded_no_flash"

    def commit_deferred(self, plan: dict[str, Any], live: Callable[[], bool]) -> None:
        """Called on the broker loop after worker exit, with no await before job commit."""
        self._check_cancelled()
        principal, root, manifest = self._pending[plan["job_id"]]
        if self.process.active or self.process.uncertain or not live():
            raise ValueError("build_final_commit_not_authorized")
        if (
            self.prepare(plan["project_id"], principal, plan.get("source_receipt"))["fingerprint"]
            != plan["fingerprint"]
        ):
            raise FixedDiagnosticFailure("build_inputs_changed")
        if not live():
            raise ValueError("build_final_commit_not_authorized")
        self._check_cancelled()
        (root / "broker-manifest.pending.json").replace(root / "broker-manifest.json")
        self._release_output_lock()
        self.artifacts.publish(plan["job_id"], principal, root, manifest)
        self.manifests[plan["job_id"]] = (principal, manifest)
        self._pending.pop(plan["job_id"])
        self._committed_job = plan["job_id"]
        self.logs[plan["job_id"]].append(
            "Build complete; artifact hashes recorded; flash requires separate approval"
        )

    def discard_deferred(self, job_id: str) -> None:
        """Discard only this job's unpublished candidate; never delete source/artifact data."""
        self.manifests.pop(job_id, None)
        self.artifacts.discard(job_id)
        pending = self._pending.pop(job_id, None)
        if pending is not None:
            try:
                (pending[1] / "broker-manifest.pending.json").unlink(missing_ok=True)
            except Exception:
                self.output_uncertain = True
        if (
            self._build_lock is not None
            and not self.process.active
            and not self.process.uncertain
            and not self.output_uncertain
        ):
            try:
                self._release_output_lock()
            except BuildLocationError:
                pass

    def _release_output_lock(self) -> None:
        if self._build_lock is None:
            return
        try:
            self._build_lock.release()
            if self._build_lock.is_locked:
                raise ValueError("build_output_release_unconfirmed")
        except Exception:
            self.output_uncertain = True
            raise BuildLocationError("build_output_release_unconfirmed") from None
        self._build_lock = None

    def storage(self, project_id: str) -> dict[str, Any]:
        project = self.settings.build_projects.get(project_id)
        if project is None:
            raise BuildLocationError("registered_build_project_required")
        return storage_snapshot(
            self.settings.state_dir,
            getattr(project, "build_root", None),
            min_free_bytes=getattr(project, "build_min_free_bytes", 512 * 1024 * 1024),
        )

    def resolve_flash_manifest(
        self, job_id: str, principal: str, project_id: str
    ) -> tuple[Path, dict[str, Any]]:
        cached_root, manifest = self.artifacts.resolve(job_id, principal)
        project = self.settings.build_projects.get(project_id)
        if project is None:
            raise BuildLocationError("registered_build_project_required")
        root = resolve_build_manifest(self.settings.state_dir, project.build_root, manifest)
        if root.resolve() != cached_root:
            raise BuildLocationError("build_output_identity_mismatch")
        return root, manifest

    def read_logs(self, request: Any, principal: str) -> dict[str, Any]:
        with self._lock:
            logs = self.logs.get(request.job_id)
            if logs is None:
                raise ValueError("build_log_not_available")
            result = logs.read(principal, request)
            if result["expired"]:
                self.manifests.pop(request.job_id, None)
            manifest = self.manifests.get(request.job_id)
            if manifest and manifest[0] == principal and not result["expired"]:
                result["manifest"] = manifest[1]
            return result
