# Linux x64 distribution

The `Linux executable and tarball` Actions workflow builds one console executable,
`cardputer`. Python is included; a Python installation is not needed to run it.
The tarball is built on Ubuntu 22.04 and links the system C library dynamically,
so it needs glibc 2.35 or newer. `gui` additionally needs the system Tk library
(for example the `python3-tk` package) to be installed.

Download `cardputer-v<version>-linux-x64.tar.gz` and its `.sha256` from a Release
(or a workflow run), verify and extract it into a writable application folder:

```sh
sha256sum -c cardputer-v<version>-linux-x64.tar.gz.sha256
mkdir cardputer && tar -xzf cardputer-v<version>-linux-x64.tar.gz -C cardputer
cd cardputer
sha256sum -c SHA256SUMS.txt
```

The tarball contains the executable, this guide, LICENSE, NOTICE, a mock-only
example, `build-runtime.json`, `manifest.json` and `SHA256SUMS.txt`. The manifest
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

This example binds to loopback with the synthetic mock backend. Stop its owned
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
