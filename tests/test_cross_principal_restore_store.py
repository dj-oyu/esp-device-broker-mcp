import hashlib,json
from cardputer_mcp.source_retention import retention_evidence
from cardputer_mcp.store import Store


def test_restore_owner_change_keeps_original_source_protected(tmp_path):
    store=Store(tmp_path/'state.sqlite3')
    def add(job,plan,diagnostic=None,state='succeeded'):
        plan={'job_id':job,**plan};raw=json.dumps(plan,sort_keys=True,separators=(',',':'))
        digest=hashlib.sha256(raw.encode()).hexdigest()
        store.put_plan(digest,plan);store.add_job(job,digest)
        store.set_job(job,state,diagnostic=diagnostic)
    source='a'*64;receipt='b'*64
    add('fetch',{'test':'firmware_fetch_source','project_id':'p','source_principal':'alice'},
        {'source':{'source_receipt':source}})
    add('build',{'test':'firmware_build','project_id':'p','build_principal':'alice','source_receipt':source})
    add('flash',{'test':'firmware_flash','device_id':'dev','build_job_id':'build','flash_principal':'alice'},state='running')
    store.finish_firmware('flash','ok',{'receipt':receipt,'device_id':'dev','principal':'alice',
        'kind':'artifact_bundle','baseline':'flash'},lambda:True)
    add('restore',{'test':'firmware_restore','device_id':'dev','flash_principal':'bob',
        'recovery_kind':'artifact_bundle','lkg_receipt':receipt},state='running')
    store.finish_firmware('restore','ok',{'receipt':receipt,'device_id':'dev','principal':'bob',
        'kind':'artifact_bundle','baseline':'flash'},lambda:True)
    policy=retention_evidence(store,'p','bob','epoch')
    assert policy['complete']
    assert 'current_lkg_evidence' in policy['protected'][source]
    store.db.close()
