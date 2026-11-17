import unittest
from test_source_lifecycle import CacheFixture
from cardputer_mcp.source_payloads import _reclaim
from cardputer_mcp.source_snapshot import SourceError


class CrossRestoreReclamation(CacheFixture,unittest.TestCase):
    def setUp(self):self.setup_cache()
    def test_other_principal_restore_preserves_referenced_payload(self):
        source=self.fetch(1,principal='alice');receipt=source['source_receipt'];lkg='b'*64
        self.add({'job_id':'build','test':'firmware_build','project_id':'fixture',
                  'build_principal':'alice','source_receipt':receipt},'succeeded')
        self.add({'job_id':'flash','test':'firmware_flash','device_id':'fixture',
                  'flash_principal':'alice','build_job_id':'build'},'running')
        self.store.finish_firmware('flash','ok',{'receipt':lkg,'device_id':'fixture','principal':'alice',
            'kind':'artifact_bundle','baseline':'flash'},lambda:True)
        self.add({'job_id':'restore','test':'firmware_restore','device_id':'fixture',
                  'flash_principal':'bob','recovery_kind':'artifact_bundle','lkg_receipt':lkg},'running')
        self.store.finish_firmware('restore','ok',{'receipt':lkg,'device_id':'fixture','principal':'bob',
            'kind':'artifact_bundle','baseline':'flash'},lambda:True)
        policy=self.evidence();self.assertTrue(policy['complete'])
        self.assertIn('current_lkg_evidence',policy['protected'][receipt])
        path,metadata=self.snapshots.resolve('fixture',receipt,'alice')
        with self.assertRaisesRegex(SourceError,'source_cache_protected'):
            with self.snapshots.cache_guard('fixture',write=True):
                _reclaim(self.snapshots,self.plan(2),receipt,metadata,lambda:True)
        self.assertTrue(path.is_dir())
