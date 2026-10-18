# Local Cardputer key management

This optional mechanism is separate from the OpenAI Tunnel API key. It is not an
MCP issuance endpoint. Existing process-only local/remote tokens keep working.
No persistent user key is created merely by installing, starting or updating the broker.

## Model

- CSPRNG 256-bit secrets, public random key ID, SHA-256 verifier only in SQLite
- Default remote origin, read-only scope, one-hour expiry; maximum 30 days
- Explicit `--control` enables device session/plan/execute requests within the
  registered capabilities. The authenticated client explicitly executes each
  immutable plan without a separate local approval. Lease ownership, current
  scopes and needs_review barriers still apply
- Each key has a distinct security principal. A shared key is one principal,
  even on several terminals; it is not physical-device identity
- Every new HTTP/tool request checks current revocation/expiry/scope. Revocation
  does not terminate running hardware or transfer unresolved device ownership
- Rotation creates a new principal and atomically revokes the old key. It does
  not transfer an old device lease; release idle ownership first or wait safely
- Bounded registry/audit. No secret in list/audit/error output

Use an existing directory private to your OS account. Windows ACL privacy is an
operator prerequisite, not something inferred from chmod. Same-user processes
and administrators are trusted. Keep the verifier DB out of public repositories;
its integrity controls authorization. Do not copy private configs/state into an update ZIP.

## Explicit local issuance

In an interactive terminal in the project directory (not a pipe, transcript or recording):

```powershell
uv run cardputer keys issue --registry '.state\keys.sqlite3' --label 'remote-reader'
```

Review the stated origin, scope and lifetime and type the exact confirmation.
The token is shown once. Terminal scrollback/recording may retain it; protect it.
The program does not write the secret to a file, environment, clipboard or command
line. Failed or interrupted token display revokes the newly issued key.
No parent directory is silently created. Existing broker setup normally provides `.state`.

Use `--control` only when intentionally granting mutation capability. `--ttl-seconds`
sets the requested lifetime. `--origin local_cli` is a separate explicit origin choice;
never give a local-operator key to a remote connector.

## List, verify, revoke, rotate

```powershell
uv run cardputer keys list --registry '.state\keys.sqlite3'
uv run cardputer keys verify --registry '.state\keys.sqlite3'
uv run cardputer keys revoke --registry '.state\keys.sqlite3' --key-id cpk_REPLACE_WITH_PUBLIC_ID
uv run cardputer keys rotate --registry '.state\keys.sqlite3' --key-id cpk_REPLACE_WITH_PUBLIC_ID
```

Verification takes hidden interactive input; no key argument is accepted. Revoke
and rotate require an exact local confirmation. Rotate preserves origin/scope/label
and uses the requested/default new lifetime. Never paste the full token into shell arguments.

## Enable verification on a broker

```powershell
& .\Start-Cardputer.ps1 -Mock -KeyRegistry '.state\keys.sqlite3'
```

The existing registry is explicitly selected; no implicit migration occurs.
The launcher still provides process-only local and remote credentials. The Tunnel
launcher can request an issued client key privately; see [TUNNEL-WINDOWS.md](TUNNEL-WINDOWS.md).
For custom local clients, `--auth-source key-environment` reads CARDPUTER_CLIENT_KEY
from that process environment. Do not use persistent user/system environment settings.

This implementation and synthetic tests do not create or configure actual user
credentials. Actual issuance and external account setup are local operator actions.
