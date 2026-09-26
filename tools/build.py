#!/usr/bin/env python3
"""Build the documentation site and the print-ready HTML from build/content/*.md."""
import json, re, html, shutil
from pathlib import Path
import markdown
from pygments.formatters import HtmlFormatter

ROOT = Path("/home/claude/agentic-ai-architecture-documentation")
CONTENT = Path("/home/claude/build/content")
TEMPLATES = Path("/home/claude/build/templates")
DOC_TITLE = "Agentic AI Application Architecture"
VERSION = "1.0"
DATE = "26 September 2026"

MD_EXT = ["tables", "fenced_code", "codehilite", "toc", "attr_list", "admonition", "md_in_html", "sane_lists"]
MD_CFG = {"codehilite": {"css_class": "codehilite", "guess_lang": False},
          "toc": {"permalink": False, "toc_depth": "2-3"}}

DIAGRAM_CAPTIONS = {}


def parse_front_matter(text):
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    meta = {}
    for line in m.group(1).splitlines():
        k, v = line.split(":", 1)
        meta[k.strip()] = v.strip().strip('"')
    return meta, m.group(2)


def slugify(s):
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s


def render_markdown(body):
    md = markdown.Markdown(extensions=MD_EXT, extension_configs=MD_CFG)
    out = md.convert(body)
    # task lists
    out = re.sub(r"<li>\[ \] ", '<li class="task"><span class="checkbox" aria-hidden="true"></span>', out)
    return out


class Page:
    def __init__(self, path):
        text = path.read_text()
        self.meta, self.body = parse_front_matter(text)
        self.file = self.meta["file"]
        self.title = self.meta["title"]
        self.html_raw = render_markdown(self.body)
        self.sections = []   # (level, text, id)

    # ---- token expansion ------------------------------------------------------- #
    def expand(self, chapter_no, mode):
        """mode: 'site' or 'pdf'. Returns HTML with numbered headings and figures."""
        h = self.html_raw
        fig_n = [0]
        tab_n = [0]
        prefix = f"{chapter_no}." if chapter_no else ""

        def fig_label():
            fig_n[0] += 1
            return f"Figure {prefix}{fig_n[0]}"

        def tab_label():
            tab_n[0] += 1
            return f"Table {prefix}{tab_n[0]}"

        # architecture image
        def arch(m):
            src = "assets/architecture-diagram.png"
            return (f'<figure class="figure arch"><img src="{src}" alt="Architecture of an Agentic AI Application">'
                    f'<figcaption><b>{fig_label()}.</b> Reference architecture of a production-ready Agentic AI application, '
                    f'showing the twelve numbered layers used throughout this document.</figcaption></figure>')
        h = re.sub(r"<p>\{\{arch\}\}</p>", arch, h)

        # diagrams
        def diagram(m):
            name, caption = m.group(1), html.unescape(m.group(2))
            svg = f"assets/diagrams/{name}.svg"
            mmd = (ROOT / "diagrams" / f"{name}.mmd").read_text()
            label = fig_label()
            src_block = ""
            if mode == "site":
                src_block = (f'<details class="mmd"><summary>Mermaid source <code>diagrams/{name}.mmd</code></summary>'
                             f'<pre><code>{html.escape(mmd)}</code></pre></details>')
            return (f'<figure class="figure diagram" id="fig-{name}"><div class="diagram-scroll">'
                    f'<img src="{svg}" alt="{html.escape(caption)}"></div>'
                    f'<figcaption><b>{label}.</b> {html.escape(caption)}</figcaption>{src_block}</figure>')
        h = re.sub(r"<p>\{\{diagram:([a-z0-9-]+)\|(.*?)\}\}</p>", diagram, h)

        # tables with captions
        def table(m):
            caption = html.unescape(m.group(1))
            tbl = m.group(2)
            return (f'<figure class="table-figure"><figcaption><b>{tab_label()}.</b> {html.escape(caption)}</figcaption>'
                    f'<div class="table-scroll">{tbl}</div></figure>')
        h = re.sub(r"<p>\{\{table:(.*?)\}\}</p>\s*(<table>.*?</table>)", table, h, flags=re.S)

        # numbered headings + ids + section list
        self.sections = []
        counters = [0, 0]

        def heading(m):
            level = int(m.group(1))
            attrs = m.group(2)
            text = m.group(3)
            if level == 2:
                counters[0] += 1; counters[1] = 0
                num = f"{prefix}{counters[0]}"
            else:
                counters[1] += 1
                num = f"{prefix}{counters[0]}.{counters[1]}"
            idm = re.search(r'id="([^"]+)"', attrs)
            hid = idm.group(1) if idm else slugify(text)
            self.sections.append((level, re.sub(r"<[^>]+>", "", text), hid, num))
            return f'<h{level} id="{hid}"><span class="num">{num}</span> {text}</h{level}>'
        h = re.sub(r"<h([23])([^>]*)>(.*?)</h\1>", heading, h)

        # site: links to other pages stay; pdf: convert page links to anchors
        if mode == "pdf":
            h = re.sub(r'href="([a-z-]+)\.html(#[^"]*)?"',
                       lambda m: f'href="#ch-{m.group(1)}{m.group(2) or ""}"'.replace("#ch-", "#ch-", 1), h)
            h = h.replace('src="assets/', 'src="../agentic-ai-architecture-documentation/assets/')
        return h


