# Fixed Git submodules in source snapshots

Optionally list the GitHub repositories you expect in the project registration:

```json
"source_repository": {
  "github_repository": "dj-oyu/cardputer-adv-pocketjs",
  "cache_root": "C:\\brksrc",
  "submodule_repositories": ["lieff/minimp3"]
}
```

The list is advisory. The parent full commit remains the only MCP input. The
importer reads `.gitmodules` as data, confirms each path against the parent's
mode-160000 Gitlinks, and fetches exactly those full commits from public GitHub. It
does not run Git, shell, `.gitmodules` update commands, hooks, or branch tracking.

What the fetch result reports in `notices` instead of refusing:

- `submodule_repository_unregistered`: a Gitlink to a GitHub repository outside
  `submodule_repositories`. It is fetched and verified like any other.
- `submodule_keys_ignored`: `branch`, `shallow`, `ignore` or `fetchRecurseSubmodules`,
  which only steer a local `git submodule` and change nothing here.
- `submodule_not_fetched_update_none`: `update = none`. As in a plain recursive clone,
  the Gitlink stays in the tree and its directory stays empty.

Still refused (`source_gitmodules_rejected` and related codes): non-GitHub, relative,
SSH or credentialed URLs, redirects, `update = !command` and other unknown `update`
values, unknown keys, overlapping/case-alias/unsafe paths, missing/mismatched links
and cycles. Notices describe one fetch and are not stored in the receipt.

Editing `submodule_repositories` orphans no existing receipt (see "Registration
binding is content, not location" in SOURCE-SNAPSHOTS.md).

Each archive's regular blobs and Gitlinks reconstruct its exact Git tree SHA-1.
Each repository also records its commit, tree, archive SHA-256 and own-file
receipt SHA-256. Expanded submodule files participate in the immutable snapshot's
full source SHA-256. Nested Gitlinks have a maximum depth of 3 and a maximum total
of 16 modules. All repositories share the existing 64 MiB download, 256 MiB
expanded, 100,000 file, 200,000 ZIP entry and 180 second operation limits; cache,
Windows path, private-directory, principal, lease and cancellation checks still apply.
Large/truncated Git tree metadata fails closed. There is no automatic cleanup.

New snapshots containing submodules use receipt schema 2. The receipt and source
summary contain `submodules` records with `path`, `repository`, `commit`,
`tree_sha1`, `archive_sha256`, and `source_sha256`. The immutable build plan and
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
prepare/execute source fetch, confirm the expected minimp3 record, then prepare
and explicitly execute only the intended ESP-IDF 6.0.1 build. Local Codex is not required by
the runtime. No flash or device test is part of this change.
