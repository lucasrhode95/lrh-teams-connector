"""Passive HTML for sends: active content and unfinished markup are refused; passive markup passes unchanged.

Synthetic fixtures only.
"""

from __future__ import annotations

import pytest

from teams_connector.domain.errors import InvalidRequest
from teams_connector.service.passive_html import check_passive_html

PASSIVE = [
    "<p><strong>Due Friday</strong></p><ul><li>review the <code>budget</code> sheet</li>"
    '<li><a href="https://example.com/tracker">tracker</a></li></ul>',
    "<p>Line one<br>line two</p>",
    "<h2>Heading</h2><blockquote>quoted</blockquote><pre>code</pre>",
    "<p>Tom&amp;Jerry &lt;tag&gt;</p>",
    "Plain text with an ampersand Tom&Jerry",
]

ACTIVE = [
    ("<p><script>alert(1)</script></p>", "script"),
    ('<iframe src="https://example.com/frame"></iframe>', "iframe"),
    ("<form action=x><input name=a></form>", "form"),
    ('<a href="javascript:alert(1)">x</a>', "Active HTML URLs"),
    ('<a href="  JaVa\tScRiPt:alert(1)">x</a>', "Active HTML URLs"),
    ('<a href="vbscript:msgbox">x</a>', "Active HTML URLs"),
    ('<span onmouseover="alert(1)">x</span>', "event handlers"),
    ('<p onclick="alert(1)">x</p>', "event handlers"),
    ("<title>x</title>", "changes how"),
    ("<noscript><p>x</p></noscript>", "changes how"),
    ("<p>unfinished <b", "ends inside"),
    ('<p><a href="https://example.com/x', "ends inside"),
    ("<p>unfinished <!-- comment", "ends inside"),
    ("<p>text <script>alert(1)", "script"),
    ("<![CDATA[ > <img src=x onerror=alert(1)> ]]>", "CDATA"),
    ("<!DOCTYPE html><p>x</p>", "declarations"),
    ("<?xml version='1.0'?><p>x</p>", "processing instructions"),
    ('<p>see <img src="https://example.com/x.png"></p>', "Embedded media"),
    ('<video src="https://example.com/v.mp4"></video>', "Embedded media"),
]


@pytest.mark.parametrize("value", PASSIVE)
def test_passive_markup_passes(value: str) -> None:
    check_passive_html(value)


@pytest.mark.parametrize(("value", "reason"), ACTIVE)
def test_active_or_unfinished_markup_is_refused(value: str, reason: str) -> None:
    with pytest.raises(InvalidRequest, match=reason):
        check_passive_html(value)
