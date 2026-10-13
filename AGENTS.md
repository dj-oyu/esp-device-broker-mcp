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
