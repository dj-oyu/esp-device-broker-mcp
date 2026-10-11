"""Read-only pySerial discovery and pure identity resolution. Never opens a port."""

from collections.abc import Iterable
from typing import Any

from pydantic import Field, model_validator
from serial.tools import list_ports

from .models import StrictModel


class PortInfo(StrictModel):
    port: str
    vid: int | None = None
    pid: int | None = None
    serial_number: str | None = None
    location: str | None = None
    description: str | None = None


class DeviceSelector(StrictModel):
    vid: int = Field(ge=0, le=65535)
    pid: int = Field(ge=0, le=65535)
    serial_number: str | None = None
    location: str | None = None

    @model_validator(mode="after")
    def require_identity(self) -> "DeviceSelector":
        if not self.serial_number and not self.location:
            raise ValueError("select a serial_number or USB location, not a COM name")
        return self


def enumerate_ports() -> list[PortInfo]:
    return [
        PortInfo(
            port=p.device,
            vid=p.vid,
            pid=p.pid,
            serial_number=p.serial_number,
            location=p.location,
            description=p.description,
        )
        for p in list_ports.comports()
    ]


def resolve_port(selector: DeviceSelector, ports: Iterable[PortInfo]) -> PortInfo:
    def matches(port: PortInfo) -> bool:
        return (
            port.vid == selector.vid
            and port.pid == selector.pid
            and (selector.serial_number is None or port.serial_number == selector.serial_number)
            and (selector.location is None or port.location == selector.location)
        )

    candidates = [port for port in ports if matches(port)]
    if len(candidates) != 1:
        raise ValueError("device_missing" if not candidates else "device_ambiguous")
    return candidates[0]


def inventory_json() -> list[dict[str, Any]]:
    return [port.model_dump() for port in enumerate_ports()]
