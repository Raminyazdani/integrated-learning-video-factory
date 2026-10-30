"""md_to_latex - minimal Markdown to LaTeX body converter for Phase 5B.

Converts content_md/package_NNN_extended_fa.md into a LaTeX BODY string
suitable for injection into templates/package_pdf_template.tex via the
{{BODY}} placeholder.

Supported MD constructs (anything else is passed through as escaped prose):
    # Heading 1      -> \section*{...}
    ## Heading 2     -> \subsection*{...}
    ### Heading 3    -> \subsubsection*{...}
    #### Heading 4   -> \paragraph{...}~\\
    ![alt](path)     -> \begin{center}\includegraphics[width=0.85\linewidth]{path}\end{center}
    | a | b | c |    -> tabular (auto column widths via X via tabularx)
    **bold**         -> \textbf{...}
    _italic_         -> \textit{...}
    `code`           -> \texttt{...}
    > blockquote     -> \begin{quote}...\end{quote}
    - item           -> itemize
    1. item          -> enumerate
    [text](url)      -> \href{url}{text}

LaTeX special characters in prose are escaped: & % # $ _ { } ~ ^ \

For the image-anchor rule: image lines are converted in place, so the
MD ordering "image first, explanation second" is preserved automatically.
"""
from __future__ import annotations

import re
from typing import List


# --- escaping --------------------------------------------------------------

_ESCAPE_TABLE = {
    "\\": r"\textbackslash{}",
    "{":    r"\{",
    "}":    r"\}",
    "&":    r"\&",
    "%":    r"\%",
    "#":    r"\#",
    "$":    r"\$",
    "_":    r"\_",
    "~":    r"\textasciitilde{}",
    "^":    r"\textasciicircum{}",
}


def escape_latex(s: str) -> str:
    """Escape LaTeX special characters in plain text (not in code/math)."""
    out = []
    i = 0
    while i < len(s):
        ch = s[i]
        if ch == "\\":
            out.append(r"\textbackslash{}")
        elif ch in "{}&%#$_":
            out.append("\\" + ch)
        elif ch == "~":
            out.append(r"\textasciitilde{}")
        elif ch == "^":
            out.append(r"\textasciicircum{}")
        else:
            out.append(ch)
        i += 1
    return "".join(out)


# --- inline transforms ----------------------------------------------------

_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")
_ITALIC_RE = re.compile(r"(?<![A-Za-z0-9_])_([^_\n]+)_(?![A-Za-z0-9_])")
_CODE_RE = re.compile(r"`([^`]+)`")


def _inline_apply(text: str) -> str:
    """Apply inline transforms to one paragraph or table cell.

    Order matters: code-spans are extracted first so their contents are
    not LaTeX-escaped or further transformed.
    """
    # 1) Extract code spans into placeholders.
    placeholders: list = []

    def _stash_code(m):
        idx = len(placeholders)
        placeholders.append("\\texttt{" + escape_latex(m.group(1)) + "}")
        return f"\x00CODE{idx}\x01"

    text = _CODE_RE.sub(_stash_code, text)

    # 2) Stash links similarly so their visible text and URL are handled.
    def _stash_link(m):
        idx = len(placeholders)
        visible = escape_latex(m.group(1))
        url = m.group(2)
        if url.startswith("http"):
            placeholders.append(f"\\href{{{url}}}{{{visible}}}")
        else:
            # not a real URL - keep just the visible text
            placeholders.append(visible)
        return f"\x00CODE{idx}\x01"

    text = _LINK_RE.sub(_stash_link, text)

    # 3) Escape LaTeX specials on the surviving prose.
    text = escape_latex(text)

    # 4) Bold and italic. Use cleaned text (after escaping) but the markers
    # were escaped too; switch them back temporarily.
    text = text.replace(r"\_", "\x02UND")  # protect intentional underscores
    text = re.sub(r"\*\*([^*]+)\*\*", r"\\textbf{\1}", text)
    # Italic: a single _ wrapping non-underscore content
    text = re.sub(r"(^|\s)_([^_]+)_(\s|$|[.,;:!?])", r"\1\\textit{\2}\3", text)
    text = text.replace("\x02UND", r"\_")

    # 5) Restore placeholders.
    def _restore(m):
        return placeholders[int(m.group(1))]

    text = re.sub(r"\x00CODE(\d+)\x01", _restore, text)

    return text


# --- block parsing --------------------------------------------------------

_IMG_RE = re.compile(r"^!\[([^\]]*)\]\(([^)]+)\)\s*$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)+\|?\s*$")
_OL_ITEM_RE = re.compile(r"^\s*(\d+)\.\s+(.*)$")
_UL_ITEM_RE = re.compile(r"^\s*[-*]\s+(.*)$")
_QUOTE_RE = re.compile(r"^\s*>\s?(.*)$")


def _split_table_row(row: str) -> list:
    parts = row.strip().strip("|").split("|")
    return [p.strip() for p in parts]


