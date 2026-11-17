import hashlib,json,sqlite3,unittest
from cardputer_mcp.source_retention import retention_evidence


class RestoreRetentionTest(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:');self.db.row_factory=sqlite3.Row
        self.db.executescript('''
        CREATE TABLE plans(hash TEXT,payload TEXT);
        CREATE TABLE jobs(id TEXT,plan_hash TEXT,state TEXT,diagnostic TEXT,created TEXT,finished TEXT);
        CREATE TABLE device_requests(lease_id TEXT,state TEXT,finished TEXT);
        CREATE TABLE firmware_lkg_history(receipt TEXT,completed_job TEXT,kind TEXT,principal TEXT,device_id TEXT);
        CREATE TABLE firmware_lkg(receipt TEXT,completed_job TEXT,kind TEXT,principal TEXT,device_id TEXT);
        CREATE TABLE source_publications(job_id TEXT,project_id TEXT,receipt TEXT);
        ''')
        self.store=type('Store',(),{'db':self.db})()
        self.receipt='a'*64;self.lkg='b'*64
        self.add('fetch',{'test':'firmware_fetch_source','project_id':'p','source_principal':'alice'},
                 {'source':{'source_receipt':self.receipt}})
        self.add('build',{'test':'firmware_build','project_id':'p','build_principal':'alice','source_receipt':self.receipt})
        self.add('flash',{'test':'firmware_flash','build_job_id':'build','flash_principal':'alice','device_id':'dev'})
        self.db.execute('INSERT INTO firmware_lkg_history VALUES (?,?,?,?,?)',(self.lkg,'flash','artifact_bundle','alice','dev'))
    def add(self,job,plan,diagnostic=None,state='succeeded'):
        plan={'job_id':job,**plan};payload=json.dumps(plan,sort_keys=True,separators=(',',':'))
        digest=hashlib.sha256(payload.encode()).hexdigest()
        self.db.execute('INSERT INTO plans VALUES (?,?)',(digest,payload))
        self.db.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?)',(job,digest,state,json.dumps(diagnostic) if diagnostic else None,'date','date'))
    def selected(self,receipt=None,state='succeeded',principal='bob',recovery='artifact_bundle',device='dev',kind='firmware_restore'):
        self.add('restore',{'test':kind,'flash_principal':principal,'lkg_receipt':receipt or self.lkg,
                            'recovery_kind':recovery,'device_id':device},state=state)
        self.db.execute('INSERT INTO firmware_lkg VALUES (?,?,?,?,?)',(self.lkg,'restore','artifact_bundle',principal,'dev'))
    def replan(self,job,**changes):
        row=self.db.execute('SELECT p.hash,p.payload FROM plans p JOIN jobs j ON j.plan_hash=p.hash WHERE j.id=?',(job,)).fetchone()
        plan=json.loads(row['payload']);plan.update(changes)
        payload=json.dumps(plan,sort_keys=True,separators=(',',':'));digest=hashlib.sha256(payload.encode()).hexdigest()
        self.db.execute('INSERT INTO plans VALUES (?,?)',(digest,payload))
        self.db.execute('UPDATE jobs SET plan_hash=? WHERE id=?',(digest,job))
        self.db.execute('DELETE FROM plans WHERE hash=?',(row['hash'],))
    def policy(self):return retention_evidence(self.store,'p','bob','epoch')
    def test_successful_cross_principal_restore_preserves_source(self):
        self.selected();p=self.policy();self.assertTrue(p['complete'])
        self.assertIn('current_lkg_evidence',p['protected'][self.receipt])
    def test_successful_same_principal_restore_preserves_source(self):
        self.selected(principal='alice');p=self.policy();self.assertTrue(p['complete'])
        self.assertIn('current_lkg_evidence',p['protected'][self.receipt])
    def test_wrong_receipt_fails_closed(self):
        self.selected(receipt='c'*64);self.assertFalse(self.policy()['complete'])
    def test_unsuccessful_restore_fails_closed(self):
        self.selected(state='failed');self.assertFalse(self.policy()['complete'])
    def test_wrong_recovery_kind_fails_closed(self):
        self.selected(recovery='full_backup');self.assertFalse(self.policy()['complete'])
    def test_forged_current_principal_fails_closed(self):
        self.selected();self.db.execute("UPDATE firmware_lkg SET principal='mallory'")
        self.assertFalse(self.policy()['complete'])
    def test_wrong_device_fails_closed(self):
        self.selected();self.db.execute("UPDATE firmware_lkg SET device_id='other'")
        self.assertFalse(self.policy()['complete'])
    def test_restore_plan_wrong_device_fails_closed(self):
        self.selected(device='other');self.assertFalse(self.policy()['complete'])
    def test_restore_plan_wrong_kind_fails_closed(self):
        self.selected(kind='firmware_flash');self.assertFalse(self.policy()['complete'])
    def test_original_flash_failure_fails_closed(self):
        self.selected();self.db.execute("UPDATE jobs SET state='failed' WHERE id='flash'")
        self.assertFalse(self.policy()['complete'])
    def test_original_build_failure_fails_closed(self):
        self.selected();self.db.execute("UPDATE jobs SET state='failed' WHERE id='build'")
        self.assertFalse(self.policy()['complete'])
    def test_missing_original_flash_fails_closed(self):
        self.selected();self.db.execute("DELETE FROM jobs WHERE id='flash'")
        self.assertFalse(self.policy()['complete'])
    def test_missing_original_build_fails_closed(self):
        self.selected();self.db.execute("DELETE FROM jobs WHERE id='build'")
        self.assertFalse(self.policy()['complete'])
    def test_original_flash_wrong_device_fails_closed(self):
        self.selected();self.replan('flash',device_id='other');self.assertFalse(self.policy()['complete'])
    def test_original_flash_wrong_kind_fails_closed(self):
        self.selected();self.replan('flash',test='firmware_restore');self.assertFalse(self.policy()['complete'])
    def test_original_build_wrong_kind_fails_closed(self):
        self.selected();self.replan('build',test='firmware_fetch_source');self.assertFalse(self.policy()['complete'])
    def test_original_flash_wrong_owner_fails_closed(self):
        self.selected();self.replan('flash',flash_principal='mallory');self.assertFalse(self.policy()['complete'])
    def test_original_build_wrong_owner_fails_closed(self):
        self.selected();self.replan('build',build_principal='mallory');self.assertFalse(self.policy()['complete'])
    def test_sequential_different_principal_restores(self):
        self.selected();self.db.execute('DELETE FROM firmware_lkg')
        self.add('restore2',{'test':'firmware_restore','flash_principal':'carol','lkg_receipt':self.lkg,
                             'recovery_kind':'artifact_bundle','device_id':'dev'})
        self.db.execute('INSERT INTO firmware_lkg VALUES (?,?,?,?,?)',(self.lkg,'restore2','artifact_bundle','carol','dev'))
        p=self.policy();self.assertTrue(p['complete']);self.assertIn('current_lkg_evidence',p['protected'][self.receipt])
    def test_same_owner_flash_still_protects_source(self):
        self.db.execute('INSERT INTO firmware_lkg VALUES (?,?,?,?,?)',(self.lkg,'flash','artifact_bundle','alice','dev'))
        p=self.policy();self.assertTrue(p['complete']);self.assertIn('current_lkg_evidence',p['protected'][self.receipt])


if __name__=='__main__':unittest.main()
