# Private input composition: development checkpoint

This checkpoint is a pure identity/validation helper, not a working private-input
feature. It is not connected to MCP, source receipts, configuration or build jobs.
It grants no delivery, execution, publication or flash authority.

The helper binds public source receipt and exact Git commit, private input receipt,
owner/project, registration and build recipe digests, destination and complete
composed file manifest. Public Git provenance stays distinct from added inputs.
Portable paths, duplicate/case aliases, file/directory collisions, invalid sizes,
digests and bounded combined inventories fail closed. Paths are intentionally a
conservative ASCII subset; broadening it needs Windows collision tests.

Remaining integration work, in order:

1. A locally registered input ID, fixed destination and permitted public commit;
   no caller-selected local path, URL or credential. Authorization to introduce
   actual private inputs remains separate from this code change.
2. No-follow bounded local ingestion into immutable storage, stable file-handle
   verification, atomic publication and durable successful owned import receipts.
3. Validate those receipts and registration before calling the helper. Its string
   arguments are NOT proof of authenticity or permission.
4. Dedicated composed build workspace, leaving public source cache untouched;
   rehash before/after execution, cancellation/restart/retention references and
   artifact provenance bound to the composed identity.
5. Tests for receipt forgery, race/TOCTOU, links/junctions, interrupted publication,
   owner isolation, cache lifecycle and integrated build/flash lineage.

The current build process is not an OS filesystem/network sandbox. Granting
private inputs to public build scripts requires trusting the exact code/profile
combination; a private directory alone cannot ensure confidentiality. Compiler
output may contain private content. Diagnostics and generated binaries must keep
the appropriate private audience, with no automatic public artifact upload.

Validation for this checkpoint: stdlib unittest synthetic contract tests only.
No actual private files, source copies, broker configuration, transfers or hardware
are used. This does not establish a private-enabled firmware build.
