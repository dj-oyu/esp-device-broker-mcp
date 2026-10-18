"""Legacy approval cannot create authority or replay submitted work."""

import pytest
from test_device_observation_cli import write_plan

from cardputer_mcp.operator import approve_plan
from cardputer_mcp.store import Store


@pytest.mark.parametrize("state", [None, "awaiting_approval", "queued", "cancelled", "succeeded"])
def test_retired_cli_never_prompts_or_changes_jobs(tmp_path, state):
    digest, port = write_plan(tmp_path)
    store = Store(tmp_path / "history.sqlite3")
    try:
        if state is not None:
            store.add_job("synthetic-log-observation", digest)
            store.set_job("synthetic-log-observation", state)
        before = list(store.db.iterdump())
        prompts = []
        with pytest.raises(ValueError, match="^approval_interface_retired$"):
            approve_plan(tmp_path, digest, lambda prompt: prompts.append(prompt) or "APPROVE")
        assert not prompts and list(store.db.iterdump()) == before
        assert port.opens == 0 and not port.writes
    finally:
        store.close()
