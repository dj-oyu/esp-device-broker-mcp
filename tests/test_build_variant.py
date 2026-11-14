"""Source-bound build variant; synthetic source trees and a fake IDF build, never hardware."""

import hashlib
import json
import os
import re
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import test_flash_policy as flash_policy
import test_source_snapshot as fixture
from test_registry_http import registry_service as registry_service

from cardputer_mcp import build_variant
from cardputer_mcp import firmware_build as module
from cardputer_mcp.build_locations import BuildLocationError, build_location, claim_build_directory
from cardputer_mcp.config import BuildProject, SourceRepository
from cardputer_mcp.firmware_build import FirmwareBuild, FixedDiagnosticFailure
from cardputer_mcp.flash_binding import prepare_binding, validate_binding
from cardputer_mcp.models import PrepareRequest, TestName
from cardputer_mcp.public_reasons import PUBLIC_REASON_CODES, public_reason
from cardputer_mcp.source_snapshot import SourceSnapshots, file_receipt, tree_hash
from cardputer_mcp.store import Store

PROBE = b"CONFIG_PROBE=y\n# CONFIG_QUIET is not set\nCONFIG_LEVEL=3\n"
ALT = b"CONFIG_PROBE=n\nCONFIG_QUIET=y\n"
env = flash_policy.env


# --------------------------------------------------------------------------- grammar


@pytest.mark.parametrize(
    "raw,symbols",
    [
        (b"CONFIG_A=y\n", {"CONFIG_A": "y"}),
        (b"CONFIG_A=n\n", {"CONFIG_A": "n"}),
        (b"# CONFIG_A is not set\n", {"CONFIG_A": "n"}),
        (
            b"CONFIG_A=-12\nCONFIG_B=0\nCONFIG_C=0x1f\n",
            {"CONFIG_A": "-12", "CONFIG_B": "0", "CONFIG_C": "0x1f"},
        ),
        (b"# why this exists\n\nCONFIG_A=y\n", {"CONFIG_A": "y"}),
        (
            b"".join(b"CONFIG_S%d=y\n" % i for i in range(16)),
            {f"CONFIG_S{i}": "y" for i in range(16)},
        ),
    ],
)
def test_fragment_grammar_accepts_only_bool_and_integer_declarations(raw, symbols):
    assert build_variant.parse(raw) == symbols


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"\n",
        b"# only a comment\n",
        b"CONFIG_A=y",  # no final newline
        b"CONFIG_A=y\r\n",  # CR is not part of the grammar
        b'CONFIG_A="text"\n',  # strings can carry paths
        b"CONFIG_A=1.5\n",
        b"CONFIG_A=0X1F\n",
        b"CONFIG_A=12345678901\n",
        b"CONFIG_A= y\n",
        b"CONFIG_A=y # trailing\n",
        b"config_a=y\n",
        b"CONFIG_A=y\nCONFIG_A=n\n",  # duplicate
        b"CONFIG_A=y\n# CONFIG_A is not set\n",  # duplicate across forms
        b"# CONFIG_A=y\n",  # commented-out declaration
        b"# CONFIG_A is not set extra\n",
        b"include other.defaults\n",
        b"-include other\n",
        b"CONFIG_A=y\nsource /x\n",
        "CONFIG_A=y\n# \u00e9\n".encode(),
        b"CONFIG_A=y\x00\n",
        b"".join(b"CONFIG_S%d=y\n" % i for i in range(17)),  # 17 symbols
        b"\n" * 129 + b"CONFIG_A=y\n",  # line bound
        b"# " + b"x" * 5000 + b"\nCONFIG_A=y\n",  # byte bound
        b"CONFIG_SECURE_BOOT=y\n",
        b"CONFIG_BOOTLOADER_LOG_LEVEL=1\n",
        b"CONFIG_PARTITION_TABLE_CUSTOM=y\n",
        b"CONFIG_ESPTOOLPY_FLASHSIZE_16MB=y\n",
        b"CONFIG_EFUSE_VIRTUAL=y\n",
        b"CONFIG_IDF_TARGET=1\n",
        b"CONFIG_IDF_TARGET_ESP32S3=y\n",
        b"# CONFIG_SECURE_BOOT is not set\n",
        b"CONFIG_FLASH_ENCRYPTION_ENABLED=y\n",
        b"CONFIG_APP_ROLLBACK_ENABLE=y\n",
    ],
)
def test_fragment_grammar_rejects_everything_else_with_one_fixed_code(raw):
    with pytest.raises(ValueError) as caught:
        build_variant.parse(raw)
    assert caught.value.args == ("build_variant_invalid",)


@pytest.mark.parametrize(
    "name,ok",
    [("probe", True), ("A-b_9", True), ("x" * 24, True), ("x" * 25, False), ("", False)]
    + [(bad, False) for bad in ("a.b", "a/b", "a\\b", "a b", "../x", "a:b", "é", "a\n", None, 3)],
)
def test_variant_names_are_short_identifiers(name, ok):
    assert build_variant.name_ok(name) is ok


def _put(path, text):
    path.write_bytes(text.encode())


def _source(tmp_path, files):
    root = tmp_path / "source"
    root.mkdir()
    for name, data in files.items():
        (root / name).write_bytes(data)
    return root


def test_load_reads_exact_file_and_binds_its_hash(tmp_path):
    root = _source(tmp_path, {"sdkconfig.variant.probe": PROBE, "CMakeLists.txt": b"x"})
    symbols, digest = build_variant.load(root, "probe")
    assert symbols == {"CONFIG_PROBE": "y", "CONFIG_QUIET": "n", "CONFIG_LEVEL": "3"}
    assert digest == hashlib.sha256(PROBE).hexdigest()
    assert build_variant.bind(root, "probe") == {
        "build_variant": "probe",
        "build_variant_sha256": digest,
    }


@pytest.mark.parametrize(
    "setup,code",
    [
        ("missing", "build_variant_not_found"),
        ("wrong_case", "build_variant_not_found"),
        ("directory", "build_variant_invalid"),
        ("oversize", "build_variant_invalid"),
        ("empty", "build_variant_invalid"),
        ("bad_content", "build_variant_invalid"),
        ("hardlink", "build_variant_invalid"),
        ("symlink", "build_variant_invalid"),
    ],
)
def test_load_refuses_unsafe_or_absent_fragments(tmp_path, setup, code):
    root = _source(tmp_path, {"unrelated": b"x"})
    target = root / "sdkconfig.variant.probe"
    if setup == "wrong_case":
        (root / "sdkconfig.variant.PROBE").write_bytes(PROBE)
    elif setup == "directory":
        target.mkdir()
    elif setup == "oversize":
        target.write_bytes(b"# " + b"x" * 5000 + b"\nCONFIG_A=y\n")
    elif setup == "empty":
        target.write_bytes(b"")
    elif setup == "bad_content":
        target.write_bytes(b'CONFIG_A="x"\n')
    elif setup == "hardlink":
        (root / "other").write_bytes(PROBE)
        try:
            os.link(root / "other", target)
        except OSError:
            pytest.skip("hard links unavailable")
    elif setup == "symlink":
        (root / "real").write_bytes(PROBE)
        try:
            target.symlink_to(root / "real")
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable")
    with pytest.raises(ValueError) as caught:
        build_variant.load(root, "probe")
    assert caught.value.args == (code,)
    assert "probe" not in str(caught.value) and str(tmp_path) not in str(caught.value)


