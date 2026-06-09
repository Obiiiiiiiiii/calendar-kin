"""Backstory input — the swappable seam.

There is currently no Kindroid API to read a kin's profile/backstory, so the
MVP obtains it via user paste (file or stdin). Everything downstream of
``BackstorySource.fetch()`` is source-agnostic: when Kindroid ships a
profile-read endpoint, add a ``KindroidApiSource`` here and nothing else
changes.
"""

from __future__ import annotations

import sys
from abc import ABC, abstractmethod
from pathlib import Path


class BackstorySource(ABC):
    @abstractmethod
    def fetch(self) -> str:
        """Return the kin's backstory as plain text."""


class FileSource(BackstorySource):
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def fetch(self) -> str:
        return self.path.read_text(encoding="utf-8")


class StdinSource(BackstorySource):
    def fetch(self) -> str:
        return sys.stdin.read()


class TextSource(BackstorySource):
    def __init__(self, text: str):
        self.text = text

    def fetch(self) -> str:
        return self.text


def source_from_arg(arg: str) -> BackstorySource:
    """'-' means stdin; anything else is a file path."""
    if arg == "-":
        return StdinSource()
    return FileSource(arg)
