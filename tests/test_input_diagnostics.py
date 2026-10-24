"""Request diagnostics expose fixed schema vocabulary and never input values."""

import json
import unittest

from pydantic import ValidationError

from cardputer_mcp.input_diagnostics import REQUEST_MODELS, validate_arguments
from cardputer_mcp.models import PrepareRequest
from cardputer_mcp.surface import SubmitRequest


class InputDiagnosticTests(unittest.TestCase):
    def flash(self, **changes):
        return dict(
            job_id="synthetic-job",
            test="firmware_flash",
            session="a" * 64,
            device_id="synthetic-device",
            build_job_id="synthetic-build",
            **changes,
        )

    def submit(self, **changes):
        return dict(
            {
                "kind": "flash",
                "job_id": "synthetic-job",
                "session": "a" * 64,
                "device_id": "synthetic-device",
                "build_job_id": "synthetic-build",
            },
            **changes,
        )

    def test_field_failures_use_only_fixed_reasons_and_fields(self):
        valid = self.submit()
        cases = [
            (
                "submit",
                {**valid, "job_id": "https://SYNTHETIC_SECRET.invalid"},
                "invalid_field_format",
                ["job_id"],
            ),
            (
                "submit",
                {**valid, "session": "https://SYNTHETIC_SECRET.invalid"},
                "invalid_field_format",
                ["session"],
            ),
            ("submit", {**valid, "kind": "SYNTHETIC_SECRET"}, "invalid_field_value", ["kind"]),
            (
                "submit",
                {**valid, "SYNTHETIC_SECRET_FIELD": "SYNTHETIC_SECRET_VALUE"},
                "unexpected_field",
                ["request"],
            ),
            (
                "submit",
                {key: value for key, value in valid.items() if key != "job_id"},
                "required_field",
                ["job_id"],
            ),
            ("flash", {"session": "a" * 64}, "required_field", ["plan_hash"]),
            (
                "flash",
                {"plan_hash": "SYNTHETIC_SECRET"},
                "invalid_field_format",
                ["plan_hash"],
            ),
            ("status", {"view": "SYNTHETIC_SECRET"}, "invalid_field_value", ["view"]),
            ("status", {"view": "logs", "limit": 0}, "field_out_of_range", ["limit"]),
            ("execute", {"op": "SYNTHETIC_SECRET"}, "invalid_field_value", ["op"]),
            ("cancel", {"target": "SYNTHETIC_SECRET"}, "invalid_field_value", ["target"]),
            (
                "session",
                {"action": "open", "owner": "remote_mcp", "ttl_seconds": 10**9},
                "field_out_of_range",
                ["ttl_seconds"],
            ),
        ]
        for tool, request, reason, fields in cases:
            with self.subTest(tool=tool, reason=reason, fields=fields):
                result = validate_arguments(tool, {"request": request})
                self.assertEqual(
                    result,
                    dict(
                        ok=False,
                        error="invalid_tool_request",
                        reason=reason,
                        fields=fields,
                    ),
                )
                self.assertNotIn("SYNTHETIC", json.dumps(result))
                self.assertNotIn("https://", json.dumps(result))

    def test_request_models_cover_exactly_the_six_surface_tools(self):
        self.assertEqual(
            set(REQUEST_MODELS), {"status", "submit", "execute", "flash", "cancel", "session"}
        )
        for old in ("prepare_test", "execute_test", "read_build_logs", "cancel_job"):
            self.assertIsNone(validate_arguments(old, {"request": {"SYNTHETIC": 1}}))

    def test_missing_request_and_nested_names_have_safe_field_diagnostics(self):
        for tool in REQUEST_MODELS:
            with self.subTest(tool=tool):
                self.assertEqual(validate_arguments(tool, {})["fields"], ["request"])
        result = validate_arguments(
            "status",
            {
                "request": {
                    "view": "logs",
                    "stream": "build",
                    "job_id": "safe",
                    "levels": ["SYNTHETIC_PRIVATE"],
                }
            },
        )
        self.assertEqual(result["fields"], ["levels"])
        self.assertNotIn("SYNTHETIC", json.dumps(result))

    def test_diagnostics_do_not_change_valid_optional_nulls_or_model_admission(self):
        for kind in ("flash", "backup", "restore"):
            request = self.submit(kind=kind)
            if kind != "flash":
                request.pop("build_job_id")
            for null_project in (False, True):
                with self.subTest(kind=kind, null_project=null_project):
                    candidate = {**request, **({"project_id": None} if null_project else {})}
                    SubmitRequest.model_validate(candidate)
                    self.assertIsNone(validate_arguments("submit", {"request": candidate}))
                    try:
                        import jsonschema
                    except ModuleNotFoundError:
                        continue
                    jsonschema.validate(candidate, SubmitRequest.model_json_schema())
        # The legacy plan model still refuses a project on flash operations.
        for test in ("firmware_flash", "firmware_backup", "firmware_restore"):
            request = self.flash()
            request["test"] = test
            if test != "firmware_flash":
                request.pop("build_job_id")
            PrepareRequest.model_validate(request)
        with self.assertRaises(ValidationError):
            PrepareRequest.model_validate(self.flash(project_id="not-permitted"))

    def test_conditional_schema_requires_the_same_flash_references(self):
        import jsonschema

        schema = PrepareRequest.model_json_schema()
        valid = self.flash()
        jsonschema.validate(valid, schema)
        for name in ("session", "device_id", "build_job_id"):
            with self.subTest(field=name):
                request = dict(valid)
                request.pop(name)
                with self.assertRaises(jsonschema.ValidationError):
                    jsonschema.validate(request, schema)
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate({**valid, "project_id": "forbidden"}, schema)


if __name__ == "__main__":
    unittest.main()