def test_load_refuses_names_outside_the_pattern_without_touching_the_filesystem(tmp_path):
    root = _source(tmp_path, {"secret": PROBE})
    for name in ("../secret", "a/b", "", "x" * 25):
        with pytest.raises(ValueError) as caught:
            build_variant.load(root, name)
        assert caught.value.args == ("build_variant_invalid",)


# ----------------------------------------------------------------- seeding and checks

SYMBOLS = {"CONFIG_PROBE": "y", "CONFIG_QUIET": "n", "CONFIG_LEVEL": "3"}


def test_seed_creates_exclusively_and_accepts_only_a_satisfying_existing_config(tmp_path):
    build_variant.seed(tmp_path, SYMBOLS)
    seeded = (tmp_path / "sdkconfig").read_text()
    assert seeded == "CONFIG_LEVEL=3\nCONFIG_PROBE=y\n# CONFIG_QUIET is not set\n"
    build_variant.seed(tmp_path, SYMBOLS)  # same content passes and is left alone
    assert (tmp_path / "sdkconfig").read_text() == seeded
    _put(tmp_path / "sdkconfig", seeded + "CONFIG_EXTRA=1\n")  # IDF-added lines are fine
    build_variant.seed(tmp_path, SYMBOLS)
    for conflicting in (
        "CONFIG_PROBE=n\nCONFIG_LEVEL=3\n# CONFIG_QUIET is not set\n",
        "CONFIG_LEVEL=3\n",
        "CONFIG_PROBE=y\nCONFIG_PROBE=y\nCONFIG_LEVEL=3\n# CONFIG_QUIET is not set\n",
    ):
        _put(tmp_path / "sdkconfig", conflicting)
        with pytest.raises(ValueError) as caught:
            build_variant.seed(tmp_path, SYMBOLS)
        assert caught.value.args == ("build_variant_config_conflict",)
        assert (tmp_path / "sdkconfig").read_text() == conflicting  # never rewritten


def test_seed_refuses_a_linked_or_directory_config(tmp_path):
    (tmp_path / "sdkconfig").mkdir()
    with pytest.raises(ValueError) as caught:
        build_variant.seed(tmp_path, SYMBOLS)
    assert caught.value.args == ("build_variant_config_conflict",)


@pytest.mark.parametrize(
    "text,ok",
    [
        ("CONFIG_PROBE=y\n# CONFIG_QUIET is not set\nCONFIG_LEVEL=3\nCONFIG_X=1\n", True),
        ("CONFIG_PROBE=y\nCONFIG_LEVEL=3\n", False),  # OFF symbol absent entirely
        ("CONFIG_PROBE=y\nCONFIG_QUIET=y\nCONFIG_LEVEL=3\n", False),  # OFF symbol turned on
        ("CONFIG_PROBE=y\n# CONFIG_QUIET is not set\n", False),  # int symbol absent
        ("CONFIG_PROBE=y\n# CONFIG_QUIET is not set\nCONFIG_LEVEL=4\n", False),  # other value
        ("# CONFIG_PROBE is not set\n# CONFIG_QUIET is not set\nCONFIG_LEVEL=3\n", False),
        (
            "CONFIG_PROBE=y\nCONFIG_PROBE=y\n# CONFIG_QUIET is not set\nCONFIG_LEVEL=3\n",
            False,
        ),  # two declarations
        (
            "CONFIG_PROBE=y\n# CONFIG_QUIET is not set\nCONFIG_QUIET=y\nCONFIG_LEVEL=3\n",
            False,
        ),  # both forms of one symbol
        ("CONFIG_PROBE_EXTRA=y\n# CONFIG_QUIET is not set\nCONFIG_LEVEL=3\n", False),
    ],
)
def test_verify_applied_reads_the_generated_config(tmp_path, text, ok):
    _put(tmp_path / "sdkconfig", text)
    if ok:
        receipt = build_variant.verify_applied(tmp_path, SYMBOLS)
        assert receipt == {
            "name": "sdkconfig",
            "bytes": len(text),
            "sha256": hashlib.sha256(text.encode()).hexdigest(),
        }
        build_variant.verify_receipt(tmp_path, receipt)
        _put(tmp_path / "sdkconfig", text + "CONFIG_TAMPER=1\n")
        with pytest.raises(ValueError) as caught:
            build_variant.verify_receipt(tmp_path, receipt)
        assert caught.value.args == ("build_variant_not_applied",)
    else:
        with pytest.raises(ValueError) as caught:
            build_variant.verify_applied(tmp_path, SYMBOLS)
        assert caught.value.args == ("build_variant_not_applied",)


def test_verify_applied_refuses_a_missing_or_oversize_config(tmp_path):
    with pytest.raises(ValueError):
        build_variant.verify_applied(tmp_path, SYMBOLS)
    (tmp_path / "sdkconfig").write_bytes(b"#" * (build_variant.MAX_CONFIG_BYTES + 1))
    with pytest.raises(ValueError):
        build_variant.verify_applied(tmp_path, SYMBOLS)
    with pytest.raises(ValueError):
        build_variant.verify_receipt(tmp_path, None)


def test_plan_identity_is_all_or_nothing():
    assert build_variant.identity({}) is None
    assert build_variant.plan_kwargs({}) == {}
    plan = {"build_variant": "probe", "build_variant_sha256": "a" * 64}
    assert build_variant.identity(plan) == ("probe", "a" * 64)
    assert build_variant.plan_kwargs(plan) == {"variant": "probe"}
    for broken in (
        {"build_variant": "probe"},
        {"build_variant_sha256": "a" * 64},
        {"build_variant": "bad name", "build_variant_sha256": "a" * 64},
        {"build_variant": "probe", "build_variant_sha256": "A" * 64},
        {"build_variant": "probe", "build_variant_sha256": "a" * 63},
    ):
        with pytest.raises(ValueError):
            build_variant.identity(broken)


# ------------------------------------------------------- request shape and vocabulary


