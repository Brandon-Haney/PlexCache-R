"""
Confirm prompts use the themed dialog, not the browser's confirm() box.

app.js hooks htmx:confirm, so every hx-confirm gets pcConfirm(); plain JS calls
pcConfirm() directly. These checks keep native confirm() from coming back and
give each hx-confirm a dialog title.
"""

import re
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "web"
APP_JS = (WEB / "static" / "js" / "app.js").read_text(encoding="utf-8")


def _sources():
    for path in (WEB / "templates").rglob("*.html"):
        yield path
    for path in (WEB / "static" / "js").rglob("*.js"):
        if "vendor" not in path.parts:
            yield path


_COMMENT = re.compile(r"""<!--.*?-->|\{#.*?#\}|/\*.*?\*/|(?<![:'"\\])//[^\n]*""", re.S)


def _strip_comments(source):
    """Blank out comments, keeping newlines so line numbers still match."""
    return _COMMENT.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), source)


def test_app_js_routes_hx_confirm_through_the_themed_dialog():
    assert "htmx:confirm" in APP_JS
    assert "window.pcConfirm" in APP_JS
    assert "issueRequest(true)" in APP_JS


def test_no_native_confirm_calls():
    """Only app.js may fall back to window.confirm (before <body> exists)."""
    offenders = []
    for path in _sources():
        source = _strip_comments(path.read_text(encoding="utf-8"))
        for match in re.finditer(r"(?<![\w$])(?:window\.)?confirm\s*\(", source):
            if path.name == "app.js" and match.group(0).startswith("window."):
                continue
            line = source[:match.start()].count("\n") + 1
            offenders.append(f"{path.relative_to(WEB)}:{line}")
    assert not offenders, "Use pcConfirm() instead of confirm(): " + ", ".join(offenders)


def test_every_hx_confirm_has_a_dialog_title():
    tag = re.compile(r"<(?:[^<>]|\{\{[^}]*\}\}|\{%[^%]*%\})*?\bhx-confirm=(?:[^<>]|\{\{[^}]*\}\}|\{%[^%]*%\})*>", re.S)
    offenders = []
    for path in (WEB / "templates").rglob("*.html"):
        source = path.read_text(encoding="utf-8")
        for match in tag.finditer(source):
            if "data-confirm-title=" not in match.group(0):
                line = source[:match.start()].count("\n") + 1
                offenders.append(f"{path.relative_to(WEB)}:{line}")
    assert not offenders, "Add data-confirm-title to: " + ", ".join(offenders)
