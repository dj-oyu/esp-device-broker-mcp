"""Frozen pre-onion descriptor encoding and the omitted-digest J1 exception.

These historical bytes identify old records. They never admit a source commit.
Do not add a verified-commit ledger here or rewrite saved hashes.
"""

import hashlib
import json
from dataclasses import dataclass


@dataclass(frozen=True)
class LegacyDiagnosticDescriptor:
    repository: str
    commit: str
    contract: str = "j1-fixed-v1"
    modes: tuple[str, ...] = ("ON", "OFF")
    dma_modes: tuple[str, ...] = ()

    @property
    def sha256(self) -> str:
        descriptor = {
            "schema_version": 1,
            "repository": self.repository,
            "commit": self.commit,
            "contract": self.contract,
            "modes": self.modes,
            "idf_version": "6.0.1",
            "target": "esp32s3",
            "detail_option": "POCKET_DERBY_DETAIL_MEASURE",
        }
        if self.dma_modes:
            descriptor["dma_modes"] = self.dma_modes
            descriptor["dma_option"] = "KASANE_DMA_OVERLAP"
        raw = json.dumps(descriptor, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(raw).hexdigest()


J1_COMMIT = "b561aea1c0798feb28055a00d36681fae81ac58a"
J1_REPOSITORY = "dj-oyu/cardputer-adv-pocketjs"
_J1_ENTRY = LegacyDiagnosticDescriptor(J1_REPOSITORY, J1_COMMIT)
