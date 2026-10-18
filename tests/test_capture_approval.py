"""Retired approval entry points cannot create authority or invoke prompts."""

import pytest

from cardputer_mcp.operator import approve_plan
from cardputer_mcp.store import Store


def test_offline_approval_is_retired_without_prompt_or_database_changes(tmp_path):
    store = Store(tmp_path / "history.sqlite3")
    try:
        before = "\n".join(store.db.iterdump())
        with pytest.raises(ValueError, match="^approval_interface_retired$"):
            approve_plan(tmp_path, "a" * 64, lambda _: pytest.fail("must not prompt"))
        assert "\n".join(store.db.iterdump()) == before
        with pytest.raises(ValueError, match="^approval_interface_retired$"):
            store.approve("a" * 64)
        assert store.db.execute("SELECT count(*) FROM approvals").fetchone()[0] == 0
    finally:
        store.close()