def test_request_model_binds_variant_to_a_source_build():
    ok = PrepareRequest(
        test=TestName.BUILD, job_id="b", project_id="p", source_receipt="a" * 64, variant="probe"
    )
    assert ok.variant == "probe"
    assert "variant" not in PrepareRequest(
        test=TestName.BUILD, job_id="b", project_id="p"
    ).model_dump(exclude_none=True)
    for fields in (
        {"test": TestName.BUILD, "project_id": "p"},  # no source_receipt
        {"test": TestName.BUILD, "project_id": "p", "source_receipt": "a" * 64, "variant": None},
        {"test": TestName.SOURCE, "project_id": "p", "source_commit": "a" * 40},
        {"test": TestName.HELLO, "session": "a" * 64},
    ):
        with pytest.raises(ValueError) as caught:
            PrepareRequest(job_id="b", variant=fields.pop("variant", "probe"), **fields)
        reasons = {
            public_reason(error["ctx"]["error"])
            for error in caught.value.errors()
            if "ctx" in error
        }
        assert "build_variant_requires_source_build" in reasons
    for bad in ("a.b", "a b", "x" * 25, "", "é"):
        with pytest.raises(ValueError):
            PrepareRequest(
                test=TestName.BUILD,
                job_id="b",
                project_id="p",
                source_receipt="a" * 64,
                variant=bad,
            )


def test_new_reason_codes_are_in_the_fixed_public_vocabulary():
    codes = {
        "build_variant_requires_source_build",
        "build_variant_not_found",
        "build_variant_invalid",
        "build_variant_config_conflict",
        "build_variant_not_applied",
    }
    assert codes <= PUBLIC_REASON_CODES
    for code in codes:
        assert public_reason(ValueError(code)) == code
    assert public_reason(ValueError("build_variant_invalid: /private/probe")) == "invalid_request"
    for code in ("build_variant_config_conflict", "build_variant_not_applied"):
        assert FixedDiagnosticFailure(code).code == code


def test_submit_surface_accepts_variant_for_builds_only():
    from cardputer_mcp.surface import SUBMIT_RULES, Kind, SubmitRequest

    assert "variant" in SUBMIT_RULES[Kind.BUILD][1]
    for kind, (required, optional) in SUBMIT_RULES.items():
        if kind is not Kind.BUILD:
            assert "variant" not in required | optional
    request = SubmitRequest(
        kind="build", job_id="b", project_id="p", source_receipt="a" * 64, variant="probe"
    )
    assert request.variant == "probe"
    with pytest.raises(ValueError):
        SubmitRequest(kind="build", job_id="b", project_id="p", variant="not a name")


def test_tools_list_grows_by_one_short_argument_only():
    snapshot = Path(__file__).parent / "fixtures" / "surface_tools_list.json"
    tools = json.loads(snapshot.read_text(encoding="utf-8"))
    assert len(tools) == 6
    compact = len(json.dumps(tools, separators=(",", ":")))
    # main was 16030 characters; the only addition is submit.variant.
    assert compact - 16030 <= 190
    submit = next(tool for tool in tools if tool["name"] == "submit")
    schema = submit["inputSchema"]
    request = schema["$defs"][schema["properties"]["request"]["$ref"].rsplit("/", 1)[1]]
    assert request["properties"]["variant"]["description"].startswith("build;")
    assert "variant" not in schema["$defs"]["Kind"]["enum"]
    assert len(schema["$defs"]["Kind"]["enum"]) == len(set(schema["$defs"]["Kind"]["enum"]))


def test_registration_digest_inputs_are_unchanged():
    """No allow-flag or other registration field was added for variants."""
    assert list(BuildProject.model_fields) == [
        "project_dir",
        "trusted_profile",
        "build_root",
        "build_min_free_bytes",
        "profile_execution_policy",
        "node_tool",
        "git_tool",
        "source_repository",
        "expected_idf",
        "target",
    ]
    if os.name == "nt":
        from cardputer_mcp.registration_digest import project_digest

        base = Path("C:/pinbase1")
        project = BuildProject(
            project_dir=base / "project",
            trusted_profile=base / "profile.ps1",
            build_root=base / "out",
            source_repository=SourceRepository(
                github_repository="owner/repo", cache_root=base / "cache"
            ),
        )
        assert (
            project_digest(project)
            == "57a4cd76e061561c6c2d62faf25e4b1c88d0ffacdbfdde43e2de02d179ea3969"
        )


# ------------------------------------------------------------------- build harness


