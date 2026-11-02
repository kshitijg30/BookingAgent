"""Free text from providers (reviews, names, notes) is data. This wrapper makes misuse fail loudly.

An ``Untrusted`` value cannot be turned into a ``str`` by ``str()``, f-strings, ``%``, ``+`` or ``join``,
so it cannot be pasted into a prompt or a decision by accident. The only exit is
``escaped_for_display()``, used by reply templates inside a labelled quote block.
"""

from __future__ import annotations

import hashlib
import html
import re
import unicodedata
from typing import Any, NoReturn

MAX_LEN = 500

_INJECTION_HINTS = re.compile(
    r"ignore (all |any )?(previous|prior|above)|system\s*:|you are now|approve|override|"
    r"disregard|new instructions|assistant\s*:|book (this|it) (now|immediately)",
    re.IGNORECASE,
)


def _strip_invisible(text: str) -> str:
    pieces = []
    for ch in text:
        cp = ord(ch)
        if ch in "\t\n\r":
            pieces.append(ch)
            continue
        if cp < 0x20 or cp == 0x7F:
            continue
        if 0x200B <= cp <= 0x200F or 0x2028 <= cp <= 0x202E or 0x2060 <= cp <= 0x2064 or cp == 0xFEFF:
            continue
        pieces.append(ch)
    return "".join(pieces)


class Untrusted:
    __slots__ = ("_v",)
    _v: str

    def __init__(self, raw: str, max_len: int = MAX_LEN) -> None:
        cleaned = _strip_invisible(unicodedata.normalize("NFKC", raw))[:max_len]
        object.__setattr__(self, "_v", cleaned)

    def __setattr__(self, name: str, value: Any) -> NoReturn:
        raise AttributeError("Untrusted is immutable")

    def __str__(self) -> NoReturn:
        raise TypeError("Untrusted text cannot be converted to str")

    def __format__(self, _spec: str) -> NoReturn:
        raise TypeError("Untrusted text cannot be formatted")

    def __add__(self, other: object) -> NoReturn:
        raise TypeError("Untrusted text cannot be concatenated")

    def __radd__(self, other: object) -> NoReturn:
        raise TypeError("Untrusted text cannot be concatenated")

    def __iter__(self) -> NoReturn:
        raise TypeError("Untrusted text cannot be iterated")

    def __repr__(self) -> str:
        return f"Untrusted(len={len(self._v)}, sha256={self.fingerprint()})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Untrusted) and other._v == self._v

    def __hash__(self) -> int:
        return hash(("untrusted", self._v))

    def fingerprint(self) -> str:
        return hashlib.sha256(self._v.encode()).hexdigest()[:16]

    def looks_like_injection(self) -> bool:
        """For traces and demos only. Nothing in the agent branches on this."""
        return bool(_INJECTION_HINTS.search(self._v))

    def escaped_for_display(self) -> str:
        return html.escape(self._v, quote=True)

    def trace_view(self) -> dict[str, object]:
        return {
            "untrusted": True,
            "length": len(self._v),
            "sha256": self.fingerprint(),
            "injection_flag": self.looks_like_injection(),
        }
