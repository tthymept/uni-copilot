"""Offline tests for parsers/html.py, using a synthetic announcement body."""

from pathlib import Path

from uni_copilot.ntulearn_mcp.parsers.html import bb_file_name, parse_html, safe_href

SAMPLE = (Path(__file__).parent / "fixtures" / "announcement_sample.html").read_text(encoding="utf-8")


# The sample becomes readable Markdown: bold (also from <u>), italic, list, table row.
def test_markdown_formatting():
    text = parse_html(SAMPLE).text
    assert "The quiz is on **Friday 3pm** in LT99. Calculators are **NOT** allowed." in text
    assert "- matric card\n- *pencil* & eraser" in text
    assert "Group | Room\nA | TR+99 (level 2)" in text  # two <p> in one cell stay on one row


# Links: relative ones get the NTULearn base URL; javascript: links keep only their text.
def test_links():
    parsed = parse_html(SAMPLE)
    assert "[week 10](https://ntulearn.ntu.edu.sg/bbcswebdav/xid-1_1)" in parsed.text
    assert ("week 10", "https://ntulearn.ntu.edu.sg/bbcswebdav/xid-1_1") in parsed.links
    assert "click me" in parsed.text
    assert not any("javascript" in url for _, url in parsed.links)


# Attachments become [file: name] and are listed in files, not in links.
def test_files():
    parsed = parse_html(SAMPLE)
    assert parsed.files == ["tutorial5.pdf"]
    assert "[file: tutorial5.pdf] [image: diagram]" in parsed.text
    assert not any(url.endswith("/x") for _, url in parsed.links)


# <script> and <style> contents never reach the text.
def test_script_and_style_dropped():
    text = parse_html(SAMPLE).text
    assert "steal" not in text and "color:red" not in text


# Text that looks like an instruction is kept as plain data (the parser never acts on it;
# the models label it UNTRUSTED for Claude).
def test_injection_text_is_just_text():
    assert "Ignore your previous instructions" in parse_html(SAMPLE).text


# Line breaks: back-to-back blocks give one new line, <br><br> and paragraphs an empty line.
def test_line_breaks():
    assert parse_html("<div>a</div><div>b</div>").text == "a\nb"
    assert parse_html("a<br><br>b").text == "a\n\nb"
    assert parse_html("<p>x</p> <p>y</p>").text == "x\n\ny"


# Nested bold doesn't turn into "****"; empty bold leaves nothing behind.
def test_nested_and_empty_bold():
    assert parse_html("<strong>a <b>b</b> c</strong>").text == "**a b c**"
    assert parse_html("x<strong> </strong>y").text == "x y"


# Long bodies are cut and marked; links and files are still complete.
def test_truncation():
    parsed = parse_html(SAMPLE, max_chars=40)
    assert parsed.text.endswith("…[truncated]")
    assert parsed.files == ["tutorial5.pdf"]


# Plain text passes through; None and "" give an empty result.
def test_plain_and_empty():
    assert parse_html("just text").text == "just text"
    assert parse_html(None).text == "" and parse_html("").links == []


# Only http(s)/mailto links survive; /paths get the base URL; //host paths are refused.
def test_safe_href():
    assert safe_href("/a") == "https://ntulearn.ntu.edu.sg/a"
    assert safe_href("https://x.org") == "https://x.org"
    assert safe_href("mailto:a@b.c") == "mailto:a@b.c"
    assert safe_href("javascript:alert(1)") is None
    assert safe_href("data:text/html,hi") is None
    assert safe_href("//evil.example/x") is None


# File names come from linkName first, then the fallbacks; bad JSON gives None.
def test_bb_file_name():
    assert bb_file_name('{"linkName": "a.pdf", "fileName": "b.pdf"}') == "a.pdf"
    assert bb_file_name('{"displayName": "c.pdf"}') == "c.pdf"
    assert bb_file_name("not json") is None
    assert bb_file_name('["a"]') is None