class Harness:
    """Real source snapshots plus a fake IDF process; mirrors the existing build tests."""

    def __init__(self, tmp_path: Path, *, ccache: bool = False) -> None:
        self.root = tmp_path
        self.ccache = ccache
        project = tmp_path / "project"
        project.mkdir()
        (project / "CMakeLists.txt").write_text("source")
        self.profile = tmp_path / "profile.ps1"
        self.profile.write_text("return")
        self.system = tmp_path / "system"
        powershell = self.system / "System32/WindowsPowerShell/v1.0/powershell.exe"
        powershell.parent.mkdir(parents=True)
        powershell.write_bytes(b"ps")
        self.idf_py = tmp_path / "idf/tools/idf.py"
        self.idf_py.parent.mkdir(parents=True)
        self.idf_py.write_bytes(b"idf")
        self.python_env = tmp_path / "python"
        python = self.python_env / "Scripts/python.exe"
        python.parent.mkdir(parents=True)
        python.write_bytes(b"python")
        self.compiler = tmp_path / "xtensa-esp32s3-elf-gcc.exe"
        self.compiler.write_bytes(b"gcc")
        self.cache = tmp_path / "ccache.exe"
        self.cache.write_bytes(b"cache")
        registered = BuildProject(
            project_dir=project,
            trusted_profile=self.profile,
            expected_idf="6.0.1",
            target="esp32s3",
            build_root=tmp_path / "output",
            build_min_free_bytes=0,
            source_repository=SourceRepository(
                github_repository=fixture.REPOSITORY,
                cache_root=tmp_path / "source-cache",
                private_acl_confirmed=True,
            ),
        )
        self.settings = SimpleNamespace(
            state_dir=tmp_path / "state", build_projects={"cardputer": registered}
        )
        self.builder = FirmwareBuild(self.settings)
        self.files: dict[str, bytes] = {}
        self.commit = fixture.COMMIT
        self.snapshots = SourceSnapshots(self.settings, self._transport)
        self.calls: list[list[str]] = []
        self.environments: list[dict[str, str]] = []
        self.seeded: list[bytes | None] = []
        self.generate = self.default_generate
        self.stats = 0

    def _transport(self, url, *args):
        tree = tree_hash({name: file_receipt(data, "100644") for name, data in self.files.items()})
        if url.startswith("https://api.github.com/"):
            return json.dumps({"sha": self.commit, "tree": {"sha": tree}}).encode()
        return fixture.archive(self.files, commit=self.commit)

    def fetch(self, files: dict[str, bytes], commit: str | None = None) -> str:
        self.files = {"CMakeLists.txt": b"project(synthetic)\n", **files}
        self.commit = commit or fixture.COMMIT
        plan = self.snapshots.prepare("cardputer", self.commit, "alice") | {"job_id": "fetch"}
        return str(self.snapshots.fetch(plan, lambda: True, lambda _: None)["source_receipt"])

    def prepare(self, receipt, variant=None, job="job"):
        return self.builder.prepare("cardputer", "alice", receipt, variant) | {"job_id": job}

    @staticmethod
    def default_generate(directory: Path, seeded: bytes | None) -> None:
        """What IDF does with a seeded sdkconfig: keep it and append generated defaults."""
        (directory / "sdkconfig").write_bytes((seeded or b"") + b'CONFIG_IDF_TARGET="esp32s3"\n')

    def _process(self):
        harness = self

        class FakeProcess:
            stop_requested = threading.Event()
            active = False
            uncertain = False

            def run(self, args, env, cwd, emit, timeout=1800):
                harness.calls.append(args)
                harness.environments.append(dict(env))
                if "-e" in args:
                    for line in (
                        f"IDF_PATH={harness.idf_py.parent.parent}",
                        f"IDF_PYTHON_ENV_PATH={harness.python_env}",
                        f"PATH={harness.root}",
                        *(("IDF_CCACHE_ENABLE=1",) if harness.ccache else ()),
                    ):
                        emit(line, "stdout")
                elif args[-1] == "--print-stats":
                    for name, value in (
                        ("direct_cache_hit", 2),
                        ("preprocessed_cache_hit", 1),
                        ("cache_miss", 3),
                    ):
                        emit(f"{name}\t{value if harness.stats else 0}", "stdout")
                    harness.stats += 1
                elif args[-1] == "--version":
                    if args[0] == str(harness.cache):
                        emit("ccache version 4.12.1", "stdout")
                    else:
                        emit(
                            "ESP-IDF v6.0.1" if args[1] == str(harness.idf_py) else "version",
                            "stdout",
                        )
                else:
                    directory = Path(args[args.index("-B") + 1])
                    sdkconfig = directory / "sdkconfig"
                    harness.seeded.append(sdkconfig.read_bytes() if sdkconfig.exists() else None)
                    directory.mkdir(parents=True, exist_ok=True)
                    (directory / "project_description.json").write_text('{"target":"esp32s3"}')
                    (directory / "firmware.bin").write_bytes(b"firmware")
                    (directory / "config").mkdir(exist_ok=True)
                    (directory / "config/sdkconfig.json").write_text(
                        '{"IDF_TARGET":"esp32s3","SECURE_BOOT":false,'
                        '"SECURE_FLASH_ENC_ENABLED":false,"BOOTLOADER_APP_ANTI_ROLLBACK":false}'
                    )
                    (directory / "flasher_args.json").write_text(
                        '{"extra_esptool_args":{"chip":"esp32s3"},'
                        '"flash_files":{"0x10000":"firmware.bin"}}'
                    )
                    harness.generate(directory, harness.seeded[-1])
                return 0

        return FakeProcess()

    def run(self, plan, *, defer=False):
        self.builder.process = self._process()
        self.calls.clear()
        self.environments.clear()
        self.seeded.clear()
        which = {
            "xtensa-esp32s3-elf-gcc.exe": str(self.compiler),
            **({"ccache.exe": str(self.cache)} if self.ccache else {}),
        }
        with (
            patch.object(
                module,
                "os",
                SimpleNamespace(
                    name="nt",
                    environ={
                        "SYSTEMROOT": str(self.system),
                        "PROCESSOR_ARCHITECTURE": "AMD64",
                        "PROCESSOR_ARCHITEW6432": "AMD64",
                    },
                ),
            ),
            patch.object(
                module.shutil, "which", side_effect=lambda name, path=None: which.get(name)
            ),
        ):
            return self.builder._execute(plan, lambda _: None, defer)

    def build_env(self):
        return next(
            env
            for args, env in zip(self.calls, self.environments, strict=True)
            if args[-1] == "build"
        )

    def build_args(self):
        return next(args for args in self.calls if args[-1] == "build")


@pytest.fixture
def harness(tmp_path):
    if os.name != "nt":
        pytest.skip("the build runs on the external Windows IDF contract")
    return Harness(tmp_path)


FILES = {
    "sdkconfig.variant.probe": PROBE,
    "sdkconfig.variant.alt": ALT,
    "sdkconfig.variant.bad": b'CONFIG_PATH="x"\n',
    "sdkconfig.variant.secure": b"CONFIG_SECURE_BOOT=y\n",
}
OLD_PLAN_KEYS = {
    "project_id",
    "source_receipt",
    "source_provenance",
    "fingerprint",
    "expected_idf",
    "target",
    "profile_execution_policy",
    "build_principal",
    "stages",
    "effects",
    "argv_template",
}


# ------------------------------------------------------- plan, fingerprint and slot


