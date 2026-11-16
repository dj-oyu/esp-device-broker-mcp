"""Generated request dispatch tests; actual ingestion has separate native tests."""

from dataclasses import FrozenInstanceError

import pytest

from cardputer_mcp import private_startup_import as module
from cardputer_mcp.config import Settings
from cardputer_mcp.private_input_contract import InputFile
from cardputer_mcp.private_startup import private_startup_factories
from cardputer_mcp.private_startup_import import PrivateImportRequest, StartupImports


def request(**changes):
    return PrivateImportRequest(
        **(
            {
                "project_id": "fixture",
                "input_id": "asset",
                "source_receipt": "a" * 64,
                "principal": "synthetic-owner",
                "manifest": (InputFile("fixture.bin", 0, "b" * 64),),
            }
            | changes
        )
    )


def test_request_immutable_and_repr_hidden():
    item = request()
    assert "synthetic-owner" not in repr(item) and "fixture.bin" not in repr(item)
    with pytest.raises(FrozenInstanceError):
        item.principal = "replacement"


@pytest.mark.parametrize(
    "change",
    [
        {"manifest": []},
        {"source_receipt": "bad"},
        {"project_id": "../private"},
        {"build_variant": "bad/name"},
        {"manifest": ()},
    ],
)
def test_invalid_request_rejected(change):
    with pytest.raises(ValueError):
        request(**change)


def test_off_refuses_explicit_import_batch():
    batch = StartupImports((request(),), authorized=lambda _: True, cancelled=lambda: False)
    with pytest.raises(ValueError, match="requires_enabled"):
        private_startup_factories(Settings(), imports=batch)
    assert not batch._attempted and batch.receipts == ()


def test_exact_requests_authority_and_receipts(monkeypatch):
    item = request(build_variant="fixture")
    seen = []

    def ingest(builder, store, **kwargs):
        assert kwargs["authorized"]() is True and kwargs["cancelled"]() is False
        assert kwargs["variant"] == {"build_variant": "fixture"}
        assert kwargs["manifest"] is item.manifest
        seen.append(kwargs["principal"])
        return "c" * 64

    monkeypatch.setattr(module, "ingest_registered_input", ingest)
    batch = StartupImports((item,), authorized=lambda req: req is item, cancelled=lambda: False)
    batch.run(object(), object())
    assert seen == [item.principal] and batch.receipts == ("c" * 64,)
    assert "cccc" not in repr(batch)
    with pytest.raises(ValueError, match="already_attempted"):
        batch.run(object(), object())


@pytest.mark.parametrize("authorized,cancelled", [(False, False), (True, True)])
def test_denial_or_cancel_never_calls_ingest(monkeypatch, authorized, cancelled):
    monkeypatch.setattr(module, "ingest_registered_input", lambda *_a, **_k: pytest.fail("read"))
    batch = StartupImports(
        (request(),), authorized=lambda _: authorized, cancelled=lambda: cancelled
    )
    with pytest.raises(ValueError, match="import_failed"):
        batch.run(object(), object())
    assert batch.receipts == ()
    with pytest.raises(ValueError, match="already_attempted"):
        batch.run(object(), object())


def test_partial_batch_receipts_remain_private_and_error_redacted(monkeypatch):
    count = 0

    def ingest(*_args, **_kwargs):
        nonlocal count
        count += 1
        if count == 2:
            raise OSError("private-secret-path")
        return "c" * 64

    monkeypatch.setattr(module, "ingest_registered_input", ingest)
    batch = StartupImports(
        (request(), request()), authorized=lambda _: True, cancelled=lambda: False
    )
    with pytest.raises(ValueError, match="^private_startup_import_failed$"):
        batch.run(object(), object())
    assert batch.receipts == ("c" * 64,)


def test_callbacks_required_and_batch_bounded():
    with pytest.raises(ValueError, match="batch_invalid"):
        StartupImports((request(),), authorized=None, cancelled=lambda: False)
    with pytest.raises(ValueError, match="batch_invalid"):
        StartupImports((request(),) * 17, authorized=lambda _: True, cancelled=lambda: False)
    with pytest.raises(ValueError, match="batch_invalid"):
        private_startup_factories(Settings(), imports=object())


def test_authority_rechecked_inside_ingest(monkeypatch):
    allowed = True

    def ingest(*_args, **kwargs):
        nonlocal allowed
        assert kwargs["authorized"]() is True
        allowed = False
        assert kwargs["authorized"]() is False
        raise ValueError("revoked")

    monkeypatch.setattr(module, "ingest_registered_input", ingest)
    batch = StartupImports((request(),), authorized=lambda _: allowed, cancelled=lambda: False)
    with pytest.raises(ValueError, match="import_failed"):
        batch.run(object(), object())
    assert batch.receipts == ()


def test_startup_rechecks_settings_after_import(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from test_private_startup import prepared

    from cardputer_mcp import private_startup as startup
    from cardputer_mcp.config import BuildProject
    from cardputer_mcp.private_startup import PrivateStartup

    settings, scratch = prepared(tmp_path, monkeypatch)
    batch = StartupImports((), authorized=lambda _: True, cancelled=lambda: False)
    factories = private_startup_factories(settings, PrivateStartup(True, scratch), imports=batch)
    monkeypatch.setattr(startup, "_verify_container", lambda _: None)

    def mutate(*_args):
        settings.build_projects["later"] = BuildProject(
            project_dir=tmp_path / "source", trusted_profile=tmp_path / "profile.ps1"
        )

    monkeypatch.setattr(batch, "run", mutate)
    monkeypatch.setattr(startup, "NativePrivateProducer", lambda *_: pytest.fail("installed"))
    with pytest.raises(ValueError, match="settings_changed"):
        factories["private_runtime_session_factory"](
            SimpleNamespace(settings=settings), SimpleNamespace(store=object())
        )
