"""Bind a redacted export to an independently retained successful flash plan.

This is an offline consistency check. Hashes do not authenticate an entirely
rewritten archive or reveal the private manifest/cache preimage.
"""
import hashlib
import json
import re

ABSENT = object()
CONTRACT = 'broker-build-manifest-export-v1'
MAX_CACHE_BYTES = 128 * 1024
EXPORT_FIELDS = {'schema_version', 'contract', 'original_manifest_sha256', 'manifest'}


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def sha256(value):
    require(type(value) is str and re.fullmatch(r'[a-f0-9]{64}', value) is not None,
            'manifest_export_digest_type')
    return value


def export_digest(value):
    """Match broker canonical JSON; reject non-JSON NaN/Infinity explicitly."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=True, allow_nan=False).encode('utf-8')).hexdigest()


def read_export_response(value):
    """Accept an owner read_build_logs response; log metadata is not authority."""
    require(type(value) is dict and 'manifest_export' in value and
            'manifest_export_sha256' in value, 'manifest_export_response')
    wrapper = value['manifest_export']
    require(type(wrapper) is dict and type(wrapper.get('manifest')) is dict,
            'manifest_export_schema')
    if 'manifest' in value:
        require(export_digest(value['manifest']) == export_digest(wrapper['manifest']),
                'manifest_export_public_copy_mismatch')
    return wrapper['manifest'], wrapper, value['manifest_export_sha256']


def bind_export(manifest, proof, flash_plan, flash_job, *, wrapper, supplied_digest,
                expected_flash_plan_hash, expected_detail, expected_dma, expected_cache_bytes=None):
    require(type(wrapper) is dict and set(wrapper) == EXPORT_FIELDS and
            type(wrapper['schema_version']) is int and wrapper['schema_version'] == 1 and
            type(wrapper['contract']) is str and wrapper['contract'] == CONTRACT and
            type(wrapper['manifest']) is dict, 'manifest_export_schema')
    require(type(manifest) is dict and export_digest(wrapper['manifest']) == export_digest(manifest),
            'manifest_export_public_copy_mismatch')
    original = sha256(wrapper['original_manifest_sha256'])
    commitment = sha256(supplied_digest)
    require(export_digest(wrapper) == commitment == sha256(flash_plan.get('manifest_export_sha256')),
            'manifest_export_commitment_mismatch')
    require(original == sha256(flash_plan.get('manifest_sha256')), 'manifest_export_original_mismatch')
    # The expected hash comes from a separately retained owner-authorized durable
    # job record. Never infer this trust anchor from the mutable export itself.
    anchor = sha256(expected_flash_plan_hash)
    require(flash_job.get('state') == 'succeeded' and flash_job.get('id') == flash_plan.get('job_id') and
            anchor == sha256(flash_job.get('plan_hash')) == export_digest(flash_plan),
            'manifest_export_successful_plan_anchor')
    envelope = manifest.get('diagnostic_modeproof')
    require(type(envelope) is dict, 'manifest_export_proof_schema')
    required = {'requested_mode', 'proof', 'proof_sha256'}
    if expected_dma is not None:
        required |= {'requested_dma_mode', 'cache_evidence'}
    require(set(envelope) in (required, required | {'cache_evidence'}), 'manifest_export_proof_schema')
    require(export_digest(envelope['proof']) == sha256(envelope['proof_sha256']) == export_digest(proof) and
            envelope['requested_mode'] == expected_detail, 'manifest_export_proof_mismatch')
    if expected_dma is not None:
        require(envelope['requested_dma_mode'] == expected_dma, 'manifest_export_dma_mismatch')
    if 'cache_evidence' in envelope:
        cache = envelope['cache_evidence']
        require(type(cache) is dict and set(cache) == {'retained_private', 'bytes', 'sha256'} and
                cache['retained_private'] is True and type(cache['bytes']) is int and
                0 < cache['bytes'] <= MAX_CACHE_BYTES, 'manifest_export_cache_schema')
        cache_hash = sha256(cache['sha256'])
        diagnostic = proof['diagnostic'] if expected_dma is not None else proof
        require(type(diagnostic) is dict and
                all(type(diagnostic.get(key)) is dict for key in ('build_mode', 'build_measure')),
                'manifest_export_local_proof_schema')
        hashes = [diagnostic.get(key, {}).get('cmake_cache_sha256')
                  for key in ('build_mode', 'build_measure')]
        if expected_dma is not None:
            hashes.append(proof['dma_overlap']['cmake_cache_sha256'])
        require(all(value == cache_hash for value in hashes), 'manifest_export_local_cache_mismatch')
        if expected_cache_bytes is not None:
            require(type(expected_cache_bytes) is int and expected_cache_bytes == cache['bytes'],
                    'manifest_export_local_cache_size')
    return original, dict(contract=CONTRACT, manifest_export_sha256=commitment,
                          original_manifest_sha256=original, original_manifest_recomputed=False,
                          expected_flash_plan_hash=anchor,
                          scope='offline consistency against owner-retained successful plan commitment')
