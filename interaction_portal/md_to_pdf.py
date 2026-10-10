#!/usr/bin/env python3
"""
Convert Markdown documents (such as README.md) into styled PDF files.

Usage:
    python md_to_pdf.py [input_markdown] [output_pdf]

Defaults:
    input_markdown: README.md
    output_pdf: README.pdf
"""

import argparse
import re
import sys
from pathlib import Path

import markdown
from xhtml2pdf import pisa


def preprocess_markdown(text: str) -> str:
    """Preprocess Markdown text for clean, universal PDF rendering."""
    # Convert inline LaTeX math like $3 \times 3 + 1$ into clean text
    text = re.sub(r"\$3\s*\\times\s*3\s*\+\s*1\$", "3 x 3 + 1", text)
    text = re.sub(r"\$([^\$]+)\$", r"\1", text)

    # Normalize special unicode symbols that Type-1 fonts do not contain
    unicode_replacements = {
        "├──": "|-- ",
        "└──": "\\-- ",
        "├─": "|- ",
        "└─": "\\- ",
        "│": "|",
        "─": "-",
        "←": "<-",
        "→": "->",
        "×": "x",
        "•": "*",
        "–": "-",
        "—": "--",
    }
    for char, rep in unicode_replacements.items():
        text = text.replace(char, rep)

    # Convert GitHub alert callouts into styled blocks
    alert_pattern = re.compile(
        r"> \[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\]\s*\n((?:>.*(?:\n|$))+)",
        re.MULTILINE,
    )

    def alert_sub(match):
        alert_type = match.group(1).lower()
        title = alert_type.capitalize()
        lines = match.group(2).strip().splitlines()
        clean_lines = [re.sub(r"^>\s?", "", line) for line in lines]
        body = "\n".join(clean_lines)
        return (
            f'\n<div class="callout callout-{alert_type}">\n'
            f'<div class="callout-title"><b>{title}</b></div>\n'
            f'<div class="callout-body">{body}</div>\n'
            f'</div>\n'
        )

    text = alert_pattern.sub(alert_sub, text)

    return text


def build_html_document(body_html: str, title: str = "Documentation") -> str:
    """Wrap body HTML in an A4-optimized styled template."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
    @page {{
        size: a4 portrait;
        margin: 20mm 15mm 20mm 15mm;
        @bottom-left {{
            content: "Interaction Portal (v2) - Documentation";
            font-size: 8pt;
            color: #718096;
            font-family: Helvetica, sans-serif;
        }}
        @bottom-right {{
            content: "Page " counter(page);
            font-size: 8pt;
            color: #718096;
            font-family: Helvetica, sans-serif;
        }}
    }}

    body {{
        font-family: Helvetica, sans-serif;
        font-size: 8.5pt;
        line-height: 1.4;
        color: #2d3748;
    }}

    h1 {{
        font-size: 18pt;
        color: #1a202c;
        border-bottom: 2px solid #2b6cb0;
        padding-bottom: 6px;
        margin-top: 0;
        margin-bottom: 12px;
    }}

    h2 {{
        font-size: 13pt;
        color: #2b6cb0;
        border-bottom: 1px solid #e2e8f0;
        padding-bottom: 4px;
        margin-top: 16px;
        margin-bottom: 8px;
    }}

    h3 {{
        font-size: 10.5pt;
        color: #2d3748;
        margin-top: 12px;
        margin-bottom: 6px;
    }}

    h4 {{
        font-size: 9.5pt;
        color: #4a5568;
        margin-top: 8px;
        margin-bottom: 4px;
    }}

    p {{
        margin-top: 4px;
        margin-bottom: 6px;
    }}

    ul, ol {{
        margin-top: 4px;
        margin-bottom: 6px;
        padding-left: 18px;
    }}

    li {{
        margin-bottom: 3px;
    }}

    code {{
        font-family: Courier, monospace;
        font-size: 8pt;
        background-color: #f7fafc;
        color: #c53030;
    }}

    pre {{
        font-family: Courier, monospace;
        font-size: 7.5pt;
        background-color: #f8fafc;
        border: 1px solid #e2e8f0;
        padding: 6px 8px;
        margin: 6px 0;
        line-height: 1.25;
    }}

    pre code {{
        background-color: transparent;
        color: #2d3748;
        padding: 0;
    }}

    table {{
        width: 100%;
        margin: 8px 0;
        font-size: 8pt;
    }}

    th {{
        background-color: #edf2f7;
        color: #2d3748;
        font-weight: bold;
        text-align: left;
        padding: 5px 6px;
        border: 1px solid #cbd5e0;
    }}

    td {{
        padding: 4px 6px;
        border: 1px solid #e2e8f0;
        vertical-align: top;
    }}

    tr:nth-child(even) td {{
        background-color: #f7fafc;
    }}

    hr {{
        border: 0;
        border-top: 1px solid #e2e8f0;
        margin: 12px 0;
    }}

    .callout {{
        border-left: 4px solid #3182ce;
        background-color: #ebf8ff;
        padding: 6px 10px;
        margin: 8px 0;
    }}

    .callout-tip {{
        border-left-color: #38a169;
        background-color: #f0fff4;
    }}

    .callout-warning {{
        border-left-color: #dd6b20;
        background-color: #fffaf0;
    }}

    .callout-important {{
        border-left-color: #e53e3e;
        background-color: #fff5f5;
    }}

    .callout-title {{
        margin: 0 0 3px 0;
        font-size: 8.5pt;
    }}

    .callout-tip .callout-title {{
        color: #276749;
    }}

    .callout-warning .callout-title {{
        color: #c05621;
    }}

    .callout-important .callout-title {{
        color: #c53030;
    }}

    .callout-body {{
        font-size: 8pt;
    }}
</style>
</head>
<body>
{body_html}
</body>
</html>
"""


def convert_md_to_pdf(input_md_path: Path, output_pdf_path: Path) -> bool:
    """Convert Markdown file to a styled PDF."""
    if not input_md_path.exists():
        print(f"Error: Input file '{input_md_path}' does not exist.", file=sys.stderr)
        return False

    print(f"Reading: {input_md_path.resolve()}")
    with open(input_md_path, "r", encoding="utf-8") as f:
        md_content = f.read()

    processed_md = preprocess_markdown(md_content)

    html_body = markdown.markdown(
        processed_md,
        extensions=[
            "tables",
            "fenced_code",
            "sane_lists",
        ],
    )

    title = input_md_path.stem.replace("_", " ").title()
    full_html = build_html_document(html_body, title=title)

    print(f"Generating PDF: {output_pdf_path.resolve()}")
    with open(output_pdf_path, "wb") as pdf_file:
        pisa_status = pisa.CreatePDF(full_html, dest=pdf_file, encoding="utf-8")

    if pisa_status.err:
        print(f"Error: PDF generation encountered {pisa_status.err} issue(s).", file=sys.stderr)
        return False

    size_kb = output_pdf_path.stat().st_size / 1024
    print(f"Success! Generated '{output_pdf_path.name}' ({size_kb:.1f} KB)")
    return True


def main():
    parser = argparse.ArgumentParser(description="Convert Markdown to styled PDF.")
    parser.add_argument(
        "input",
        nargs="?",
        default="README.md",
        help="Path to input Markdown file (default: README.md)",
    )
    parser.add_argument(
        "output",
        nargs="?",
        default="README.pdf",
        help="Path to output PDF file (default: README.pdf)",
    )

    args = parser.parse_args()
    input_path = Path(args.input)
    output_path = Path(args.output)

    success = convert_md_to_pdf(input_path, output_path)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
