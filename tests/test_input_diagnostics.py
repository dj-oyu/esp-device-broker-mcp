"""Request diagnostics expose fixed schema vocabulary and never input values."""

import json
import unittest

from cardputer_mcp.input_diagnostics import REQUEST_MODELS, validate_arguments
from cardputer_mcp.models import PrepareRequest


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
        for tool in REQUEST_MODELS:
            self.assertEqual(validate_arguments(tool, {})["fields"], ["request"])
        nested = {"view": "logs", "stream": "build", "job_id": "safe", "levels": ["SYNTHETIC"]}
        result = validate_arguments("status", {"request": nested})
        self.assertEqual(result["fields"], ["levels"])
        self.assertNotIn("SYNTHETIC", json.dumps(result))
        base = {
            "job_id": "build",
            "test": "firmware_build",
            "project_id": "fixture",
            "source_receipt": "a" * 64,
        }
        for mode in ("ON", "OFF"):
            self.assertEqual(PrepareRequest(**base, derby_detail_mode=mode).derby_detail_mode, mode)
        for mode in ("-DANY=1", "on", "ON;echo unsafe", True):
            with self.assertRaises(ValueError):
                PrepareRequest(**base, derby_detail_mode=mode)
        with self.assertRaises(ValueError):
            PrepareRequest(job_id="build", test="firmware_build", derby_detail_mode="OFF")
        with self.assertRaises(ValueError):
            PrepareRequest(**base, flash_job_id="flash")

    def test_request_models_cover_exactly_the_six_surface_tools(self):
        self.assertEqual(
            set(REQUEST_MODELS), {"status", "submit", "execute", "flash", "cancel", "session"}
        )
        for old in ("prepare_test", "execute_test", "read_build_logs", "cancel_job"):
            self.assertIsNone(validate_arguments(old, {"request": {"SYNTHETIC": 1}}))

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
