"""Authority timeouts retain a fixed cause and completed subprocess statuses."""

from types import SimpleNamespace

import pytest

from cardputer_mcp import firmware_build


async def test_authority_callback_timeout_is_fixed_and_keeps_completed_status(monkeypatch):
    builder = firmware_build.FirmwareBuild(SimpleNamespace())
    builder.authority = lambda: True
    cancelled = []

    class Pending:
        def result(self, timeout):
            assert timeout == 5
            raise TimeoutError("token=synthetic-private-value")

        def cancel(self):
            cancelled.append(True)

    def submit(coroutine, loop):
        coroutine.close()
        return Pending()

    def steps(plan, stage, defer_publish, statuses):
        statuses.update(profile_export=0, build_diagnostic=0)
        builder._authority_monitor()
        raise AssertionError("timeout must prevent further work")

    monkeypatch.setattr(firmware_build.asyncio, "run_coroutine_threadsafe", submit)
    monkeypatch.setattr(builder, "_execute_steps", steps)
    with pytest.raises(firmware_build.FixedDiagnosticFailure) as caught:
        await builder.execute({}, lambda _: None, defer_publish=True)
    assert cancelled == [True]
    assert builder._authority_monitor is None
    assert caught.value.public_diagnostic() == {
        "code": "build_authority_check_timeout",
        "subprocess_exit_codes": {"profile_export": 0, "build_diagnostic": 0},
    }
    assert "synthetic-private" not in str(caught.value)
