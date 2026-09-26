#!/usr/bin/env python3
"""Render build/print.html to PDF with a paginated table of contents (two passes)."""
import json, re, html
from pathlib import Path
from playwright.sync_api import sync_playwright

SRC = Path("/home/claude/build/print.html")
OUT = Path("/home/claude/agentic-ai-architecture-documentation/Agentic-AI-Architecture-Design-Document.pdf")
TMP = Path("/home/claude/build/pass1.pdf")
TMP_HTML = Path("/home/claude/build/print-pass.html")

HEADER = """<div style="font-family:Helvetica,Arial,sans-serif;font-size:7.5pt;color:#6b7280;width:100%;
padding:0 15mm;display:flex;justify-content:space-between;border-bottom:0.4pt solid #d7dbe1;padding-bottom:3pt;">
<span>Design and Implementation of a Production-Grade Agentic AI Application Architecture</span><span>Version 1.0</span></div>"""
FOOTER = """<div style="font-family:Helvetica,Arial,sans-serif;font-size:7.5pt;color:#6b7280;width:100%;
padding:0 15mm;display:flex;justify-content:space-between;">
<span>26 September 2026</span><span>Page <span class="pageNumber"></span> of <span class="totalPages"></span></span></div>"""


def toc_html(entries, pages):
    items = []
    for i, (level, num, text, anchor) in enumerate(entries):
        pg = pages[i] if pages else ""
        items.append(f'<li class="l{level}"><span class="n">{num}</span><a class="t" href="#{anchor}">{html.escape(text)}</a>'
                     f'<span class="dots"></span><span class="p">{pg}</span></li>')
    return "".join(items)


def render(html_text, out, pw):
    TMP_HTML.write_text(html_text)
    b = pw.chromium.launch()
    pg = b.new_page()
    pg.goto(TMP_HTML.as_uri(), wait_until="load")
    pg.wait_for_timeout(500)
    pg.pdf(path=str(out), format="A4", print_background=True, prefer_css_page_size=True,
           display_header_footer=True, header_template=HEADER, footer_template=FOOTER,
           margin={"top": "18mm", "bottom": "16mm", "left": "15mm", "right": "15mm"})
    b.close()


def norm(s):
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def find_pages(pdf_path, entries):
    import subprocess
    raw = subprocess.run(["pdftotext", "-layout", str(pdf_path), "-"], capture_output=True, text=True).stdout
    texts = [norm(t) for t in raw.split("\f")]
    pages, start = [], 3   # skip cover; TOC pages contain the same strings
    # the TOC itself lists every heading, so begin searching after the TOC (first page with "Chapter 1" kicker)
    for i, t in enumerate(texts):
        if "chapter 1 " in t and "introduction and scope" in t and i >= 2:
            start = i; break
    cur = start
    for level, num, text, anchor in entries:
        key = norm(f"{num} {text}") if level == 2 else norm(text)
        found = None
        for i in range(cur, len(texts)):
            if key in texts[i]:
                found = i; break
        if found is None:   # heading may wrap lines; try the text only
            for i in range(cur, len(texts)):
                if norm(text) in texts[i]:
                    found = i; break
        if found is None:
            found = cur
        pages.append(found + 1)
        cur = found
    return pages


def main():
    src = SRC.read_text()
    entries = [(l, n, html.unescape(t), a) for l, n, t, a in json.loads(re.search(r"<!--TOC:(.*?)-->", src, re.S).group(1))]
    with sync_playwright() as pw:
        pass1 = src.replace('<ul class="toc-list" id="tocList"></ul>', f'<ul class="toc-list">{toc_html(entries, None)}</ul>')
        render(pass1, TMP, pw)
        pages = find_pages(TMP, entries)
        pass2 = src.replace('<ul class="toc-list" id="tocList"></ul>', f'<ul class="toc-list">{toc_html(entries, pages)}</ul>')
        render(pass2, OUT, pw)
        # verify page count did not shift the TOC
        pages2 = find_pages(OUT, entries)
        if pages2 != pages:
            pass3 = src.replace('<ul class="toc-list" id="tocList"></ul>', f'<ul class="toc-list">{toc_html(entries, pages2)}</ul>')
            render(pass3, OUT, pw)
    from pypdf import PdfReader
    print(f"PDF: {len(PdfReader(str(OUT)).pages)} pages -> {OUT}")
    for (level, num, text, a), p in list(zip(entries, pages2))[:16]:
        print(f"  {'  ' if level == 2 else ''}{num} {text} .... {p}")


if __name__ == "__main__":
    main()
