# Private input reader: Windows design and acceptance tests

Status: design/model tests only. No Windows reader or MCP endpoint is enabled by
this checkpoint. Synthetic tests do not prove Win32 behavior.

## Consumer boundary

`PrivateInputStore.ingest(..., files=...)` accepts an iterable of `(portable_path,
bytes)`. The reader must enforce byte limits **before** allocating each payload.
The store separately checks the complete inventory digest and commits atomically.
Expected names, lengths and SHA-256 values must come from a manifest bound to the
trusted registration digest, not from untrusted directory discovery. Exact
inventory equality is required; extra directory files are not implicitly imported.

## Windows implementation contract

1. Resolve only the locally registered root. Reject UNC/network namespaces and
   device paths initially; support a documented local filesystem only. Never take
   a caller-selected path from MCP. Anchor at a verified volume/root handle, open
   each component without following reparses, reject *all* reparse tags, and keep
   handles alive. Root opening has the same ancestor race requirements as files.
2. For each portable manifest component, open relative to the held parent handle.
   `CreateFileW(full_path, FILE_FLAG_OPEN_REPARSE_POINT)` alone is insufficient:
   the flag prevents following the final reparse point, not a replaced ancestor.
   `lstat -> open` and `GetFinalPathNameByHandle` after a pathname open are also
   insufficient proof of containment. A native relative-handle backend (for
   example carefully reviewed `NtCreateFile` using `RootDirectory`) needs Windows
   tests. Do not silently fall back to pathname opening when unavailable.
3. Check file type/reparse attributes from the opened handle, not the prior path.
   Keep directory/file handles pinned, deny write/delete sharing for the capture
   where supported, and reject sharing violations. Read from that same handle.
   Reject multiple hardlinks for the initial supported mode. File-ID and volume
   identity checks are additional guards, not substitutes for relative opens.
4. Bound size from the manifest and registration before reading. Read at most
   expected size plus one sentinel byte; reject shrink/growth and digest mismatch.
   Any unsupported handle property fails closed. Do not enumerate arbitrary large
   directory trees or follow link targets to discover input content.
5. Verify cancellation between bounded reads. On failure discard staged bytes,
   close every handle, and return a stable redacted error (no absolute paths or
   underlying exception text). Successful reader completion is not authorization.
   Store receipt creation remains the atomic acceptance point.

Hashes guarantee accepted bytes match the expected inventory, not that bytes were
never read from an unapproved location. Thus inventory matching does not excuse
path containment. Same-user/admin malicious modifications, filesystem drivers,
pre-existing writable mappings and local ACL changes are not fully excluded by
sharing flags; explicit trust assumptions and native testing are required.

## Required native validation before integration

- NTFS junction/symlink at root, every ancestor and leaf, including swap between
  observation and open; no outside-file reads, not merely no receipt creation.
- Held ancestor rename/delete and final replacement, concurrent writers/mappings,
  hardlink, ADS/device/UNC names, case aliases, huge/sparse files and growth/shrink.
- Cancellation and exception at each open/read; no handle or receipt leak.
- Real Windows API behavior and error redaction under the packaged broker.

`tests/test_private_input_reader_model.py` demonstrates the race distinction with
an in-memory namespace. It is a design regression, not a filesystem adapter test.