def test_plan_without_variant_has_the_unchanged_shape_and_fingerprint(harness):
    receipt = harness.fetch(FILES)
    plan = harness.builder.prepare("cardputer", "alice", receipt)
    assert set(plan) == OLD_PLAN_KEYS
    assert harness.builder.prepare("cardputer", "alice", receipt, None) == plan
    project, _ = harness.builder._project("cardputer", "alice", receipt)
    expected = hashlib.sha256(
        json.dumps(
            {
                "source": module.source_fingerprint(
                    project.project_dir, excluded_roots=(harness.settings.state_dir.resolve(),)
                ),
                "project_id": "cardputer",
                "source_receipt": receipt,
                "canonical_project": str(project.project_dir.resolve(strict=True)).casefold(),
                "profile": module.hash_file(project.trusted_profile),
                "version": "6.0.1",
                "target": "esp32s3",
                "compiler_cache_contract": module.CCACHE_CONTRACT,
                "profile_policy": "inherit",
                "build_min_free_bytes": 0,
                "build_root_id": hashlib.sha256(
                    str(project.build_root.resolve()).casefold().encode()
                ).hexdigest(),
                "registered_tools": {},
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    assert plan["fingerprint"] == expected


def test_variant_is_bound_into_plan_fingerprint_and_slot(harness):
    receipt = harness.fetch(FILES)
    plain = harness.builder.prepare("cardputer", "alice", receipt)
    probe = harness.builder.prepare("cardputer", "alice", receipt, "probe")
    alt = harness.builder.prepare("cardputer", "alice", receipt, "alt")
    assert probe["build_variant"] == "probe"
    assert probe["build_variant_sha256"] == hashlib.sha256(PROBE).hexdigest()
    assert set(probe) == OLD_PLAN_KEYS | {"build_variant", "build_variant_sha256"}
    assert {plain["fingerprint"], probe["fingerprint"], alt["fingerprint"]}.__len__() == 3
    assert harness.builder.prepare("cardputer", "alice", receipt, "probe") == probe
    from cardputer_mcp.build_locations import build_slot

    environment = "e" * 64
    assert len({build_slot(item["fingerprint"], environment) for item in (plain, probe, alt)}) == 3
    # The same name over different fragment bytes is a different plan and slot.
    changed = harness.fetch(
        {**FILES, "sdkconfig.variant.probe": PROBE + b"CONFIG_MORE=1\n"}, "b" * 40
    )
    again = harness.builder.prepare("cardputer", "alice", changed, "probe")
    assert again["build_variant_sha256"] != probe["build_variant_sha256"]
    assert again["fingerprint"] != probe["fingerprint"]


@pytest.mark.parametrize(
    "variant,receipt_given,code",
    [
        ("probe", False, "build_variant_requires_source_build"),
        ("missing", True, "build_variant_not_found"),
        ("bad", True, "build_variant_invalid"),
        ("secure", True, "build_variant_invalid"),
        ("not a name", True, "build_variant_invalid"),
    ],
)
def test_prepare_refuses_with_fixed_codes(harness, variant, receipt_given, code):
    receipt = harness.fetch(FILES)
    with pytest.raises(ValueError) as caught:
        harness.builder.prepare("cardputer", "alice", receipt if receipt_given else None, variant)
    assert caught.value.args == (code,)
    assert public_reason(caught.value) == code


# ----------------------------------------------------------------- executing builds


def test_build_without_variant_is_unchanged(harness):
    receipt = harness.fetch(FILES)
    plan = harness.prepare(receipt)
    assert harness.run(plan) == "firmware_build_succeeded_no_flash"
    assert harness.seeded == [None]  # nothing is seeded
    manifest = harness.builder.manifests["job"][1]
    assert not [key for key in manifest if key.startswith("build_variant")]
    assert not [arg for arg in harness.build_args() if arg.startswith("CONFIG_")]


def test_build_seeds_and_verifies_the_generated_config(harness):
    receipt = harness.fetch(FILES)
    plan = harness.prepare(receipt, "probe")
    assert harness.run(plan) == "firmware_build_succeeded_no_flash"
    assert harness.seeded == [b"CONFIG_LEVEL=3\nCONFIG_PROBE=y\n# CONFIG_QUIET is not set\n"]
    manifest = harness.builder.manifests["job"][1]
    assert manifest["build_variant"] == "probe"
    assert manifest["build_variant_sha256"] == plan["build_variant_sha256"]
    config = manifest["build_variant_config"]
    root = Path(harness.build_args()[harness.build_args().index("-B") + 1])
    data = (root / "sdkconfig").read_bytes()
    assert config == {
        "name": "sdkconfig",
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    # No extra build argument: the seed is the only carrier of the selection.
    assert not [arg for arg in harness.build_args() if arg.startswith(("CONFIG_", "SDKCONFIG_D"))]
    assert harness.build_args().count("-D") == 1
    assert (
        manifest["build_plan_hash"]
        == hashlib.sha256(
            json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )


def test_different_variants_never_share_a_slot_or_artifact(harness):
    receipt = harness.fetch(FILES)
    roots = {}
    for name in (None, "probe", "alt"):
        plan = harness.prepare(receipt, name, job=f"job-{name}")
        harness.run(plan)
        args = harness.build_args()
        roots[name] = Path(args[args.index("-B") + 1])
        manifest = harness.builder.manifests[f"job-{name}"][1]
        assert manifest["fingerprint"] == plan["fingerprint"]
        assert manifest["build_location"]["slot"] in str(roots[name])
    assert len(set(roots.values())) == 3
    assert b"CONFIG_PROBE=y" in (roots["probe"] / "sdkconfig").read_bytes()
    assert b"CONFIG_QUIET=y" in (roots["alt"] / "sdkconfig").read_bytes()
    assert (
        not (roots[None] / "sdkconfig").exists()
        or b"CONFIG_PROBE" not in (roots[None] / "sdkconfig").read_bytes()
    )


def test_compiler_cache_namespace_is_per_variant_and_unchanged_without(tmp_path):
    if os.name != "nt":
        pytest.skip("the build runs on the external Windows IDF contract")
    harness = Harness(tmp_path, ccache=True)
    receipt = harness.fetch(FILES)
    seen = {}
    for name in (None, "probe", "alt", "probe"):
        harness.run(harness.prepare(receipt, name, job=f"j{len(seen)}-{name}"))
        seen.setdefault(name, []).append(harness.build_env()["CCACHE_NAMESPACE"])
    assert len({seen[None][0], seen["probe"][0], seen["alt"][0]}) == 3
    assert seen["probe"][0] == seen["probe"][1]
    # The ordinary namespace is exactly what the frozen modes hashed to before variants.
    environment = None
    with patch.object(module, "configure_ccache") as configure:
        configure.side_effect = RuntimeError("stop")
        with pytest.raises(RuntimeError):
            harness.run(harness.prepare(receipt, None, job="spy"))
        environment = configure.call_args.args[-1]
    assert environment == module.CCACHE_NAMESPACE_MODES
    with patch.object(module, "configure_ccache") as configure:
        configure.side_effect = RuntimeError("stop")
        with pytest.raises(RuntimeError):
            harness.run(harness.prepare(receipt, "probe", job="spy2"))
        assert configure.call_args.args[-1] == {
            **module.CCACHE_NAMESPACE_MODES,
            "build_variant": "probe",
            "build_variant_sha256": hashlib.sha256(PROBE).hexdigest(),
        }


@pytest.mark.parametrize(
    "mode,code",
    [
        ("dropped", "build_variant_not_applied"),
        ("flipped", "build_variant_not_applied"),
        ("duplicated", "build_variant_not_applied"),
        ("off_missing", "build_variant_not_applied"),
        ("deleted", "build_variant_not_applied"),
    ],
)
def test_unapplied_generated_config_is_refused_before_a_manifest_exists(harness, mode, code):
    receipt = harness.fetch(FILES)
    plan = harness.prepare(receipt, "probe")

    def generate(directory, seeded):
        text = (seeded or b"").decode()
        if mode == "dropped":
            text = text.replace("CONFIG_PROBE=y\n", "")
        elif mode == "flipped":
            text = text.replace("CONFIG_PROBE=y", "CONFIG_PROBE=n")
        elif mode == "duplicated":
            text += "CONFIG_PROBE=y\n"
        elif mode == "off_missing":
            text = text.replace("# CONFIG_QUIET is not set\n", "")
        if mode == "deleted":
            (directory / "sdkconfig").unlink(missing_ok=True)
        else:
            _put(directory / "sdkconfig", text)

    harness.generate = generate
    with pytest.raises(FixedDiagnosticFailure) as caught:
        harness.run(plan)
    assert caught.value.code == code
    assert not harness.builder.manifests
    assert not list(harness.root.rglob("broker-manifest*.json"))
    assert caught.value.public_diagnostic()["code"] == code


def test_preexisting_conflicting_sdkconfig_in_the_slot_fails_the_build(harness):
    receipt = harness.fetch(FILES)
    plan = harness.prepare(receipt, "probe")
    harness.run(plan)  # create the slot
    root = Path(harness.build_args()[harness.build_args().index("-B") + 1])
    harness.builder.manifests.clear()
    harness.builder.artifacts = type(harness.builder.artifacts)(None)
    _put(root / "sdkconfig", "CONFIG_PROBE=n\n")
    with pytest.raises(FixedDiagnosticFailure) as caught:
        harness.run(plan | {"job_id": "again"})
    assert caught.value.code == "build_variant_config_conflict"
    assert not any(args[-1] == "build" for args in harness.calls)
    assert (root / "sdkconfig").read_text() == "CONFIG_PROBE=n\n"


def test_reusing_the_same_variant_slot_accepts_its_generated_config(harness):
    receipt = harness.fetch(FILES)
    harness.run(harness.prepare(receipt, "probe"))
    harness.builder.manifests.clear()
    harness.run(harness.prepare(receipt, "probe", job="second"))
    assert harness.seeded[-1].startswith(b"CONFIG_LEVEL=3")  # the previous output is kept


def test_fragment_changed_after_prepare_is_a_changed_input(harness):
    receipt = harness.fetch(FILES)
    plan = harness.prepare(receipt, "probe")
    root, _ = harness.snapshots.resolve("cardputer", receipt, "alice")
    (root / "sdkconfig.variant.probe").write_bytes(b"CONFIG_PROBE=n\n")
    with pytest.raises(ValueError) as caught:
        harness.run(plan)
    # The receipt-verified source no longer matches its receipt: refused before any build.
    assert caught.value.args == ("source_snapshot_changed",)
    assert not harness.builder.manifests
    assert not any(args[-1] == "build" for args in harness.calls)
    with pytest.raises(ValueError) as stale:
        build_variant.expected_symbols(root, "probe", plan["build_variant_sha256"])
    assert stale.value.args == ("build_inputs_changed",)


def test_tampered_config_is_caught_before_publication(harness):
    receipt = harness.fetch(FILES)
    plan = harness.prepare(receipt, "probe")
    real = module.build_variant.verify_applied

    def tamper(build_dir, symbols):
        value = real(build_dir, symbols)
        _put(build_dir / "sdkconfig", "CONFIG_PROBE=n\n")
        return value

    with patch.object(module.build_variant, "verify_applied", tamper):
        with pytest.raises(ValueError) as caught:
            harness.run(plan)
    assert caught.value.args == ("build_variant_not_applied",)
    assert "job" not in harness.builder.manifests
    assert not list(harness.root.rglob("broker-manifest.json"))


def test_deferred_commit_rechecks_the_generated_config(harness):
    receipt = harness.fetch(FILES)
    plan = harness.prepare(receipt, "probe")
    assert harness.run(plan, defer=True) == "firmware_build_succeeded_no_flash"
    root = harness.builder._pending["job"][1]
    _put(root / "sdkconfig", "CONFIG_PROBE=n\n")
    with pytest.raises(FixedDiagnosticFailure) as caught:
        harness.builder.commit_deferred(plan, lambda: True)
    assert caught.value.code == "build_variant_not_applied"
    assert "job" not in harness.builder.manifests
    assert not (root / "broker-manifest.json").exists()
    # The untouched candidate commits (new build, same slot content restored).
    (root / "sdkconfig").write_bytes(b"CONFIG_LEVEL=3\nCONFIG_PROBE=y\n# CONFIG_QUIET is not set\n")
    harness.builder._release_output_lock = lambda: None  # lock owned by the worker thread
    pending = harness.builder._pending["job"][2]
    pending["build_variant_config"] = module.build_variant.verify_applied(root, SYMBOLS)
    harness.builder.commit_deferred(plan, lambda: True)
    assert harness.builder.manifests["job"][1]["build_variant"] == "probe"


def test_cancelled_variant_build_publishes_nothing(harness):
    receipt = harness.fetch(FILES)
    plan = harness.prepare(receipt, "probe")
    real = module.build_variant.seed

    def seed_then_stop(build_dir, symbols):
        real(build_dir, symbols)
        harness.builder.request_stop("job")

    with patch.object(module.build_variant, "seed", seed_then_stop):
        with pytest.raises(ValueError) as caught:
            harness.run(plan)
    assert caught.value.args == ("build_interrupted_needs_review",)
    assert not any(args[-1] == "build" for args in harness.calls)
    assert not harness.builder.manifests


# ------------------------------------------------------------------ durable restore


class Durable:
    """A published variant build in the durable store, resolved the way flash resolves it."""

    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path
        self.state = tmp_path / "state"
        self.state.mkdir()
        self.history = self.state / "history.sqlite3"
        self.store = Store(self.history)
        self.project = SimpleNamespace(build_root=tmp_path / "output", target="esp32s3")
        self.settings = SimpleNamespace(
            state_dir=self.state, build_projects={"fixture": self.project}
        )
        self.builder = FirmwareBuild(self.settings, self.history)

    def publish(self, *, variant: bool = True, extra=None):
        job = "build-0"
        fingerprint = hashlib.sha256(job.encode()).hexdigest()
        environment = "e" * 64
        plan = dict(
            job_id=job,
            test="firmware_build",
            build_principal="alice",
            project_id="fixture",
            fingerprint=fingerprint,
        )
        plan_hash = hashlib.sha256(
            json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        self.store.put_plan(plan_hash, plan)
        self.store.add_job(job, plan_hash)
        self.store.set_job(job, "running")
        root = claim_build_directory(self.state, self.project.build_root, fingerprint, environment)

        def receipt(name, data):
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            return dict(name=name, bytes=len(data), sha256=hashlib.sha256(data).hexdigest())

        config = receipt("sdkconfig", b"CONFIG_PROBE=y\n")
        manifest = dict(
            schema_version=1,
            build_job_id=job,
            build_plan_hash=plan_hash,
            idf_version="6.0.1",
            target="esp32s3",
            fingerprint=fingerprint,
            environment_fingerprint=environment,
            artifacts=[receipt("app.bin", b"firmware"), receipt("app.elf", b"ELF")],
            flash_layout=receipt("flasher_args.json", b'{"fixture":"layout"}'),
            flash_security=receipt("config/sdkconfig.json", b'{"fixture":"security"}'),
            flash_authorized=False,
            build_location=build_location(fingerprint, environment, self.project.build_root),
        )
        if variant:
            manifest.update(
                build_variant="probe",
                build_variant_sha256=hashlib.sha256(PROBE).hexdigest(),
                build_variant_config=config,
            )
        manifest.update(extra or {})
        (root / "broker-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        self.builder.artifacts.publish(job, "alice", root, manifest)
        self.store.set_job(job, "succeeded", "firmware_build_succeeded_no_flash")
        return root, manifest

    def resolve(self):
        return self.builder.resolve_flash_manifest("build-0", "alice", "fixture")

    def close(self):
        self.store.close()


@pytest.fixture
def durable(tmp_path):
    value = Durable(tmp_path)
    yield value
    value.close()


def test_restore_resolves_the_verified_variant_build(durable):
    root, manifest = durable.publish()
    assert durable.resolve() == (root, manifest)


def test_restore_without_variant_is_unchanged(durable):
    root, manifest = durable.publish(variant=False)
    assert durable.resolve() == (root, manifest)


@pytest.mark.parametrize("tamper", ["bytes", "delete", "relink"])
def test_restore_rechecks_the_generated_config(durable, tamper):
    root, _ = durable.publish()
    if tamper == "bytes":
        (root / "sdkconfig").write_bytes(b"CONFIG_PROBE=n\n")
    elif tamper == "delete":
        (root / "sdkconfig").unlink()
    else:
        (root / "other").write_bytes(b"CONFIG_PROBE=y\n")
        (root / "sdkconfig").unlink()
        try:
            os.link(root / "other", root / "sdkconfig")
        except OSError:
            pytest.skip("hard links unavailable")
    with pytest.raises(BuildLocationError) as caught:
        durable.resolve()
    assert caught.value.code == "build_output_identity_mismatch"
    # The durable manifest digest is checked first for the plain-byte case, so a manifest
    # edit that hides the tamper is caught as well.


@pytest.mark.parametrize(
    "extra",
    [
        {"build_variant_config": {"name": "sdkconfig", "bytes": 1, "sha256": "a" * 64}},
        {"build_variant": "probe"},
        {"build_variant": "probe", "build_variant_sha256": "a" * 64},
        {"build_variant_sha256": "a" * 64},
    ],
)
def test_restore_refuses_a_half_bound_variant_manifest(durable, extra):
    durable.publish(variant=False, extra=extra)
    with pytest.raises(BuildLocationError) as caught:
        durable.resolve()
    assert caught.value.code == "build_output_identity_mismatch"


# ------------------------------------------------------------------- flash binding


@pytest.mark.parametrize("env", ["variant"], indirect=True)
async def test_flash_binding_carries_one_consistent_variant_identity(env):
    prepared = env.prepare("binding", build_job_id="build-one")
    binding = prepare_binding(env.broker, prepared["plan"])
    assert validate_binding(env.broker, binding, prepared["plan"])
    # Display only: the name appears in the job and recovery views, nowhere as a gate.
    from cardputer_mcp import job_reads

    report = job_reads.recovery(env.broker, "alice")
    entries = [entry for device in report["devices"] for entry in device["lkg_history"]]
    assert entries and all(entry["build_variant"] == "probe" for entry in entries)
    flash_plan = env.store.plan(env.store.job("flash-one")["plan_hash"])
    assert job_reads.build_variant_display(
        flash_plan,
        lambda build_id: env.store.plan(env.store.job(build_id)["plan_hash"]),
    ) == {"build_variant": "probe"}
    assert env.store.firmware_current("fixture") is not None  # a variant build may become LKG


@pytest.mark.parametrize(
    "env",
    [
        "variant:manifest_sha",
        "variant:plan_only",
        "variant:manifest_only",
        "variant:source_changed",
    ],
    indirect=True,
)
async def test_flash_binding_refuses_mismatched_or_unbound_variant_identity(env):
    from cardputer_mcp.broker import BrokerError

    try:
        prepared = env.prepare("binding", build_job_id="build-one")
    except BrokerError as refused:
        assert str(refused) in ("flash_binding_invalid", "backend_preflight_failed")
        return
    with pytest.raises(BrokerError, match="flash_binding_invalid"):
        prepare_binding(env.broker, prepared["plan"])


async def test_recovery_view_omits_the_variant_for_ordinary_builds(env):
    from cardputer_mcp import job_reads

    report = job_reads.recovery(env.broker, "alice")
    entries = [entry for device in report["devices"] for entry in device["lkg_history"]]
    assert entries and all("build_variant" not in entry for entry in entries)


# ----------------------------------------------------------- never echo contents


def test_diagnostics_never_echo_fragment_text_or_paths(harness):
    receipt = harness.fetch(
        {**FILES, "sdkconfig.variant.leaky": b'CONFIG_SECRET="/private/path"\n'}
    )
    with pytest.raises(ValueError) as caught:
        harness.builder.prepare("cardputer", "alice", receipt, "leaky")
    text = repr(caught.value.args) + str(caught.value)
    assert "SECRET" not in text and "private" not in text and str(harness.root) not in text
    assert caught.value.args == ("build_variant_invalid",)
    assert re.fullmatch(r"[a-z_]+", caught.value.args[0])


# ------------------------------------------------------------- over the MCP surface


async def test_variant_build_over_authenticated_mcp(registry_service, tmp_path):
    """submit(kind=build, variant) through the real tool surface, with a fake IDF."""
    if os.name != "nt":
        pytest.skip("the build runs on the external Windows IDF contract")
    from test_source_snapshot import COMMIT, REPOSITORY

    from cardputer_mcp.client import call, connect

    service = registry_service
    profile = tmp_path / "profile.ps1"
    profile.write_text("synthetic")
    project = BuildProject(
        project_dir=tmp_path / "user-worktree",
        trusted_profile=profile,
        build_root=tmp_path / "builds",
        build_min_free_bytes=0,
        source_repository=SourceRepository(
            github_repository=REPOSITORY,
            cache_root=tmp_path / "source-cache",
            private_acl_confirmed=True,
        ),
    )
    settings = service.resident.settings.model_copy(update={"build_projects": {"fixture": project}})
    service.resident.settings = settings
    service.resident.firmware_build.settings = settings
    service.resident.firmware_source.settings = settings
    service.resident.firmware_source.snapshots.settings = settings
    files = {"CMakeLists.txt": b"project(synthetic)\n", **FILES}
    tree = tree_hash({name: file_receipt(data, "100644") for name, data in files.items()})

    def transport(url, deadline, live, limit):
        if "/git/trees/" in url:
            return json.dumps(
                {
                    "sha": tree,
                    "truncated": False,
                    "tree": [
                        {
                            "path": name,
                            "sha": file_receipt(data, "100644")["git_sha1"],
                            "mode": "100644",
                            "type": "blob",
                            "size": len(data),
                        }
                        for name, data in files.items()
                    ],
                }
            ).encode()
        if "/git/commits/" in url or url.startswith("https://api.github.com/"):
            return json.dumps({"sha": COMMIT, "tree": {"sha": tree}}).encode()
        return fixture.archive(files, repository=REPOSITORY, commit=COMMIT)

    service.resident.firmware_source.snapshots.transport = transport
    sdk = tmp_path / "sdk"
    idf_py = sdk / "idf/tools/idf.py"
    python = sdk / "python/Scripts/python.exe"
    compiler = sdk / "bin/xtensa-esp32s3-elf-gcc.exe"
    system = sdk / "system"
    powershell = system / "System32/WindowsPowerShell/v1.0/powershell.exe"
    for tool in (idf_py, python, compiler, powershell):
        tool.parent.mkdir(parents=True, exist_ok=True)
        tool.write_bytes(b"synthetic executable; never invoked")
    seeded: list[bytes | None] = []

    class FakeIdf:
        active = uncertain = False
        stop_requested = threading.Event()

        def run(self, args, env, cwd, emit, timeout=1800, monitor=None):
            if "-e" in args:
                for line in (
                    "IDF_PATH=" + str(sdk / "idf"),
                    "IDF_PYTHON_ENV_PATH=" + str(sdk / "python"),
                    "PATH=" + str(sdk / "bin"),
                ):
                    emit(line, "stdout")
            elif args[-1] == "--version":
                emit("ESP-IDF v6.0.1" if str(idf_py) in args else "version", "stdout")
            elif args[-1] == "build":
                directory = Path(args[args.index("-B") + 1])
                config = directory / "sdkconfig"
                seeded.append(config.read_bytes() if config.exists() else None)
                config.write_bytes((seeded[-1] or b"") + b"CONFIG_GENERATED=1\n")
                (directory / "project_description.json").write_text('{"target":"esp32s3"}')
                (directory / "firmware.bin").write_bytes(b"synthetic firmware")
                (directory / "flasher_args.json").write_text(
                    json.dumps(
                        {
                            "flash_files": {"0x10000": "firmware.bin"},
                            "extra_esptool_args": {"chip": "esp32s3"},
                        }
                    )
                )
                (directory / "config").mkdir(exist_ok=True)
                (directory / "config/sdkconfig.json").write_text(
                    json.dumps(
                        {
                            "IDF_TARGET": "esp32s3",
                            "SECURE_BOOT": False,
                            "SECURE_FLASH_ENC_ENABLED": False,
                            "BOOTLOADER_APP_ROLLBACK_ENABLE": False,
                        }
                    )
                )
            else:
                raise AssertionError("unexpected SDK command")
            return 0

    key = service.keys["second"]
    async with connect(service.url, key.token) as remote:
        fetch = await call(
            remote,
            "submit",
            {"job_id": "fetch", "kind": "fetch", "project_id": "fixture", "source_commit": COMMIT},
        )
        assert (await call(remote, "execute", {"op": "run", "plan_hash": fetch["plan_hash"]}))["ok"]
        await service.resident.profile_jobs.queue.join()
        logs = await call(remote, "status", {"view": "logs", "stream": "build", "job_id": "fetch"})
        receipt = logs["source"]["source_receipt"]

        def build(job, **extra):
            return call(
                remote,
                "submit",
                {"job_id": job, "kind": "build", "project_id": "fixture", **extra},
            )

        # Fixed reasons only; no fragment text or path is echoed.
        refused = await build("no-source", variant="probe")
        assert refused == {"ok": False, "error": "build_variant_requires_source_build"}
        refused = await build("unknown", source_receipt=receipt, variant="nothing")
        assert refused == {"ok": False, "error": "build_variant_not_found"}
        refused = await build("bad", source_receipt=receipt, variant="bad")
        assert refused == {"ok": False, "error": "build_variant_invalid"}
        refused = await build("secure", source_receipt=receipt, variant="secure")
        assert refused == {"ok": False, "error": "build_variant_invalid"}
        with pytest.raises(ValueError, match="MCP request rejected"):
            await build("pattern", source_receipt=receipt, variant="no good")
        for job in ("no-source", "unknown", "bad", "secure", "pattern"):
            assert service.resident._store().job(job) is None

        plain = await build("plain", source_receipt=receipt)
        probe = await build("probe", source_receipt=receipt, variant="probe")
        assert plain["ok"] and probe["ok"], (plain, probe)
        assert not [name for name in plain["plan"] if name.startswith("build_variant")]
        assert probe["plan"]["build_variant"] == "probe"
        assert probe["plan"]["build_variant_sha256"] == hashlib.sha256(PROBE).hexdigest()
        assert probe["plan"]["fingerprint"] != plain["plan"]["fingerprint"]
        service.resident.firmware_build.process = FakeIdf()
        native = SimpleNamespace(name="nt", environ={"SYSTEMROOT": str(system)})
        with patch.object(module, "os", native):
            for prepared in (plain, probe):
                run = {"op": "run", "plan_hash": prepared["plan_hash"]}
                assert (await call(remote, "execute", run))["ok"]
                await service.resident.profile_jobs.queue.join()
        assert service.resident.job("plain")["state"] == "succeeded"
        assert service.resident.job("probe")["state"] == "succeeded"
        assert seeded[0] is None and seeded[1].startswith(b"CONFIG_LEVEL=3")
        probe_logs = {"view": "logs", "stream": "build", "job_id": "probe"}
        manifest = (await call(remote, "status", probe_logs))["manifest"]
        assert manifest["build_variant"] == "probe"
        assert manifest["build_variant_sha256"] == probe["plan"]["build_variant_sha256"]
        assert manifest["build_variant_config"]["name"] == "sdkconfig"
        assert manifest["flash_authorized"] is False
        plain_logs = {"view": "logs", "stream": "build", "job_id": "plain"}
        plain_manifest = (await call(remote, "status", plain_logs))["manifest"]
        assert not [name for name in plain_manifest if name.startswith("build_variant")]
        assert service.backend.calls == 0
