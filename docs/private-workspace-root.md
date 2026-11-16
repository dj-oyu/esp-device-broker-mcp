# Protected scratch root adapter

The POSIX `private_workspace_root` context manager verifies each existing ancestor
through directory handles and no-follow opens. It rejects owners other than root
or the broker UID and any group/world write bit, including sticky shared roots.
It creates a new random mode-0700 child and verifies its owner and permissions.
It never changes permissions or ownership on an existing directory. Cleanup is
relative to the held container handle, after the consumer has stopped its children.

The container is trusted deployment configuration, not an MCP argument. This
adapter supports local POSIX permission semantics only; it cannot attest mount or
network-filesystem behavior. A dedicated broker identity remains a deployment
requirement: other processes with that same UID and privileged administrators are
not isolated. Windows remains explicitly unsupported pending a native ACL/reparse
adapter and tests. Rejected roots must not be silently chmodded or bypassed.

Synthetic tests cover ownership/mode rejection, symlink ancestry, cleanup,
unsupported platforms, and composition with the materializer. The fixed Python
build fixture now traverses receipt, planner, protected root, materializer, output
hash verification and cleanup. This is still not an ESP-IDF/broker firmware build.
Authenticated receipt resolution and registered build-profile job integration
remain the next runtime steps. No runtime config, real private data or device is
changed by this checkpoint.