def _emit_table(headers: list, rows: list) -> list:
    n = max(len(headers), max((len(r) for r in rows), default=0))
    # Pad short rows.
    headers = headers + [""] * (n - len(headers))
    rows = [r + [""] * (n - len(r)) for r in rows]
    spec = "|" + "|".join(["p{" + f"{0.95/n:.3f}" + "\\linewidth}"] * n) + "|"
    out: list = []
    out.append(r"\begin{center}")
    out.append(r"\small")
    out.append("\\begin{tabular}{" + spec + "}")
    out.append(r"\hline")
    out.append(" & ".join(_inline_apply(h) for h in headers) + r" \\")
    out.append(r"\hline")
    for r in rows:
        out.append(" & ".join(_inline_apply(c) for c in r) + r" \\")
        out.append(r"\hline")
    out.append(r"\end{tabular}")
    out.append(r"\end{center}")
    return out


def _emit_image(alt: str, path: str) -> list:
    return [
        r"\begin{center}",
        f"\\includegraphics[width=0.85\\linewidth,keepaspectratio]{{{path}}}",
        r"\end{center}",
    ]


def _emit_heading(level: int, text: str) -> list:
    cmd = {
        1: r"\section*",
        2: r"\subsection*",
        3: r"\subsubsection*",
        4: r"\paragraph",
    }.get(level, r"\paragraph")
    rendered = _inline_apply(text)
    if level == 4:
        return [cmd + "{" + rendered + r"}~\\"]
    return [cmd + "{" + rendered + "}"]


# --- main converter -------------------------------------------------------

def md_to_latex_body(md: str) -> str:
    """Convert a Markdown body to a LaTeX body string."""
    lines = md.splitlines()
    out: list = []
    i = 0
    n_images = 0

    in_ul = False
    in_ol = False
    in_quote_buffer: list = []

    def flush_lists():
        nonlocal in_ul, in_ol
        if in_ul:
            out.append(r"\end{itemize}")
            in_ul = False
        if in_ol:
            out.append(r"\end{enumerate}")
            in_ol = False

    def flush_quote():
        nonlocal in_quote_buffer
        if in_quote_buffer:
            out.append(r"\begin{quote}")
            out.append(_inline_apply(" ".join(in_quote_buffer)))
            out.append(r"\end{quote}")
            in_quote_buffer = []

    while i < n_lines_safe(lines):
        line = lines[i]
        stripped = line.strip()

        # blank line -> close lists/quotes, emit \par
        if not stripped:
            flush_lists()
            flush_quote()
            out.append("")
            i += 1
            continue

        # image
        m_img = _IMG_RE.match(stripped)
        if m_img:
            flush_lists()
            flush_quote()
            alt, path = m_img.group(1), m_img.group(2)
            out.extend(_emit_image(alt, path))
            n_images += 1
            i += 1
            continue

        # heading
        if stripped.startswith("#"):
            flush_lists()
            flush_quote()
            level = 0
            while level < len(stripped) and stripped[level] == "#":
                level += 1
            text = stripped[level:].strip()
            out.extend(_emit_heading(level, text))
            i += 1
            continue

        # table - detect header row followed by separator
        if "|" in stripped and i + 1 < n_lines_safe(lines) and _TABLE_SEP_RE.match(lines[i+1].strip()):
            flush_lists()
            flush_quote()
            headers = _split_table_row(stripped)
            i += 2  # skip header and separator
            rows: list = []
            while i < n_lines_safe(lines) and "|" in lines[i] and lines[i].strip():
                rows.append(_split_table_row(lines[i]))
                i += 1
            out.extend(_emit_table(headers, rows))
            continue

        # blockquote
        m_q = _QUOTE_RE.match(line)
        if m_q:
            flush_lists()
            in_quote_buffer.append(m_q.group(1))
            i += 1
            continue
        else:
            flush_quote()

        # ordered list
        m_ol = _OL_ITEM_RE.match(line)
        if m_ol:
            if in_ul:
                out.append(r"\end{itemize}")
                in_ul = False
            if not in_ol:
                out.append(r"\begin{enumerate}")
                in_ol = True
            out.append(r"\item " + _inline_apply(m_ol.group(2)))
            i += 1
            continue

        # unordered list
        m_ul = _UL_ITEM_RE.match(line)
        if m_ul:
            if in_ol:
                out.append(r"\end{enumerate}")
                in_ol = False
            if not in_ul:
                out.append(r"\begin{itemize}")
                in_ul = True
            out.append(r"\item " + _inline_apply(m_ul.group(1)))
            i += 1
            continue

        # plain paragraph (single line at a time; consecutive non-blank
        # lines get joined by \par)
        flush_lists()
        out.append(_inline_apply(stripped))
        i += 1

    flush_lists()
    flush_quote()
    return "\n".join(out)


def n_lines_safe(lines):
    return len(lines)


# --- image-anchor count (used by validation) -----------------------------

def count_image_anchors(md: str) -> int:
    """Count Markdown image anchors of the form ![alt](path)."""
    return len(re.findall(r"^!\[[^\]]*\]\([^)]+\)\s*$", md, flags=re.MULTILINE))
