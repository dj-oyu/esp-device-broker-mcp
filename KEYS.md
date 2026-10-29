# Local Cardputer key management

This mechanism is separate from the OpenAI Tunnel API key. It is not an
MCP issuance endpoint. Existing process-only local/remote tokens keep working.
No persistent user key is created merely by installing, starting or updating the broker.

## Model

- CSPRNG 256-bit secrets, public random key ID, SHA-256 verifier only in SQLite
- Default remote origin, read-only scope, one-hour expiry; maximum 30 days
- Explicit `--control` enables device session/plan/execute requests within the
  registered capabilities. The authenticated client explicitly executes each
  immutable plan without a separate local approval. Lease ownership, current
  scopes and needs_review barriers still apply
- A local key without `--control` (`--origin local_cli`) may read the operator
  views: `GET /operator/state`, `/operator/timeline`, `/operator/events` and
  `POST /operator/job-logs` (app-session logs included). Its timeline
  offers no cancel/stop. Every other operator route still needs local control;
  remote keys never reach `/operator/*`. This is for a read-only viewer such as
  a web UI
- Each key has a distinct security principal. A shared key is one principal,
  even on several terminals; it is not physical-device identity
- Every new HTTP/tool request checks current revocation/expiry/scope. Revocation
  does not terminate running hardware or transfer unresolved device ownership
- Rotation creates a new principal and atomically revokes the old key. It does
  not transfer an old device lease; release idle ownership first or wait safely.
  A running job of the old key stops being authorized at rotation, so rotate
  between jobs
- Rotation records lineage: the replacement continues the old key's owner
  identity, so it can name builds and backups made under any earlier key of the
  same rotation chain as inputs to a new flash/restore. Plans and stored records
  keep their original principal. Only `rotate` links keys: a newly issued key,
  even with the same label, inherits nothing. If a key may be compromised,
  `revoke` it and `issue` a new key instead of rotating, so nothing it produced
  can be reused. An expired or revoked key cannot be rotated; rotate before
  expiry to keep access to its builds/backups as inputs. Reading redacted job
  output needs no lineage (any read-scoped key can read any job)
- Bounded registry/audit. No secret in list/audit/error output

Use an existing directory private to your OS account. Windows ACL privacy is an
operator prerequisite, not something inferred from chmod. Same-user processes
and administrators are trusted. Keep the verifier DB out of public repositories;
its integrity controls authorization. Do not copy private configs/state into an update ZIP.

## Explicit local issuance

In an interactive terminal in the project directory (not a pipe, transcript or recording):

```powershell
.\cardputer.exe keys issue --label 'remote-reader'
```

Review the stated origin, scope and lifetime and type the exact confirmation.
The token is shown once. Terminal scrollback/recording may retain it; protect it.
Without `--write-env` (below) the program does not write the secret to a file,
environment, clipboard or command line. Failed or interrupted token display
revokes the newly issued key.
Only confirmed default-path ISSUE may create the private registry directory. Startup never creates a key or database. Explicit override paths still require an existing private parent.

Use `--control` only when intentionally granting mutation capability. `--ttl-seconds`
sets the requested lifetime. `--origin local_cli` is a separate explicit origin choice;
never give a local-operator key to a remote connector.

## List, verify, revoke, rotate

```powershell
.\cardputer.exe keys list
.\cardputer.exe keys verify
.\cardputer.exe keys revoke --key-id cpk_REPLACE_WITH_PUBLIC_ID
.\cardputer.exe keys rotate --key-id cpk_REPLACE_WITH_PUBLIC_ID
```

`list` prints metadata only. Each entry keeps its numeric `issued_at`/`expires_at`/
`revoked_at` and adds a readable `remaining` (`expires in 12d 3h`, `expired` or
`revoked`). Active keys granted more than 3 days that expire within 3 days are also
named on stderr as a reminder to rotate them in time; short-lived keys never warn.

Verification takes hidden interactive input; no key argument is accepted. Revoke
and rotate require an exact local confirmation. Rotate preserves origin/scope/label.
Without `--ttl-seconds` the replacement gets the current key's originally granted
lifetime (its issued-to-expiry span, at most 30 days), counted from the rotation; an
explicit `--ttl-seconds` wins. The confirmation prompt shows the resulting lifetime.
Never paste the full token into shell arguments.

