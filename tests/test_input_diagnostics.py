"""Request diagnostics expose fixed schema vocabulary and never input values."""

import json
import unittest

from pydantic import ValidationError

from cardputer_mcp.input_diagnostics import validate_arguments
from cardputer_mcp.models import PrepareRequest


class InputDiagnosticTests(unittest.TestCase):
    def flash(self, **changes):
        return dict(
            job_id="synthetic-job", test="firmware_flash", session="a" * 64,
            device_id="synthetic-device", build_job_id="synthetic-build", **changes,
        )

    def test_field_and_model_failures_use_only_fixed_reasons_and_fields(self):
        valid = self.flash()
        cases = [
            ({**valid, "session": None}, "device_session_required", ["session"]),
            ({**valid, "device_id": None}, "registered_flash_device_required", ["device_id"]),
            ({**valid, "project_id": "SYNTHETIC_PRIVATE"}, "project_id_not_allowed_for_flash_operations", ["project_id"]),
            ({**valid, "build_job_id": None}, "successful_build_job_required", ["build_job_id"]),
            ({**valid, "job_id": "https://SYNTHETIC_SECRET.invalid"}, "invalid_field_format", ["job_id"]),
            ({**valid, "test": "SYNTHETIC_SECRET"}, "invalid_field_value", ["test"]),
            ({**valid, "SYNTHETIC_SECRET_FIELD": "SYNTHETIC_SECRET_VALUE"}, "unexpected_field", ["request"]),
        ]
        for request, reason, fields in cases:
            with self.subTest(reason=reason):
                result = validate_arguments("prepare_test", {"request": request})
                self.assertEqual(result, dict(
                    ok=False, error="invalid_tool_request", reason=reason, fields=fields,
                ))
                self.assertNotIn("SYNTHETIC", json.dumps(result))
                self.assertNotIn("https://", json.dumps(result))

    def test_other_tools_and_missing_request_have_safe_field_diagnostics(self):
        self.assertEqual(validate_arguments("prepare_test", {})["fields"], ["request"])
        result = validate_arguments("read_build_logs", {"request": {
            "job_id": "safe", "cursor": {"epoch": "SYNTHETIC_PRIVATE", "sequence": 0}
        }})
        self.assertEqual(result["fields"], ["cursor", "epoch"])
        self.assertNotIn("SYNTHETIC", json.dumps(result))

    def test_diagnostics_do_not_change_valid_optional_nulls_or_model_admission(self):
        for test in ("firmware_flash", "firmware_backup", "firmware_restore"):
            request = self.flash()
            request["test"] = test
            if test != "firmware_flash":
                request.pop("build_job_id")
            for null_project in (False, True):
                with self.subTest(test=test, null_project=null_project):
                    candidate = {**request, **({"project_id": None} if null_project else {})}
                    PrepareRequest.model_validate(candidate)
                    self.assertIsNone(validate_arguments("prepare_test", {"request": candidate}))
                    try:
                        import jsonschema
                    except ModuleNotFoundError:
                        continue
                    jsonschema.validate(candidate, PrepareRequest.model_json_schema())
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
