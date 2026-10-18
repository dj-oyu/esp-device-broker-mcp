# Windows x64 distribution

The `Windows executable and ZIP` Actions workflow builds one console executable,
`cardputer.exe`. Python is included; a Python installation is not needed to run
it. The console is required for CLI output and the MCP stdio adapter. `gui` opens
the existing native operator window and connects to a separately running broker.

Download the verified distribution artifact from a successful workflow run and
extract `cardputer-v<version>-windows-x64.zip` into a writable application folder.
The ZIP contains the executable, this guide, LICENSE, NOTICE, a mock-only example,
`build-runtime.json`, `manifest.json` and `SHA256SUMS.txt`. The outer ZIP checksum
is saved alongside it. The manifest records the exact source commit/ref and
locked build inputs. The bounded build-runtime receipt comes from the actual
PyInstaller interpreter, binds the EXE/source/lock/build-tool inputs, and records
its Python and uv versions. Verification also compares Python's major/minor ABI
to the embedded PyInstaller cookie; it never substitutes the verifier's runtime.
These artifacts are unsigned. No tag or GitHub Release is created by this workflow.

```powershell
.\cardputer.exe --help
.\cardputer.exe --version
.\cardputer.exe serve --config .\examples\mock-safe.toml
```

In another console in the same folder, read the mock broker:

```powershell
.\cardputer.exe status
.\cardputer.exe logs
```

This example binds to loopback with the synthetic mock backend. Stop its owned
broker console with Ctrl+C when finished. It does not open USB or connect Tunnel.
For authenticated mock use, select `--auth-source environment` and
`serve --require-auth`, using the existing process-only credential procedure in
[START-WINDOWS.md](https://github.com/dj-oyu/esp-device-broker-mcp/blob/main/START-WINDOWS.md).
The GUI requires local environment authentication. `stdio --env-file` requires
an absolute private file outside the application folder and Git checkouts, as
described in [STDIO-CLIENTS.md](https://github.com/dj-oyu/esp-device-broker-mcp/blob/main/STDIO-CLIENTS.md).
Private config, state and credentials are supplied by the operator and are never
included in the distribution. Relative `state_dir` remains relative to process cwd.

The package includes neither ESP-IDF nor a toolchain, firmware, USB driver or
native Tunnel client. ESP-IDF remains external and pinned to **6.0.1**; this
packaging change does not add build/flash capabilities or migrate to 6.1.
Serial actions retain explicit startup opt-in, authentication, owned leases and
separate immutable-plan execution requests. Product approval flows are retired.
On upgrade, legacy pending/unstarted jobs are cancelled without replay; uncertain
running work remains quarantined. Preserve the existing state directory.
The existing PowerShell source launchers still target the source checkout's venv;
use `cardputer.exe` directly for this distribution.

## Maintainer build and validation

The workflow runs on `windows-2022` x64 with Python 3.13.16 and uv 0.12.19.
The spec refuses a dirty tracked source tree and writes `cardputer.exe.build.json`
after the build. The archiver requires that receipt beside the EXE; it cannot
create provenance by inspecting its own Python version or re-label another build.
Application dependencies use the checked-in `uv.lock` with `--frozen --no-dev`.
The project is then installed without dependency resolution or build isolation,
using the explicitly pinned `uv-build==0.12.19` backend.
The complete packaging tool set is version-pinned separately in
`packaging/requirements-windows-build.txt`, installed with `--no-deps` and checked
with `uv pip check`. Action revisions are pinned to full commit SHAs.

Pushes of `v*` tags, packaging-related pull requests, and manual runs are accepted.
A tag must equal `v` followed by the project version (for example `v0.12.0`);
the lockfile, broker wire version and GUI version must also agree. A mismatch fails
before dependency installation or artifact upload. PR/manual artifacts are test
builds of their recorded ref/commit and do not claim to be tagged releases.

The runner tests packaging rules, builds with PyInstaller, verifies the exact ZIP
member allowlist/checksums, and runs the extracted EXE from a separate temporary cwd.
It checks CLI help/version, fixed Tunnel supervisor dispatch, an authenticated
mock broker/client HTTP round-trip, shared log reads and anonymous denial. Only
temporary dummy credentials are used; no device, real Tunnel or ESP-IDF is accessed.
The verified EXE/ZIP/checksum artifact is retained for 30 days. Build diagnostics
are retained for seven days. The workflow has read-only repository permissions
and never uploads a failed distribution.

PyInstaller builds Windows executables on Windows; this cloud Linux workspace
cannot validate a Windows binary locally. See the workflow run for native evidence.
References: [PyInstaller usage](https://pyinstaller.org/en/v6.16.0/usage.html),
[PyInstaller release metadata](https://pypi.org/project/pyinstaller/6.16.0/).