## Writing the key into an existing stdio env file

`issue` and `rotate` accept `--write-env <absolute path>` to put the new key
straight into the private env file used by the stdio adapter
([STDIO-CLIENTS.md](STDIO-CLIENTS.md)) instead of displaying it:

```powershell
.\cardputer.exe keys rotate --key-id cpk_REPLACE_WITH_PUBLIC_ID --write-env 'C:\Users\YOUR_NAME\AppData\Local\DeviceBroker\local-client.env'
```

- Only when explicitly asked. There is no default path, and the same interactive
  exact confirmation is required. On success the token is not displayed.
- Only into a file that already exists and passes the adapter's own reader checks
  (absolute path outside the adapter and Git checkouts; no link/reparse point or
  extra hard link; at most 8 KiB; strict grammar; on POSIX owned by you with mode
  0400/0600). The current `CARDPUTER_CLIENT_KEY` value may be a placeholder; a file
  without that line gets one appended. `rotate` refuses a file holding a different
  valid key. A Windows file marked read-only is refused. The file is never created.
- Only the `CARDPUTER_CLIENT_KEY` value changes. Every other line and comment, the
  `CARDPUTER_BROKER_URL` value, the BOM and the newline style are kept byte for byte.
- The new content goes to a temporary file in the same folder, is flushed, and then
  swapped in after re-checking that the target is still the same unlinked file with
  unchanged content. Windows uses `ReplaceFileW`, which keeps the existing file's
  ACL and attributes (the original is moved to a hidden `.bak` beside it and removed
  after success; if the swap stops halfway it is left there and the command says
  so); POSIX keeps the original mode. Until the swap, the temporary
  file has the folder's default protection, so the folder must be private.
  ACLs are not changed. Afterwards the file is re-read with the adapter's reader.
- Every check, and creation of the empty temporary file, happens before the registry
  changes. If writing fails afterwards: `issue` revokes the new key; `rotate` (whose
  old key is already revoked) shows the replacement once on the terminal instead and
  says the file was not updated, so you can save it yourself. If that display also
  fails, the new key is revoked: run `keys issue`. An interrupt during the write
  revokes the new key. A stdio adapter reads the file only when it starts, so
  restart running clients to pick up the new key (after `rotate` the old key is
  already rejected).

## Automatic verification on a broker

The single default is `%LOCALAPPDATA%\DeviceBroker\keys.sqlite3` on Windows;
on POSIX it is `$XDG_STATE_HOME/DeviceBroker/keys.sqlite3`, falling back to
`~/.local/state/DeviceBroker/keys.sqlite3`. It is independent of cwd, executable
version and installation folder. The CLI and authenticated broker use this same
path. Existing DeviceBroker registries stay in place; no migration, path search,
implicit merge or permission repair is performed. An old nonstandard registry
can be explicitly selected with `serve --key-registry` and `keys --registry`.

Use the installed executable or existing installed launcher, not an old Downloads
script. With the launcher's existing process-only local/remote tokens:

```powershell
.\cardputer.exe --auth-source environment serve --config '<private-config>'
```

No `--key-registry` flag is needed. Do not regenerate existing launcher credentials
just to enable the registry. Authentication remains required; GUI uses its existing
local token and Tunnel retains its existing credential source. Every request reads
the current registry, so committed additions/revocations take effect without broker
restart. Missing stores deny issued keys while existing process tokens still work;
malformed/unsafe existing default stores reject startup. Runtime failures deny issued
keys without caching old grants. In-progress SQLite transactions expose committed
state only; busy/incomplete/corrupt stores never authorize a fallback principal.
Default and explicit registries use the same existing OS-user/private-store trust
boundary and KeyRegistry path checks. Windows ACL privacy remains an operator
prerequisite; the broker does not scan or rewrite Windows ACLs. Existing link,
reparse, file-size and POSIX ownership/mode guards remain enforced.
The Tunnel launcher can request an issued client key privately; see
[TUNNEL-WINDOWS.md](TUNNEL-WINDOWS.md).
For custom local clients, `--auth-source key-environment` reads CARDPUTER_CLIENT_KEY
from that process environment. Do not use persistent user/system environment settings.

This implementation and synthetic tests do not create or configure actual user
credentials. Actual issuance and external account setup are local operator actions.
