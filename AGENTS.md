# Repository working instructions

These instructions guide work; they do not grant authorization. Follow the user's
authorized scope and the execution environment's approval/security rules. Do not
infer permission for merges, new tags/Releases, hardware writes or relaxed
settings from this file.

## Workflow and evidence

- Develop in the cloud, submit changes through GitHub, verify the actual artifact
  on local Windows and hardware where relevant, fix findings, then merge validated
  PRs individually within the user's authorized scope.
- Keep independent tasks separate and run independent work in parallel when
  permitted. Deliver completed parts without coupling them to unrelated work.
- Distinguish environment preparation, implementation and validation. Set an early
  checkpoint; report the exact operation, evidence, blocker and next decision.
  Do not repeat the same startup/communication failure without new evidence.
- Treat shell network access, GitHub connector access and Actions execution as
  separate capabilities. Use authorized working routes; do not bypass restrictions.
- For DLL/execution failures, compare the agent environment with a normal terminal
  before blaming a damaged compiler. Use official approval flows; do not disable
  security controls or work around access denials.

## Runtime and data boundaries

- ESP-IDF is external and fixed at **6.0.1**. Do not automatically migrate/fall back
  to 6.1 or bundle an SDK/toolchain.
- Keep MCP side effects simple and explicit. Backup, flash and restore are
  separate approved operations. Leave verified new firmware installed; do not
  add implicit rollback, write retries or other automatically executed commands.
  Disclose existing reset/reopen behavior and retain cancellation/ownership checks.
- Retain exact verified firmware bytes by private content hash. LKG selection and
  successful job must commit atomically after write/readback/expected same-device
  boot/confirmed close/final authority. Failure/cancel/crash keeps the prior target.
  Restore binds a prepared receipt and remains explicit. Keep the initial full
  backup separate; protect referenced recovery objects from build retention.
  Do not imply NVS/data compatibility or add implicit data/partition migrations.
- Register projects and execution profiles locally. MCP must not accept arbitrary
  shell commands or unrestricted project/profile paths.
- Preserve approval, ownership, cancellation and redaction boundaries in build,
  shared-log and flash work. Do not release device ownership while owned processes
  or device work remain active.
- Broker startup/open state does not prove a physical device connection. Report
  observed device/protocol evidence separately; mock success is not hardware success.
- Never commit credentials, private configuration, personal identity/contact data,
  private absolute paths, generated caches/state or virtual environments. Keep
  private config external. Do not multiply working copies under OneDrive.
- Do not publish internal task identifiers or non-public operational details.

## Repository and checks

Application code is in `src/cardputer_mcp/`; tests are in `tests/`.
Windows packaging is in `packaging/` and
`.github/workflows/windows-package.yml`. Examples are templates, not private profiles.
Consult `README.md`, `VERIFICATION.md`, `WINDOWS-DISTRIBUTION.md` and `pyproject.toml`.

Use the checked-in lockfile and the relevant documented checks:

```sh
uv sync --frozen
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src
uv run pytest -q
python -m unittest discover -s tests -p test_windows_release.py -v
```

- Choose checks appropriate to the change. Document blocked checks instead of
  silently substituting dependencies or claiming a pass.
- Separate change-specific failures from the existing baseline using comparable
  source/dependencies/platforms. Native Windows mypy and `--platform linux` results
  are different evidence; report them separately.
- Verify the current PR head, Actions checkout/source SHA, artifact provenance and
  hashes. A PR artifact can record the tested merge SHA rather than the head SHA.
- Record what actually ran and what remains unverified. Do not label physical
  build/flash/device behavior successful based on packaging, mocks or broker readiness.

## MCP metadata checkpoints

At development checkpoints and after server updates/restarts/reconnections, compare
the actual server tools/list names, input schemas and annotations with the tools
callable by the parent cloud client. Server-only discovery is not acceptance.
The repository's stdio adapter currently advertises tools.listChanged=false; no
dynamic tool-list notification path is implemented here. That does not refresh
a host's imported metadata.

For a custom ChatGPT MCP connection (including Secure MCP Tunnel), the official
flow is: deploy/restart server, open its connection in ChatGPT Plugins, select
Refresh, verify changed metadata, then start a new conversation and retest.
https://developers.openai.com/plugins/deploy/connect-chatgpt#refresh-metadata
Do not claim the current conversation refreshed or that refresh is automatic.
Host Refresh is not exposed by the tools in this cloud task. Do not change
permissions, authentication, or re-register a connection as a workaround.

## Updating an existing Windows installation

- Locate the installed application before changing the source checkout. Check
  `%LOCALAPPDATA%/esp-device-broker-mcp` first, its `device.local.toml`, version
  directories and deployment receipts; then inspect listeners and executable
  paths. A checkout can be older than the running packaged application. Avoid
  recursive searches across the entire user profile, SDKs and dependency caches.
- Read `state_working_directory` from the existing deployment receipt. Relative
  `state_dir` resolves against process cwd, not the executable or config directory.
  Run a new version's executable from the existing state cwd unless a separate
  state migration is explicitly authorized. Retain previous versions.
- Verify the successful Actions run, PR head, artifact checksums and manifest.
  A PR artifact can contain a tested merge commit; compare its Git tree with the
  authorized merged code rather than requiring the source commit to equal the
  PR head. Do not guess documentation filenames; list tracked packaging files.
- For a config-only allowlist addition, back up the original bytes, validate the
  updated config using the deployed version's schema, and compare parsed values
  after removing just the requested change. Preserve unrelated settings, SDK
  registration, source cache, durable state and recovery firmware. Where useful,
  verify recovery/tool hashes and compare SQLite contents with a pre-start snapshot
  through read-only connections; startup may legitimately update recovery state,
  so report any difference rather than assuming byte-for-byte DB identity.
- If process creation fails with access denied before PowerShell executes, use
  the execution environment's approval/escalation flow. In the observed runner,
  changing the requested shell path still resolved to the same packaged pwsh;
  repeatedly swapping shell paths did not resolve the denial. Do not infer a
  broken Python/SDK installation from this failure or bypass access controls.
- Do not reuse source launchers blindly for a packaged installation: they can
  select the checkout's venv/config and generate new credentials. Inspect the
  existing launcher first. Pass fresh broker credentials only through child
  process environments, preserve the existing Tunnel metadata and credential
  source, and never print secrets. Use the documented broker OS environment
  allowlist and the narrower Tunnel child environment helper.
- Start background helpers hidden. Track the supervisor, broker bootloader,
  application child and Tunnel PIDs; the listener PID can differ from the launched
  PyInstaller PID. Establish owned-process cleanup on partial startup failure.
  Keep any source/helper paths used by a resident supervisor available for its
  lifetime; do not delete its staging directory as routine post-deploy cleanup.
- Check broker readiness with authenticated read-only MCP calls, then native
  Tunnel `/readyz`, then a call from the parent cloud connection. Tunnel health
  alone does not prove cloud routing. A cloud call before Tunnel startup can return
  `tunnel_client_not_seen`; establish readiness before diagnosing that as failure.
  Compare tool names, schemas and annotations under the metadata checkpoint above.
  A restart request does not authorize flash, restore, device reset or test jobs.
