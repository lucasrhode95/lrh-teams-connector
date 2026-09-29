from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from typing import Any


class TeamsHtmlParser(HTMLParser):
    """Convert common Teams rich-text markup into readable Markdown-like text."""

    BLOCK_TAGS = {"p", "div", "section", "article", "tr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.list_stack: list[dict[str, Any]] = []
        self.link_stack: list[tuple[int, str]] = []
        self.pre_depth = 0
        self.ignored_depth = 0
        self.suppress_leading_mention_at = False
        self.mention_tags: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        attributes = {name.lower(): value or "" for name, value in attrs}
        item_type = attributes.get("itemtype", "").lower()
        if item_type.rstrip("/").endswith("/mention"):
            self.mention_tags.append(tag)
            self.suppress_leading_mention_at = True
            self.parts.append("@(")
        if "amsimage" in item_type:
            image_url = (
                attributes.get("src")
                or attributes.get("data-src")
                or attributes.get("itemid")
                or attributes.get("alt")
            )
            if image_url or tag == "img":
                self._newline(1)
                self.parts.append(f"[Image: {image_url or 'inline image'}]")
                if tag == "img":
                    return
        if tag in {"script", "style"}:
            self.ignored_depth += 1
            return
        if self.ignored_depth:
            return
        if tag in self.BLOCK_TAGS:
            self._newline(2)
        elif tag == "br":
            self._newline(1)
        elif tag in {"ul", "ol"}:
            self.list_stack.append(
                {"tag": tag, "next": self._positive_int(attributes.get("start"), 1)}
            )
        elif tag == "li":
            self._newline(1)
            depth = max(0, len(self.list_stack) - 1)
            context = self.list_stack[-1] if self.list_stack else {"tag": "ul", "next": 1}
            if context["tag"] == "ol":
                marker = f"{context['next']}. "
                context["next"] += 1
            else:
                marker = "- "
            self.parts.append("  " * depth + marker)
        elif tag in {"strong", "b"}:
            self.parts.append("**")
        elif tag in {"em", "i"}:
            self.parts.append("*")
        elif tag == "u":
            self.parts.append("__")
        elif tag == "blockquote":
            self._newline(2)
            self.parts.append("> ")
        elif tag == "a":
            href = attributes.get("href", "").strip()
            self.link_stack.append((len(self.parts), href))
        elif tag == "pre":
            self._newline(2)
            self.parts.append("```\n")
            self.pre_depth += 1
        elif tag == "code" and not self.pre_depth:
            self.parts.append("`")
        elif tag == "img":
            item_type = attributes.get("itemtype", "").lower()
            if "amsimage" in item_type or attributes.get("src"):
                image_url = (
                    attributes.get("src")
                    or attributes.get("data-src")
                    or attributes.get("itemid")
                    or attributes.get("alt")
                )
                self._newline(1)
                self.parts.append(f"[Image: {image_url or 'inline image'}]")

    def handle_endtag(self, tag: str) -> None:
        if self.mention_tags and tag == self.mention_tags[-1]:
            self.mention_tags.pop()
            self.suppress_leading_mention_at = False
            self.parts.append(")")
        if tag in {"script", "style"} and self.ignored_depth:
            self.ignored_depth -= 1
            return
        if self.ignored_depth:
            return
        if tag in self.BLOCK_TAGS or tag == "li":
            self._newline(2 if tag in self.BLOCK_TAGS else 1)
        elif tag in {"ul", "ol"}:
            if self.list_stack:
                self.list_stack.pop()
            self._newline(1)
        elif tag in {"strong", "b"}:
            self.parts.append("**")
        elif tag in {"em", "i"}:
            self.parts.append("*")
        elif tag == "u":
            self.parts.append("__")
        elif tag == "blockquote":
            self._newline(2)
        elif tag == "a" and self.link_stack:
            start, href = self.link_stack.pop()
            label = "".join(self.parts[start:]).strip()
            del self.parts[start:]
            if label and href and href != label:
                self.parts.append(f"{label} ({href})")
            else:
                self.parts.append(label or href)
        elif tag == "pre" and self.pre_depth:
            self.pre_depth -= 1
            self._newline(1)
            self.parts.append("\n```")
            self._newline(2)
        elif tag == "code" and not self.pre_depth:
            self.parts.append("`")

    def handle_data(self, data: str) -> None:
        if self.ignored_depth:
            return
        if self.suppress_leading_mention_at and data.strip():
            leading_whitespace = len(data) - len(data.lstrip())
            if data[leading_whitespace:].startswith("@"):
                data = data[:leading_whitespace] + data[leading_whitespace + 1 :]
            self.suppress_leading_mention_at = False
        self.parts.append(data)

    def _newline(self, count: int) -> None:
        current = "".join(self.parts)
        trailing = len(current) - len(current.rstrip("\n"))
        if trailing < count:
            self.parts.append("\n" * (count - trailing))

    @staticmethod
    def _positive_int(value: str, fallback: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return fallback
        return parsed if parsed > 0 else fallback

    def text(self) -> str:
        raw = "".join(self.parts).replace("\r", "").replace("\xa0", " ")
        result: list[str] = []
        in_code = False
        previous_blank = False
        for line in raw.split("\n"):
            if line.strip().startswith("```"):
                in_code = not in_code
                result.append(line.strip())
                previous_blank = False
                continue
            if in_code:
                result.append(line.rstrip())
                continue
            cleaned = re.sub(r"[\t ]+", " ", line).strip()
            if not cleaned:
                if result and not previous_blank:
                    result.append("")
                previous_blank = True
                continue
            result.append(cleaned)
            previous_blank = False
        while result and not result[-1]:
            result.pop()
        return "\n".join(result).strip()


class TeamsMessageFormatter:
    """Render one Teams message as readable plain text with useful markers."""

    def format_message(self, message: dict[str, Any]) -> str:
        content = self._first_string(message, "content", "body", "text") or ""
        kind = self._first_string(message, "messagetype", "messageType") or ""
        content_type = self._first_string(message, "contenttype", "contentType") or ""
        is_html = "html" in kind.lower() or "html" in content_type.lower()

        if is_html and content:
            parser = TeamsHtmlParser()
            try:
                parser.feed(content)
                parser.close()
                body = parser.text()
            except Exception:
                body = content.strip()
        else:
            body = self._normalize_plain_text(content)

        markers = self._attachment_markers(message)
        if body and markers:
            return body + "\n\n" + "\n".join(markers)
        if markers:
            return "\n".join(markers)
        if body:
            return body
        if kind and kind.lower() not in {"richtext/html", "text", "richtext"}:
            return f"[System message: {kind}]"
        return ""

    def _attachment_markers(self, message: dict[str, Any]) -> list[str]:
        properties = message.get("properties")
        properties = properties if isinstance(properties, dict) else {}
        raw_files = (
            properties.get("files")
            or message.get("files")
            or message.get("attachments")
        )
        files = self._as_items(raw_files)
        markers: list[str] = []
        for item in files:
            if not isinstance(item, dict):
                continue
            info = item.get("fileInfo")
            info = info if isinstance(info, dict) else {}
            name = (
                item.get("fileName")
                or item.get("filename")
                or item.get("name")
                or item.get("displayName")
            )
            url = (
                info.get("fileUrl")
                or info.get("shareUrl")
                or item.get("fileUrl")
                or item.get("shareUrl")
                or item.get("objectUrl")
                or item.get("url")
            )
            file_type = str(item.get("fileType") or item.get("contentType") or "")
            label = "Image" if file_type.lower().startswith("image/") else "Attachment"
            markers.append(f"[{label}: {name or 'file'}]")
            if isinstance(url, str) and url.strip():
                markers.append(url.strip())
        return self._unique(markers)

    @staticmethod
    def _as_items(value: Any) -> list[Any]:
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                return []
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            if any(key in value for key in ("fileName", "filename", "name", "fileInfo")):
                return [value]
            return [item for item in value.values() if isinstance(item, dict)]
        return []

    @staticmethod
    def _first_string(source: dict[str, Any], *keys: str) -> str | None:
        for key in keys:
            value = source.get(key)
            if isinstance(value, str):
                return value
        return None

    @staticmethod
    def _normalize_plain_text(content: str) -> str:
        lines = [re.sub(r"[\t ]+", " ", line).strip() for line in content.splitlines()]
        output: list[str] = []
        blank = False
        for line in lines:
            if not line:
                if output and not blank:
                    output.append("")
                blank = True
                continue
            output.append(line)
            blank = False
        while output and not output[-1]:
            output.pop()
        return "\n".join(output)

    @staticmethod
    def _unique(values: list[str]) -> list[str]:
        output: list[str] = []
        seen: set[str] = set()
        for value in values:
            if value not in seen:
                output.append(value)
                seen.add(value)
        return output


