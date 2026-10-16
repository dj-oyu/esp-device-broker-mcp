"""Windows local same-user pipe for one explicitly enabled operator CLI slot.

No default DACL, impersonation, remote pipe, request spool, or token file.
https://learn.microsoft.com/windows/win32/ipc/named-pipe-security-and-access-rights
"""

import json
import os
import re
import secrets
import time
from types import SimpleNamespace
from typing import Any

from .operator_launch import (
    MAX_REQUEST,
    MAX_RESPONSE,
    LaunchRequest,
    OperatorLaunchError,
    OperatorLaunchSlot,
    decode_message,
)

_ACCESS = 0x100000 | 0x1 | 0x2 | 0x80 | 0x100  # individual pipe rights, not GENERIC_WRITE
_PREFIX = "\\\\.\\pipe\\cardputer-operator-"


def _modules() -> Any:
    if os.name != "nt":
        raise OperatorLaunchError("operator_launch_windows_required")
    import pywintypes  # type: ignore[import-untyped]
    import win32api  # type: ignore[import-untyped]
    import win32file  # type: ignore[import-untyped]
    import win32pipe  # type: ignore[import-untyped]
    import win32security  # type: ignore[import-untyped]

    return SimpleNamespace(
        types=pywintypes, api=win32api, file=win32file, pipe=win32pipe, security=win32security
    )


def _sid_for_process(modules: Any, process: Any) -> str:
    token = modules.security.OpenProcessToken(process, 8)  # TOKEN_QUERY
    try:
        sid = modules.security.GetTokenInformation(token, modules.security.TokenUser)[0]
        return str(modules.security.ConvertSidToStringSid(sid))
    finally:
        token.Close()


def current_user_sid() -> str:
    modules = _modules()
    return _sid_for_process(modules, modules.api.GetCurrentProcess())


def _sid_for_pid(modules: Any, process_id: int) -> str:
    process = modules.api.OpenProcess(0x1000, False, process_id)
    try:
        return _sid_for_process(modules, process)
    finally:
        process.Close()


def _code(error: Exception) -> int:
    value = getattr(error, "winerror", error.args[0] if error.args else 0)
    return value if type(value) is int else 0


class WindowsOperatorLaunchPipe:
    """Poll from the supervisor loop, preferably via asyncio.to_thread.

    The pipe cannot enable or replenish a slot. The launcher must explicitly
    authorize its one-capture window; the first review may select its tuple.
    Publish only the nonsecret name.
    """

    def __init__(self, slot: OperatorLaunchSlot) -> None:
        self._modules = _modules()
        self.slot = slot
        if slot.owner_sid != current_user_sid() or slot.closed:
            raise OperatorLaunchError("operator_launch_invalid_owner")
        self.name = _PREFIX + secrets.token_hex(16)
        security = self._modules.security
        sid = security.ConvertStringSidToSid(slot.owner_sid)
        acl = security.ACL()
        acl.AddAccessAllowedAce(security.ACL_REVISION, _ACCESS, sid)
        descriptor = security.SECURITY_DESCRIPTOR()
        descriptor.SetSecurityDescriptorOwner(sid, False)
        descriptor.SetSecurityDescriptorDacl(True, acl, False)
        descriptor.SetSecurityDescriptorControl(0x1000, 0x1000)  # protected DACL
        attributes = self._modules.types.SECURITY_ATTRIBUTES()
        attributes.SECURITY_DESCRIPTOR = descriptor
        attributes.bInheritHandle = False
        self._handle = self._modules.pipe.CreateNamedPipe(
            self.name,
            3 | 0x80000,  # DUPLEX | FIRST_PIPE_INSTANCE
            4 | 2 | 1 | 8,  # MESSAGE | READMODE_MESSAGE | NOWAIT | REJECT_REMOTE_CLIENTS
            1,
            MAX_RESPONSE + 1024,
            MAX_REQUEST + 1,
            0,
            attributes,
        )
        self._connected = False
        self._responded = False
        self._connected_at = 0.0

    def __repr__(self) -> str:
        return "<WindowsOperatorLaunchPipe one scoped capture>"

    def _disconnect(self) -> None:
        self._modules.pipe.DisconnectNamedPipe(self._handle)
        self._connected = self._responded = False

    def poll(self) -> dict[str, Any] | None:
        if self._handle is None:
            return None
        if self._responded:
            # Do not disconnect immediately: Windows discards unread replies.
            try:
                self._modules.file.ReadFile(self._handle, 1)
            except Exception as error:
                if _code(error) == 232 and time.monotonic() - self._connected_at < 10:
                    return None
            self._disconnect()
        if self.slot.closed:
            self.close()
            return None
        if not self._connected:
            try:
                self._modules.pipe.ConnectNamedPipe(self._handle, None)
            except Exception as error:
                if _code(error) == 536:  # PIPE_LISTENING
                    return None
                if _code(error) != 535:  # PIPE_CONNECTED
                    self.close()
                    raise OperatorLaunchError("operator_launch_pipe_failed") from None
            self._connected = True
            self._connected_at = time.monotonic()
        try:
            code, data = self._modules.file.ReadFile(self._handle, MAX_REQUEST + 1)
            if code != 0:
                raise ValueError
        except Exception as error:
            if _code(error) == 232 and time.monotonic() - self._connected_at < 2:
                return None
            self.close()
            raise OperatorLaunchError("operator_launch_invalid_message") from None
        try:
            peer_sid = _sid_for_pid(
                self._modules, self._modules.pipe.GetNamedPipeClientProcessId(self._handle)
            )
            payload = decode_message(bytes(data), MAX_REQUEST)
            result = self.slot.handle(payload, peer_sid=peer_sid)
        except Exception:
            self.slot.close()
            result = {"ok": False, "error": "operator_launch_request_refused"}
        encoded = json.dumps(result, separators=(",", ":"), allow_nan=False).encode() + b"\n"
        if len(encoded) > MAX_RESPONSE:
            self.close()
            raise OperatorLaunchError("operator_launch_invalid_message")
        try:
            self._modules.file.WriteFile(self._handle, encoded)
        except Exception:
            self.close()
            raise OperatorLaunchError("operator_launch_reply_unconfirmed") from None
        self._responded = True
        self._connected_at = time.monotonic()
        return result

    def close(self) -> bool:
        if self._handle is not None:
            self._handle.Close()
            self._handle = None
        return self.slot.close()


