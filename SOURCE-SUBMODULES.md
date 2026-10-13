# Fixed Git submodules in source snapshots

Add the non-secret repository allowlist to the existing project registration:

```json
"source_repository": {
  "github_repository": "dj-oyu/cardputer-adv-pocketjs",
  "cache_root": "C:\\brksrc",
  "private_acl_confirmed": true,
  "submodule_repositories": ["lieff/minimp3"]
}
```

The firmware PR uses `third_party/minimp3`, the HTTPS URL
`https://github.com/lieff/minimp3.git`, and Gitlink commit
`ea99364f61c14656440e8d77e9c233ccf3124633`. The parent full commit remains the
only MCP input. The importer reads `.gitmodules` as data, confirms each path
against the parent's mode-160000 Gitlinks, and fetches only those full commits
from allowlisted public GitHub repositories. It does not run Git, shell,
`.gitmodules` update commands, hooks, or branch tracking. Relative/SSH/foreign
URLs, credentials, redirects, branch/update/extra configuration fields,
overlapping/case-alias paths, missing/mismatched links and cycles are rejected.

Each archive's regular blobs and Gitlinks reconstruct its exact Git tree SHA-1.
Each repository also records its commit, tree, archive SHA-256 and own-file
receipt SHA-256. Expanded submodule files participate in the immutable snapshot's
full source SHA-256. Nested Gitlinks have a maximum depth of 3 and a maximum total
of 16 modules. All repositories share the existing 64 MiB download, 256 MiB
expanded, 100,000 file, 200,000 ZIP entry and 180 second operation limits; cache,
Windows path, private ACL, principal, lease and cancellation checks still apply.
Large/truncated Git tree metadata fails closed. There is no automatic cleanup.

New snapshots containing submodules use receipt schema 2. The receipt and source
summary contain `submodules` records with `path`, `repository`, `commit`,
`tree_sha1`, `archive_sha256`, and `source_sha256`. The approved build plan and
firmware manifest preserve these records and use
`dependency_policy=gitlinks_fixed_commits_no_fetch`. Build configuration keeps
`IDF_COMPONENT_MANAGER=0`; submodule initialization never occurs during build.
Source tampering is rechecked before and after build. No extra flash authority
is granted by source retrieval or build.

Existing schema-1 receipts remain byte-for-byte unchanged and usable when an
allowlist is added to the same registration. Their original root registration,
principal, successful fetch job and file hashes remain required. Schema-2
receipts bind the full allowlist; changing it requires another source fetch.

Firmware still has separate BMI270 and libopus `.cache` requirements listed in
its `docs/platform/clean-checkout-dependencies.md`. Importing minimp3 does not
populate them and does not make the whole firmware clean-buildable. They need a
separate explicit dependency decision; no implicit SDK fetch is allowed.

Deployment acceptance: refresh the running server/client metadata using the
existing host procedure, use the reviewed firmware full commit, explicitly
review/approve source fetch, confirm the expected minimp3 record, then prepare
and approve only the intended ESP-IDF 6.0.1 build. Local Codex is not required by
the runtime. No flash or device test is part of this change.
