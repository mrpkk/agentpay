"""Реальные детерминированные ядра, продаваемые по вызовам."""

from __future__ import annotations

from .ailegal import (
    AilegalCore,
    CoreUnavailable,
    ailegal_root,
    load_ailegal_core,
)

__all__ = [
    "AilegalCore",
    "CoreUnavailable",
    "ailegal_root",
    "load_ailegal_core",
]
