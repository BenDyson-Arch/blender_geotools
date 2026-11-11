"""Problems found by checks, shared by every check in GeoTools."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Severity(StrEnum):
    ERROR = "error"  # publishing would fail or be wrong
    WARNING = "warning"  # publishing works but something is dropped or degraded
    INFO = "info"


@dataclass(frozen=True, slots=True)
class Issue:
    severity: Severity
    code: str  # stable kebab-case id, e.g. "coordinate-range"
    message: str  # one plain sentence for the UI
    subject: str | None = None  # object/material/image name, if any