def request_operator_launch(
    name: str, payload: dict[str, Any], *, timeout: float = 20
) -> dict[str, Any]:
    """Unprivileged local sender. No broker token is read or transmitted."""
    if not name.startswith(_PREFIX) or re.fullmatch(r"[a-f0-9]{32}", name[len(_PREFIX) :]) is None:
        raise OperatorLaunchError("operator_launch_invalid_pipe")
    if not 0 < timeout <= 30:
        raise OperatorLaunchError("operator_launch_invalid_timeout")
    request = LaunchRequest.model_validate(payload)
    data = json.dumps(request.model_dump(), separators=(",", ":")).encode() + b"\n"
    if len(data) > MAX_REQUEST:
        raise OperatorLaunchError("operator_launch_invalid_message")
    modules = _modules()
    deadline = time.monotonic() + timeout
    handle = None
    try:
        while handle is None:
            try:
                handle = modules.file.CreateFile(name, _ACCESS, 0, None, 3, 0, None)
            except Exception as error:
                if _code(error) not in (2, 231) or time.monotonic() >= deadline:
                    raise OperatorLaunchError("operator_launch_not_available") from None
                time.sleep(0.02)
        server_sid = _sid_for_pid(modules, modules.pipe.GetNamedPipeServerProcessId(handle))
        if server_sid != current_user_sid():
            raise OperatorLaunchError("operator_launch_same_user_required")
        modules.pipe.SetNamedPipeHandleState(handle, 1, None, None)  # NOWAIT, byte read mode
        modules.file.WriteFile(handle, data)
        response = bytearray()
        while time.monotonic() < deadline:
            try:
                _, chunk = modules.file.ReadFile(handle, MAX_RESPONSE + 1)
                response.extend(chunk)
                if len(response) > MAX_RESPONSE:
                    raise OperatorLaunchError("operator_launch_invalid_message")
                if b"\n" in response:
                    return decode_message(bytes(response), MAX_RESPONSE)
            except OperatorLaunchError:
                raise
            except Exception as error:
                if _code(error) != 232:
                    raise OperatorLaunchError("operator_launch_reply_unconfirmed") from None
            time.sleep(0.02)
        raise OperatorLaunchError("operator_launch_reply_unconfirmed")
    finally:
        if handle is not None:
            handle.Close()
