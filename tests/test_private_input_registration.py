import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from pydantic import ValidationError

from cardputer_mcp.config import BuildProject, Settings
from cardputer_mcp.private_input_registration import PrivateInputRegistration


class PrivateInputRegistrationTests(unittest.TestCase):
    def args(self):
        return dict(
            local_root=str(Path.cwd() / "unopened-private-fixture"),
            destination="main/private_input",
            source_commit="a" * 40,
            inventory_sha256="b" * 64,
            recipe_sha256="c" * 64,
        )

    def project(self, inputs):
        return BuildProject(
            project_dir=Path.cwd(), trusted_profile=Path.cwd() / "build.py", private_inputs=inputs
        )

    def test_default_has_no_inputs(self):
        self.assertEqual(self.project({}).private_inputs, {})

    def test_registration_does_not_read_or_create_input(self):
        with TemporaryDirectory() as tmp:
            target = Path(tmp) / "absent"
            with patch.object(Path, "open", side_effect=AssertionError("unexpected I/O")):
                registration = PrivateInputRegistration(**(self.args() | {"local_root": target}))
                self.project({"fixture": registration})
            self.assertFalse(target.exists())

    def test_private_location_not_in_public_dump_or_repr(self):
        registration = PrivateInputRegistration(**self.args())
        project = self.project({"fixture": registration})
        settings = Settings(build_projects={"fixture": project})
        for value in (
            registration.model_dump_json(),
            repr(registration),
            settings.model_dump_json(),
            repr(settings),
        ):
            self.assertNotIn("unopened-private-fixture", value)
        self.assertNotIn("private_inputs", project.model_dump())

    def test_strict_registration(self):
        for change in (
            {"local_root": "relative"},
            {"local_root": "/tmp/../x"},
            {"destination": "../escape"},
            {"destination": "CON.txt"},
            {"source_commit": "main"},
            {"inventory_sha256": "bad"},
            {"max_bytes": True},
            {"max_files": 100001},
            {"url": "https://invalid.example/input"},
        ):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                PrivateInputRegistration(**(self.args() | change))

    def test_input_id_bounded_and_no_path(self):
        for name in ("../input", "UPPER", "a" * 65, "", "with space"):
            with self.subTest(name=name), self.assertRaises(ValidationError):
                self.project({name: self.args()})

    def test_identity_binds_all_registration_fields(self):
        args = self.args()
        registration = PrivateInputRegistration(**args)
        digest = registration.identity(project_id="project", input_id="fixture")
        self.assertEqual(digest, registration.identity(project_id="project", input_id="fixture"))
        for key, value in {
            "local_root": str(Path.cwd() / "other"),
            "destination": "main/other",
            "source_commit": "d" * 40,
            "inventory_sha256": "e" * 64,
            "recipe_sha256": "f" * 64,
            "max_files": 1,
            "max_bytes": 1,
        }.items():
            changed = PrivateInputRegistration(**(args | {key: value}))
            self.assertNotEqual(digest, changed.identity(project_id="project", input_id="fixture"))
        self.assertNotEqual(digest, registration.identity(project_id="other", input_id="fixture"))
        self.assertNotEqual(digest, registration.identity(project_id="project", input_id="other"))

    def test_invalid_registration_does_not_echo_private_root(self):
        for build in (
            lambda: self.project({"bad/id": self.args()}),
            lambda: self.project({"fixture": self.args() | {"destination": "../x"}}),
        ):
            with self.assertRaises(ValidationError) as caught:
                build()
            self.assertNotIn("unopened-private-fixture", str(caught.exception))
