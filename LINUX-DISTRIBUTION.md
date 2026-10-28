# Linux x64 distribution

The `Linux executable and tarball` Actions workflow builds one console executable,
`cardputer`. Python is included; a Python installation is not needed to run it.
The tarball is built on Ubuntu 22.04 and links the system C library dynamically,
so it needs glibc 2.35 or newer. The Linux build has no GUI: the `gui` command and
the native Tunnel launcher are Windows-only, and Tk is not bundled.

Download `cardputer-v<version>-linux-x64.tar.gz` and its `.sha256` from a Release
(or a workflow run), verify and extract it into a writable application folder:

```sh
sha256sum -c cardputer-v<version>-linux-x64.tar.gz.sha256
mkdir cardputer && tar -xzf cardputer-v<version>-linux-x64.tar.gz -C cardputer
cd cardputer
sha256sum -c SHA256SUMS.txt
```

The tarball contains the executable, this guide, the two start scripts, LICENSE,
NOTICE, a mock-only example, `build-runtime.json`, `manifest.json` and `SHA256SUMS.txt`. The manifest
records the exact source commit/ref and locked build inputs; the build-runtime
receipt comes from the actual PyInstaller interpreter and binds the executable,
source, lock and build-tool inputs. These artifacts are unsigned.

```sh
./cardputer --help
./cardputer --version
./cardputer serve --config ./examples/mock-safe.toml
```

In another terminal in the same folder, read the mock broker:

```sh
./cardputer status
./cardputer logs
```

## Start scripts

`start-cardputer.sh` starts one authenticated broker and `start-cardputer-stack.sh`
also starts the Claude gateway and the read-only web UI. They are the Linux
counterparts of `Start-Cardputer.ps1` and `Start-CardputerStack.ps1` and take the
same two required inputs, the executable and your private profile:

```sh
. ./start-cardputer.sh --exe ./cardputer --profile /path/to/device.local.toml
. ./start-cardputer-stack.sh --exe ./cardputer --profile /path/to/device.local.toml
```

**Source them** (`. ./script`, not `./script`): they create fresh process-only
tokens and a child process cannot put them into its parent's environment, so the
clients you start from the same prompt need this shell to hold them. The scripts
refuse to run any other way. They require bash.

- The broker runs as a background job of that shell and logs to
  `${XDG_STATE_HOME:-~/.local/state}/cardputer/broker.log` (`--log-dir` changes it);
  the gateway and web UI log next to it. Closing the terminal ends them.
- They refuse with `listener_exists` if the broker URL already accepts a connection,
  never read the profile, never stop an existing process and print the `kill`
  command for each process they started. A profile that does not listen on
  `127.0.0.1:8765` needs `--url`.
- Startup does not open USB. `--enable-serial` is passed by default and only
  permits the serial backend; `--no-serial` leaves it out.
- The helpers get no `CARDPUTER_*` or `OPENAI_*` variables and read only the key
  files named in the profile.

When finished, stop what you started and clear the tokens:

```sh
kill "$CARDPUTER_BROKER_PID"
unset CARDPUTER_LOCAL_TOKEN CARDPUTER_REMOTE_TOKEN
```

The mock example (`serve --config ./examples/mock-safe.toml`) binds to loopback with the synthetic mock backend. Stop its owned
broker with Ctrl+C when finished. It does not open USB or connect Tunnel. Private
config, state and credentials are supplied by the operator and are never included
in the distribution. Relative `state_dir` remains relative to process cwd.

The package includes neither ESP-IDF nor a toolchain, firmware, USB permission
rules or native Tunnel client. ESP-IDF remains external and pinned to **6.0.1**.
Serial access needs the user to be allowed to open the device node (commonly the
`dialout` group); that is a system setting this package does not change.

Only the packaging and a mock-backend smoke test are exercised on Linux in CI.
Registered firmware builds, flashing and real device behavior on Linux have not
been verified by this workflow.
