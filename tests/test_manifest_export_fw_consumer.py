"""Unmodified broker responses meet the pinned FW gate; synthetic records only."""

import copy
import hashlib
import importlib.machinery
import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import test_build_artifact_persistence as persistence
from test_dma_modeproof import DMA_ENTRY, dma_fixture

import cardputer_mcp.diagnostic_registry as registry
from cardputer_mcp.build_artifacts import manifest_digest
from cardputer_mcp.build_logs import BuildLogs
from cardputer_mcp.diagnostic_dma_modeproof import collect_dma_modeproof
from cardputer_mcp.manifest_export import canonical_json, export_response
from cardputer_mcp.models import BuildLogRequest

FW_HEAD = "824e9f91b52069c08df68e967d83b44e9f7d5240"


def load_fw(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_file_location(name, path, loader=loader)
    if spec is None or spec.loader is None:
        raise AssertionError("Missing pinned FW consumer")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def publish_dma(case, detail, dma):
    root, manifest = case.publish(finish=False, publish=False)
    source, artifacts, _ = dma_fixture(root, detail, dma)
    envelope = collect_dma_modeproof(root, detail, dma, source, artifacts)
    manifest.update(
        source_provenance=source,
        derby_detail_mode=detail,
        derby_dma_mode=dma,
        diagnostic_entry_sha256=DMA_ENTRY.sha256,
        diagnostic_modeproof=envelope,
        artifacts=artifacts,
    )
    job = case.store.job("build-0")
    plan = case.store.plan(job["plan_hash"])
    plan.update(
        derby_detail_mode=detail,
        derby_dma_mode=dma,
        source_provenance=source,
        diagnostic_entry_sha256=DMA_ENTRY.sha256,
    )
    plan_hash = manifest_digest(plan)
    case.store.put_plan(plan_hash, plan)
    case.store.db.execute("UPDATE jobs SET plan_hash=? WHERE id=?", (plan_hash, "build-0"))
    case.store.db.commit()
    manifest["build_plan_hash"] = plan_hash
    (root / "broker-manifest.json").write_text(json.dumps(manifest))
    case.builder.artifacts.publish("build-0", "alice", root, manifest)
    case.store.set_job("build-0", "succeeded", "firmware_build_succeeded_no_flash")
    return root, manifest


class FixedFirmwareConsumerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture = Path(__file__).parent / "fixtures/derby_manifest_export_pr38.py.txt"
        if hashlib.sha256(fixture.read_bytes()).hexdigest() != (
            "fb275ee8e763e89a59d5deea132529e9d18787d731fae0f27b84acd06f1fc726"
        ):
            raise AssertionError("Pinned PR38 consumer changed")
        cls.consumer = load_fw("pinned_export_consumer", fixture)
        cls.tools = Path(os.environ.get("DERBY_FW_ROOT", ".")).resolve() / "tools"

    def bind_response(self, response, proof, plan, job, anchor, detail, dma, cache_bytes):
        manifest, wrapper, commitment = self.consumer.read_export_response(response)
        return self.consumer.bind_export(
            manifest,
            proof,
            plan,
            job,
            wrapper=wrapper,
            supplied_digest=commitment,
            expected_flash_plan_hash=anchor,
            expected_detail=detail,
            expected_dma=dma,
            expected_cache_bytes=cache_bytes,
        )

    @unittest.skipUnless(os.environ.get("DERBY_FW_ROOT"), "Run the pinned FW junction workflow")
    def test_unmodified_producer_response_four_choices_complete_recorded_chain(self):
        before_path = sys.path[:]
        try:
            sys.path[:0] = [str(self.tools), str(self.tools / "games")]
            fixture = load_fw("pinned_export_fixture", self.tools / "test_derby_manifest_export.py")
            for detail in ("OFF", "ON"):
                for dma in ("OFF", "ON"):
                    with self.subTest(detail=detail, dma=dma):
                        args, options = fixture.sample(detail, dma)
                        original = copy.deepcopy(args[3])
                        envelope = original["diagnostic_modeproof"]
                        envelope.pop("cache_evidence")
                        envelope["cmake_cache"] = (
                            f"POCKET_DERBY_DETAIL_MEASURE:BOOL={detail}\n"
                            f"KASANE_DMA_OVERLAP:BOOL={dma}\n"
                        )
                        response = export_response(original)
                        before = canonical_json(response)
                        manifest, wrapper, commitment = self.consumer.read_export_response(response)
                        self.assertNotIn("export_sha256", response)
                        args[3] = manifest
                        options.update(manifest_export=wrapper, manifest_export_sha256=commitment)
                        result = fixture.verify_recorded_trial(*args, **options)
                        self.assertTrue(result["recorded_chain_matches"])
                        self.assertFalse(result["hardware_tested"])
                        self.assertEqual(canonical_json(response), before)
        finally:
            sys.path[:] = before_path

    def test_unmodified_durable_response_four_choices_with_fixed_anchor(self):
        for detail in ("OFF", "ON"):
            for dma in ("OFF", "ON"):
                with self.subTest(detail=detail, dma=dma), patch.object(
                    registry, "_ENTRIES", (*registry._ENTRIES, DMA_ENTRY)
                ):
                    case = persistence.DurableBuildArtifactTests()
                    case.setUp()
                    try:
                        root, original = publish_dma(case, detail, dma)
                        proof = original["diagnostic_modeproof"]["proof"]
                        cache_bytes = len((root / "CMakeCache.txt").read_bytes())
                        logs = BuildLogs("alice")
                        logs.append("synthetic retained output")
                        case.builder.logs["build-0"] = logs
                        initial = case.builder.read_logs(
                            BuildLogRequest(job_id="build-0"), "alice", project_id="fixture"
                        )
                        plan = {
                            "job_id": "synthetic-flash",
                            "manifest_sha256": manifest_digest(original),
                            "manifest_export_sha256": initial["manifest_export_sha256"],
                        }
                        anchor = hashlib.sha256(canonical_json(plan)).hexdigest()
                        job = {"id": plan["job_id"], "state": "succeeded", "plan_hash": anchor}
                        expected = initial["manifest_export"], initial["manifest_export_sha256"]
                        for retention in ("fresh", "restart", "expired"):
                            if retention != "fresh":
                                case.restart()
                            if retention == "expired":
                                logs = BuildLogs("alice")
                                logs.deadline = 0
                                case.builder.logs["build-0"] = logs
                            before_db = list(case.store.db.iterdump())
                            response = case.builder.read_logs(
                                BuildLogRequest(job_id="build-0"), "alice", project_id="fixture"
                            )
                            before = canonical_json(response)
                            self.assertEqual(response["artifact_status"], "verified")
                            self.assertIs(response["expired"], retention != "fresh")
                            self.assertNotIn("export_sha256", response)
                            self.assertEqual(
                                (response["manifest_export"], response["manifest_export_sha256"]),
                                expected,
                            )
                            digest, report = self.bind_response(
                                response, proof, plan, job, anchor, detail, dma, cache_bytes
                            )
                            self.assertEqual(digest, manifest_digest(original))
                            self.assertFalse(report["original_manifest_recomputed"])
                            self.assertEqual(canonical_json(response), before)
                            self.assertEqual(list(case.store.db.iterdump()), before_db)
                            for mutation in ("old_name", "null", "export_rehash", "plan_rehash"):
                                bad, bad_plan, bad_job = map(copy.deepcopy, (response, plan, job))
                                if mutation == "old_name":
                                    bad["export_sha256"] = bad.pop("manifest_export_sha256")
                                elif mutation == "null":
                                    bad["manifest_export_sha256"] = None
                                elif mutation == "export_rehash":
                                    bad["manifest_export"]["original_manifest_sha256"] = "0" * 64
                                    bad["manifest_export_sha256"] = hashlib.sha256(
                                        canonical_json(bad["manifest_export"])
                                    ).hexdigest()
                                else:
                                    bad_plan["job_id"] = bad_job["id"] = "other-flash"
                                    bad_job["plan_hash"] = hashlib.sha256(
                                        canonical_json(bad_plan)
                                    ).hexdigest()
                                with self.subTest(retention=retention, mutation=mutation):
                                    with self.assertRaises(ValueError):
                                        self.bind_response(
                                            bad,
                                            proof,
                                            bad_plan,
                                            bad_job,
                                            anchor,
                                            detail,
                                            dma,
                                            cache_bytes,
                                        )
                    finally:
                        case.doCleanups()
