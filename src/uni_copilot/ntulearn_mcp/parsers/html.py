"""
HTML -> light Markdown + links + files, for announcement and message bodies.

In plain words:
  NTULearn stores a post's body as HTML: the text plus tags like <p>, <br>,
  <strong>, <a href=...>, <table>. The tools return JSON to Claude, and Claude
  then writes the answer for wherever you asked (Telegram or the terminal).
  So this file's job is to give Claude the post faithfully but cleanly:
  - `text`: the post as light Markdown, which keeps what matters on the page
    (**bold**, *italic*, [link text](url), "- " list items, "a | b" table rows)
    and drops the noise (styles, <span>s, hidden attributes). Fewer tokens
    than HTML, and Claude turns Markdown into Telegram formatting easily.
  - `links`: every link as (text, url), so Claude can list them directly.
  - `files`: names of files attached inside the post (Ultra "attachment"
    links), e.g. "tutorial5.pdf".

How: Python's built-in html.parser walks the HTML and calls our methods for
each opening tag, closing tag and piece of text. We collect the text and add
Markdown and line breaks where the page would show formatting.

Verified on your 83 announcements (24 Oct): tags used are mostly p, td, span,
br, strong, tr, li, a, ul, em; tables are common, often with <p> inside cells;
attached files are <a data-bbtype="attachment" data-bbfile='{...}'> and the
JSON always has "linkName" (26/26).

Safety: the body is untrusted (written by someone else). This parser only
reads it: no network, nothing executed. <script>/<style> contents are dropped,
and only http(s)/mailto links are kept, so a "javascript:" link can't sneak
through as something clickable. Markdown characters already in the text
(like * or _) are left as they are, not escaped.
"""

import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urlparse

from uni_copilot.ntulearn_mcp.config import BASE_URL