# --------------------------------------------------------------------------- #
def build():
    pages = [Page(p) for p in sorted(CONTENT.glob("*.md"))]
    pyg_css = HtmlFormatter(style="friendly").get_style_defs(".codehilite")

    site_css = (TEMPLATES / "site.css").read_text().replace("/*PYGMENTS*/", pyg_css)
    (ROOT / "assets" / "style.css").write_text(site_css)
    shutil.copy(TEMPLATES / "site.js", ROOT / "assets" / "site.js")
    shutil.copy(TEMPLATES / "favicon.svg", ROOT / "assets" / "icons" / "favicon.svg")
    shutil.copy(TEMPLATES / "logo.svg", ROOT / "assets" / "icons" / "logo.svg")

    site_tpl = (TEMPLATES / "page.html").read_text()
    search_index = []

    # expand all pages first (site mode) so nav can include sections
    expanded = {}
    for i, p in enumerate(pages):
        chapter_no = i + 1
        expanded[p.file] = p.expand(chapter_no, "site")
        # search index: one entry per section with its full text
        search_index.append({"p": p.title, "u": p.file, "t": p.title, "b": p.meta["summary"]})
        parts = re.split(r'(<h[23] id="[^"]+">.*?</h[23]>)', expanded[p.file])
        cur = None
        for part in parts:
            hm = re.match(r'<h[23] id="([^"]+)">(.*?)</h[23]>', part, re.S)
            if hm:
                cur = {"p": p.title, "u": f"{p.file}#{hm.group(1)}",
                       "t": re.sub(r"<[^>]+>", "", hm.group(2)).strip(), "b": ""}
                search_index.append(cur)
            elif cur is not None:
                txt = re.sub(r"<details.*?</details>", "", part, flags=re.S)
                txt = re.sub(r"<[^>]+>", " ", txt)
                txt = html.unescape(re.sub(r"\s+", " ", txt)).strip()
                cur["b"] = (cur["b"] + " " + txt).strip()[:6000]

    def nav_html(current):
        items = []
        for i, p in enumerate(pages):
            active = " active" if p.file == current else ""
            subs = "".join(f'<li><a href="{p.file}#{hid}">{html.escape(text)}</a></li>'
                           for level, text, hid, num in p.sections if level == 2)
            items.append(
                f'<li class="nav-item{active}"><a class="nav-link" href="{p.file}" style="--accent:{p.meta["accent"]}">'
                f'<span class="nav-num">{i+1}</span>{html.escape(p.meta["nav"])}</a>'
                f'<button class="nav-toggle" aria-label="Toggle sections"></button>'
                f'<ul class="nav-sub">{subs}</ul></li>')
        return "".join(items)

    for i, p in enumerate(pages):
        prev_p = pages[i - 1] if i > 0 else None
        next_p = pages[i + 1] if i < len(pages) - 1 else None
        toc = "".join(f'<li class="l{level}"><a href="#{hid}">{html.escape(text)}</a></li>'
                      for level, text, hid, num in p.sections)
        pager = ""
        if prev_p:
            pager += f'<a class="pager prev" href="{prev_p.file}"><span>Previous</span>{html.escape(prev_p.title)}</a>'
        if next_p:
            pager += f'<a class="pager next" href="{next_p.file}"><span>Next</span>{html.escape(next_p.title)}</a>'
        out = (site_tpl
               .replace("{{TITLE}}", html.escape(p.title))
               .replace("{{DOC_TITLE}}", DOC_TITLE)
               .replace("{{CHAPTER}}", html.escape(p.meta["chapter"]))
               .replace("{{CHAPTER_NO}}", str(i + 1))
               .replace("{{ACCENT}}", p.meta["accent"])
               .replace("{{SUMMARY}}", html.escape(p.meta["summary"]))
               .replace("{{NAV}}", nav_html(p.file))
               .replace("{{TOC}}", toc)
               .replace("{{CONTENT}}", expanded[p.file])
               .replace("{{PAGER}}", pager)
               .replace("{{VERSION}}", VERSION)
               .replace("{{DATE}}", DATE))
        (ROOT / p.file).write_text(out)

    (ROOT / "assets" / "search-index.js").write_text("window.SEARCH_INDEX=" + json.dumps(search_index) + ";")

    # ---- print HTML ------------------------------------------------------------ #
    print_tpl = (TEMPLATES / "print.html").read_text()
    exec_summary = render_markdown((TEMPLATES / "executive-summary.md").read_text())
    chapters_html = []
    toc_entries = []   # (level, num, text, anchor)
    for i, p in enumerate(pages):
        chapter_no = i + 1
        body = p.expand(chapter_no, "pdf")
        anchor = f"ch-{p.file.replace('.html', '')}"
        toc_entries.append((1, str(chapter_no), p.meta["chapter"], anchor))
        for level, text, hid, num in p.sections:
            if level == 2:
                toc_entries.append((2, num, text, hid))
        chapters_html.append(
            f'<section class="chapter" id="{anchor}" style="--accent:{p.meta["accent"]}">'
            f'<div class="chapter-head"><div class="chapter-kicker">Chapter {chapter_no}</div>'
            f'<h1 class="chapter-title"><span class="ch-num">{chapter_no}</span>{html.escape(p.meta["chapter"])}</h1>'
            f'<p class="chapter-summary">{html.escape(p.meta["summary"])}</p></div>{body}</section>')
    print_out = (print_tpl
                 .replace("{{EXEC_SUMMARY}}", exec_summary)
                 .replace("{{CHAPTERS}}", "".join(chapters_html))
                 .replace("{{TOC_ENTRIES}}", json.dumps(toc_entries))
                 .replace("{{VERSION}}", VERSION).replace("{{DATE}}", DATE)
                 .replace("/*PYGMENTS*/", pyg_css))
    Path("/home/claude/build/print.html").write_text(print_out)
    print(f"built {len(pages)} pages, {len(search_index)} search entries, {len(toc_entries)} toc entries")


if __name__ == "__main__":
    build()
