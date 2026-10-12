"""Independent adversarial key review; synthetic keys and fake terminals only."""

import io
import itertools
import os
from pathlib import Path

import pytest

from cardputer_mcp.key_operator import KeyOperatorError, run
from cardputer_mcp.key_registry import KeyRegistry, KeyRegistryError
from cardputer_mcp.models import Origin


@pytest.mark.skipif(os.name != "posix", reason="POSIX ownership checks")
@pytest.mark.parametrize("mode", [0o755, 0o1777])
def test_foreign_owned_ancestor_cannot_be_trusted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: int
) -> None:
    """An ancestor's owner may rename children, even if those children are private."""
    ancestor = tmp_path / "foreign-owned"
    ancestor.mkdir(mode=mode)
    ancestor.chmod(mode)
    private = ancestor / "private"
    private.mkdir(mode=0o700)
    original_lstat = Path.lstat
    foreign_uid = max(os.geteuid() + 1, 1)

    def foreign_lstat(path: Path) -> os.stat_result:
        info = original_lstat(path)
        if path == ancestor:
            fields = list(info)
            fields[4] = foreign_uid
            return os.stat_result(fields)
        return info

    monkeypatch.setattr(Path, "lstat", foreign_lstat)
    with pytest.raises(KeyRegistryError, match="^unsafe_key_registry_permissions$"):
        KeyRegistry(private / "keys.sqlite3")
    assert not (private / "keys.sqlite3").exists()


@pytest.mark.parametrize("command", ["issue", "rotate"])
@pytest.mark.parametrize("interruption", [KeyboardInterrupt, SystemExit])
def test_interrupted_one_time_display_revokes_new_key(
    tmp_path: Path, command: str, interruption: type[BaseException]
) -> None:
    """Ctrl-C during disclosure must run the same compensation as a failed write."""

    class Terminal(io.StringIO):
        def isatty(self) -> bool:
            return True

    class InterruptedTerminal(Terminal):
        token_writes = 0

        def write(self, value: str) -> int:
            if value.startswith("API key (shown once): "):
                self.token_writes += 1
                super().write(value[:30])
                raise interruption
            return super().write(value)

    counter = itertools.count(1)
    path = tmp_path / "keys.sqlite3"

    def factory(path: Path, *, create: bool = True) -> KeyRegistry:
        return KeyRegistry(
            path,
            clock=lambda: 1_700_000_000.0,
            random_bytes=lambda size: next(counter).to_bytes(size, "big"),
            create=create,
        )

    old = None
    if command == "rotate":
        registry = factory(path)
        old = registry.issue("review", Origin.REMOTE)
        registry.close()
    output = InterruptedTerminal()
    confirmation = "ISSUE" if old is None else "ROTATE " + old.metadata.key_id
    failure: BaseException | None = None
    try:
        run(
            command,  # type: ignore[arg-type]
            path,
            label="review",
            key_id=None if old is None else old.metadata.key_id,
            confirm=lambda _: confirmation,
            input_stream=Terminal(),
            output_stream=output,
            registry_factory=factory,
        )
    except (KeyOperatorError, KeyboardInterrupt, SystemExit) as error:
        failure = error
    # Preserve operator cancellation, but only after compensating revocation.
    assert isinstance(failure, interruption)
    assert output.token_writes == 1
    registry = factory(path, create=False)
    try:
        assert all(key.revoked_at is not None for key in registry.list_keys())
    finally:
        registry.close()