# Tags that start a new line on the page.
BLOCK_TAGS = {
    "p", "div", "section", "article", "header", "footer", "blockquote", "pre",
    "table", "tr", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "hr",
}
# Of those, the ones that also leave an empty line around them.
PARAGRAPH_TAGS = {"p", "blockquote", "pre", "table", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6"}
# Inline tags turned into Markdown. <u> (underline) has no Markdown, and profs
# use it for emphasis, so it becomes bold. Headings also become bold.
BOLD_TAGS = {"strong", "b", "u", "h1", "h2", "h3", "h4", "h5", "h6"}
ITALIC_TAGS = {"em", "i"}
# Tags whose contents are never shown as text.
SKIP_TAGS = {"script", "style", "head", "title", "noscript"}
SAFE_SCHEMES = {"http", "https", "mailto"}
TRUNCATED = " …[truncated]"


# What parse_html returns: the post as Markdown, its links and its files.
@dataclass
class ParsedHtml:
    text: str = ""
    links: list[tuple[str, str]] = field(default_factory=list)  # (text, url)
    files: list[str] = field(default_factory=list)


# Walks the HTML and collects text pieces into self.parts. html.parser calls
# handle_starttag / handle_endtag / handle_data as it reads, in order.
class _TextExtractor(HTMLParser):
    # convert_charrefs=True turns "&amp;" into "&", "&nbsp;" into a space, etc.
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.links: list[tuple[str, str]] = []
        self.files: list[str] = []
        self._skip_depth = 0   # >0 while inside <script>, <style>, ...
        self._pre_depth = 0    # >0 while inside <pre>: keep spacing as is
        self._cell_depth = 0   # >0 while inside a table cell: no line breaks
        # Open <a> tags: (safe url or None, attached file name or None, start index in parts).
        self._open_links: list[tuple[str | None, str | None, int]] = []
        # Open bold/italic tags: (tag, Markdown marker, start index in parts).
        self._open_marks: list[tuple[str, str, int]] = []

    # An opening tag like <p>, <br>, <strong>, <a href=...>, <td>.
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in SKIP_TAGS:
            self._skip_depth += 1
            return
        a = dict(attrs)
        if tag in BLOCK_TAGS:
            self._break(2 if tag in PARAGRAPH_TAGS else 1)
            if tag == "pre":
                self._pre_depth += 1
        if tag == "br":
            self.parts.append(" " if self._cell_depth else "\n")
        elif tag == "li":
            self._break(1)
            self.parts.append("- ")
        elif tag in ("td", "th"):
            self.parts.append(" | ")
            self._cell_depth += 1
        elif tag == "img":
            alt = (a.get("alt") or "").strip()
            self.parts.append(f"[image: {alt}]" if alt else "[image]")
        elif tag == "a":
            file_name = bb_file_name(a.get("data-bbfile")) if a.get("data-bbfile") else None
            self._open_links.append((safe_href(a.get("href")), file_name, len(self.parts)))
        if tag in BOLD_TAGS or tag in ITALIC_TAGS:
            marker = "**" if tag in BOLD_TAGS else "*"
            # Already inside the same kind of mark (e.g. <strong> in <strong>)?
            # Don't open it twice, or we'd get "****".
            if not any(m == marker for _, m, _ in self._open_marks):
                self._open_marks.append((tag, marker, len(self.parts)))

    # A closing tag like </p>, </strong> or </a>.
    def handle_endtag(self, tag: str) -> None:
        if tag in SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag in BOLD_TAGS or tag in ITALIC_TAGS:
            for i in range(len(self._open_marks) - 1, -1, -1):
                if self._open_marks[i][0] == tag:
                    _, marker, start = self._open_marks.pop(i)
                    self._wrap(start, marker)
                    break
        if tag == "a" and self._open_links:
            self._close_link(*self._open_links.pop())
        elif tag in ("td", "th"):
            self._cell_depth = max(0, self._cell_depth - 1)
        if tag in BLOCK_TAGS:
            self._break(2 if tag in PARAGRAPH_TAGS else 1)
            if tag == "pre":
                self._pre_depth = max(0, self._pre_depth - 1)

    # A piece of text between tags. Outside <pre>, runs of spaces and source
    # line breaks collapse to one space, like a browser does.
    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        self.parts.append(data if self._pre_depth else re.sub(r"\s+", " ", data))

    # Finishes an <a>: attached files become "[file: name]" and go to
    # self.files; normal links become "[text](url)" (or just the url) and go
    # to self.links; unsafe links keep only their text.
    def _close_link(self, href: str | None, file_name: str | None, start: int) -> None:
        start = min(start, len(self.parts))
        text = "".join(self.parts[start:]).strip()
        if file_name:
            del self.parts[start:]
            self.parts.append(f"[file: {file_name}]")
            if file_name not in self.files:
                self.files.append(file_name)
        elif href:
            if (text or href, href) not in self.links:
                self.links.append((text or href, href))
            if text and text != href:
                del self.parts[start:]
                self.parts.append(f"[{text}]({href})")
            elif not text:
                self.parts.append(href)

    # Puts a Markdown marker around everything written since `start`, keeping
    # outer spaces outside: " Friday " -> " **Friday** " (Markdown needs the
    # marker right next to the text). Empty or blank contents are left alone.
    def _wrap(self, start: int, marker: str) -> None:
        start = min(start, len(self.parts))
        segment = "".join(self.parts[start:])
        core = segment.strip()
        if not core:
            return
        lead = segment[: len(segment) - len(segment.lstrip())]
        trail = segment[len(segment.rstrip()):]
        del self.parts[start:]
        self.parts.append(f"{lead}{marker}{core}{marker}{trail}")

    # Makes sure the text so far ends with at least `n` line breaks
    # (1 = new line, 2 = empty line) without piling up extra ones, so
    # "</tr><tr>" or "</div><div>" gives one new line, not an empty line.
    # Inside a table cell it adds a space instead, so a <p> in a cell doesn't
    # split the row. <br> doesn't use this: two <br>s really mean an empty line.
    def _break(self, n: int) -> None:
        if not self.parts:
            return
        if self._cell_depth:
            self.parts.append(" ")
            return
        tail = "".join(self.parts[-4:]).rstrip(" ")
        have = len(tail) - len(tail.rstrip("\n"))
        if have < n:
            self.parts.append("\n" * (n - have))


# The main entry point: HTML (or plain text, or None) -> ParsedHtml.
# max_chars cuts very long posts and marks the cut, so one huge announcement
# can't flood Claude's context. links and files are always complete.
def parse_html(html: str | None, max_chars: int | None = None) -> ParsedHtml:
    if not html:
        return ParsedHtml()
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    text = tidy("".join(parser.parts))
    if max_chars is not None and len(text) > max_chars:
        text = text[:max_chars].rstrip() + TRUNCATED
    return ParsedHtml(text=text, links=parser.links, files=parser.files)


# Cleans up the joined text: non-breaking spaces become spaces, each line is
# trimmed, table rows lose their leading "| ", and blank lines are limited to
# one in a row.
def tidy(text: str) -> str:
    text = text.replace("\xa0", " ").replace("\r", "")
    lines = []
    for line in text.split("\n"):
        line = re.sub(r"[ \t]+", " ", line).strip()
        if line.startswith("| "):
            line = line[2:]
        lines.append(line)
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# Keeps a link only if it's http(s) or mailto. "/path" links on NTULearn get
# the base URL in front. Anything else (javascript:, data:, ...) -> None.
def safe_href(href: str | None) -> str | None:
    if not href:
        return None
    href = href.strip()
    if href.startswith("/") and not href.startswith("//"):
        return BASE_URL + href
    return href if urlparse(href).scheme.lower() in SAFE_SCHEMES else None


# Reads the file name from Ultra's data-bbfile JSON. "linkName" was present on
# all 26 attachments checked (24 Oct); the others are fallbacks.
def bb_file_name(raw: str | None) -> str | None:
    if not raw:
        return None
    try:
        info = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(info, dict):
        return None
    for key in ("linkName", "displayName", "fileName", "name"):
        if isinstance(info.get(key), str) and info[key].strip():
            return info[key].strip()
    return None


# Self-test: uv run python -m uni_copilot.ntulearn_mcp.parsers.html
# Offline, made-up HTML only.
def _main() -> None:
    sample = """
    <p>Dear all,</p>
    <p>The   quiz is on <strong>Friday&nbsp;3pm </strong>in LT1. Calculators are <u>NOT</u> allowed.<br>Bring:</p>
    <ul><li>matric card</li><li><em>calculator</em> &amp; pencil</li></ul>
    <p>Slides: <a href="/bbcswebdav/xid-1_1">week 10</a>,
       Zoom: <a href="https://example.com/j/1">https://example.com/j/1</a>,
       <a href="javascript:alert(1)">click me</a></p>
    <script>steal()</script><style>p{color:red}</style>
    <p><a href="/x" data-bbtype="attachment" data-bbfile='{"linkName":"tutorial5.pdf","render":"inline"}'>tutorial5.pdf</a> <img alt="diagram"></p>
    <table><tbody><tr><th>Group</th><th>Room</th></tr>
    <tr><td><p>A</p></td><td><p>TR+12</p><p>(level 2)</p></td></tr></tbody></table>
    """
    parsed = parse_html(sample)
    print("1) text:")
    print(parsed.text)
    print("\n2) links:", parsed.links)
    print("3) files:", parsed.files)
    print("\n4) truncated to 40 chars:", repr(parse_html(sample, max_chars=40).text))
    print("5) plain text and None:", repr(parse_html("just text").text), repr(parse_html(None).text))


if __name__ == "__main__":
    _main()
