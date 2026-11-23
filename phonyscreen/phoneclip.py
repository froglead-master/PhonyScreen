"""Text from the phone's clipboard when a copy carries no plain text of its own.

WebKit-based apps (Gmail, browsers, mail) offer about eight types per copy, in an order that changes
from copy to copy: HTML, a web archive, plain text, RTF, ... pymobiledevice3 asks only for each item's
first (primary) type, because asking for more can stall the phone's pasteboard daemon for a minute,
so the plain-text version often doesn't arrive. When the primary type is HTML or a web archive (which
wraps the HTML), the text is extracted here. Other primary types fall through to the full pull
(LeanMirror enables allow_full_pull).
"""

from __future__ import annotations

import plistlib
import re
from html.parser import HTMLParser
from typing import Any, Optional

UTI_HTML = "public.html"
UTI_WEB_ARCHIVES = ("Apple Web Archive pasteboard type", "com.apple.webarchive")

_BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "dd", "div", "dl", "dt", "figcaption", "figure",
    "footer", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li", "main", "nav", "ol", "p",
    "pre", "section", "table", "tr", "ul",
}
_SKIP_TAGS = {"head", "script", "style", "title"}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def _break(self, blank_line: bool = False) -> None:
        """End the current line at a block boundary, unless it already ended (no doubled breaks)."""
        tail = "".join(self.parts[-3:]).rstrip(" ")
        if tail and not tail.endswith("\n"):
            self.parts.append("\n")
        if blank_line:
            self.parts.append("\n")

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag == "br":
            self.parts.append("\n")
        elif tag in _BLOCK_TAGS:
            self._break()

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK_TAGS:
            self._break(blank_line=tag == "p")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(re.sub(r"[ \t\r\n\f]+", " ", data))  # HTML whitespace collapsing


def html_to_text(html: str) -> str:
    """Readable plain text of an HTML fragment: block elements and <br> become line breaks."""
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    text = "".join(parser.parts).replace("\xa0", " ")
    text = "\n".join(line.strip() for line in text.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _inline(data_map: dict[str, Any], uti: str) -> Optional[bytes]:
    datum = data_map.get(uti)
    raw = datum.get("data") if isinstance(datum, dict) else None
    return bytes(raw) if isinstance(raw, (bytes, bytearray)) and raw else None


def _decode(raw: bytes, encoding: Optional[str]) -> str:
    try:
        return raw.decode(encoding or "utf-8", errors="replace")
    except LookupError:  # an encoding name Python doesn't know
        return raw.decode("utf-8", errors="replace")


def web_archive_html(archive: bytes) -> Optional[str]:
    """HTML of a WebKit web archive's main resource (a binary plist), or None."""
    try:
        main = plistlib.loads(archive).get("WebMainResource") or {}
    except Exception:
        return None
    data = main.get("WebResourceData")
    if not isinstance(data, bytes) or "html" not in str(main.get("WebResourceMIMEType", "text/html")):
        return None
    return _decode(data, main.get("WebResourceTextEncodingName"))


def snapshot_html_text(snapshot: dict[str, Any]) -> Optional[str]:
    """Text of the first item that carries inline HTML or a web archive, or None."""
    pasteboard = snapshot.get("pasteboard")
    if isinstance(pasteboard, dict):
        snapshot = pasteboard
    for item in snapshot.get("items") or []:
        data_map = item.get("data") or {}
        html: Optional[str] = None
        raw = _inline(data_map, UTI_HTML)
        if raw is not None:
            html = _decode(raw, "utf-8")
        else:
            for uti in UTI_WEB_ARCHIVES:
                archive = _inline(data_map, uti)
                html = web_archive_html(archive) if archive is not None else None
                if html:
                    break
        text = html_to_text(html) if html else ""
        if text:
            return text
    return None


def install() -> None:
    """Make pymobiledevice3's pasteboard reading fall back to the HTML. PasteboardContent.from_snapshot
    looks snapshot_text up in its module on every call, so this covers both its PasteboardMonitor and
    read_pasteboard (used by LeanMirror._clip_pull_loop)."""
    from pymobiledevice3.remote.core_device import pasteboard_service as pb

    if getattr(pb.snapshot_text, "_phonyscreen_html", False):
        return
    original = pb.snapshot_text

    def snapshot_text(snapshot: dict[str, Any]) -> Optional[str]:
        return original(snapshot) or snapshot_html_text(snapshot)

    snapshot_text._phonyscreen_html = True  # type: ignore[attr-defined]
    pb.snapshot_text = snapshot_text
