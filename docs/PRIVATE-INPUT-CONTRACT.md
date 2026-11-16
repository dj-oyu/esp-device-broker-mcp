# Private input composition: development checkpoint

This checkpoint is a pure identity/validation helper, not a working private-input
feature. The identity helper is not connected to MCP, source receipts or build jobs.
It grants no delivery, execution, publication or flash authority.

The helper binds public source receipt and exact Git commit, private input receipt,
owner/project, registration and build recipe digests, destination and complete
composed file manifest. Public Git provenance stays distinct from added inputs.
Portable paths, duplicate/case aliases, file/directory collisions, invalid sizes,
digests and bounded combined inventories fail closed. Paths are intentionally a
conservative ASCII subset; broadening it needs Windows collision tests.

Remaining integration work, in order:

1. Local registration schema is implemented (see below): input ID, fixed destination
   and permitted public commit;
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

Validation covers synthetic identity and registration tests; no real imports.
No actual private files, source copies, broker configuration, transfers or hardware
are used. This does not establish a private-enabled firmware build.

## Local registration checkpoint

`BuildProject.private_inputs` now accepts bounded, locally configured input IDs.
Each declaration fixes an absolute local root, portable destination, exact public
commit, inventory hash, recipe hash and file/byte limits. Configuration loading
performs no input I/O. Private roots and declarations are omitted from normal
model serialization/repr. The registration identity binds every field and project
and input IDs; it is not an authorization token.

This is schema support only. Existing build jobs do not consume these declarations
and no importer or MCP method is enabled. Do not register actual private inputs
until owned durable receipts, no-follow ingestion and private build composition
are integrated and reviewed. No configuration or credentials are changed by this
checkpoint. Next: receipt-backed ingestion with synthetic race/interruption tests.

## Receipt store checkpoint

`PrivateInputStore` accepts already-read file bytes from a trusted ingestion
adapter and commits a complete manifest plus bytes in one SQLite transaction.
Receipts bind owner, project, input ID and the registration identity; resolution
rehashes stored bytes. Repeated imports verify existing content, cancellation
before commit rolls back both tables, and restart resolves committed receipts.
Transactions use explicit SQL BEGIN/COMMIT/ROLLBACK, including connections with
Python 3.12 `autocommit=True`; connection context-manager behavior is not assumed.
The caller supplies a dedicated broker-owned connection with no active transaction.
This storage component neither authenticates callers nor protects the database
path; the broker integration must supply those boundaries.

Synthetic tests cover restart, idempotence, owner/registration isolation, forged
receipts, changed bytes, invalid inventories, cancellation and active transactions.
The no-follow filesystem reader, platform-specific race tests, MCP route and
composed build consumer are still missing. No private data has been imported.
