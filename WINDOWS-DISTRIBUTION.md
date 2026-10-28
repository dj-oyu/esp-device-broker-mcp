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
These artifacts are unsigned. This workflow creates no tag or Release itself;
pushing a `v*` tag runs the `Release` workflow, which calls it and publishes the
ZIP together with the Linux tarball.

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

Registered Windows firmware builds use IDF's `--no-hints` direct subprocess
mode. The broker already captures both output streams; avoiding IDF's nested
asyncio hints collector leaves one owner for output collection and teardown.
Compiler output, exit codes, cancellation and authorization checks remain active.
A build-authority callback that cannot answer within its existing five-second
bound reports `build_authority_check_timeout`, retaining completed subprocess
exit codes without exposing exception text. An interrupted subprocess has no
invented exit code. A failed or cancelled build never publishes a firmware receipt.
Serial actions retain explicit startup opt-in, authentication, owned leases and
separate immutable-plan execution requests. Product approval flows are retired.
On upgrade, legacy pending/unstarted jobs are cancelled without replay; uncertain
running work remains recorded as diagnostic history. History alone does not block
a new explicitly requested job; current resource uncertainty still blocks. Preserve
the existing state directory.
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

### English status and emoji rendering acceptance

The live view uses short English labels (`✅ Succeeded`, `❌ Failed`,
`⏳ Running`) and an independent Review column (`Not reviewed` / `Reviewed`).
Machine state values, IDs, raw logs, codes and durable history are unchanged.
No specific emoji color font is forced: Tk/Windows chooses glyph fallback, so
color emoji support is not assumed. Status/review columns reserve measured text
width and retain horizontal scrolling; failure/unknown row colors remain.

On the exact packaged artifact, run the existing native GUI test group, then
inspect Jobs and Details at normal and 168 DPI with system/high-contrast themes.
Select succeeded, failed, running, unknown and both review states. Check for
missing-glyph boxes, clipped emoji/text, row-height clipping, selection visibility
and correct independent review labels. Record Windows/Tk version and the actual
font/fallback appearance, and capture screenshots. Metric assertions cannot prove
glyph appearance or color support. This cloud-only change does not deploy or
claim that native Windows rendering has been accepted.


## Standard key registry

Authenticated broker startup automatically reads `%LOCALAPPDATA%\DeviceBroker\keys.sqlite3`
(Unix: `$XDG_STATE_HOME/DeviceBroker/keys.sqlite3`, otherwise
`~/.local/state/DeviceBroker/keys.sqlite3`). `cardputer.exe keys` uses the same default.
It does not depend on cwd or an executable version directory. No registry flag,
Downloads helper, credential regeneration or broker restart is needed for committed
key additions/revocations. Existing GUI local tokens, stdio's explicit private
`--env-file`, Tunnel credentials and scope/principal isolation remain unchanged.
No key is issued at startup; no existing registry is moved or merged. See [KEYS.md](KEYS.md)
for failure behavior and explicit compatibility overrides.
