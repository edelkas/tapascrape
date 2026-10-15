"""Small tables of numbers, printed as text or written as Markdown or HTML.

A row label's leading spaces indent it (two per level): a row breaking down
the one above.
"""

import html
import re
from dataclasses import dataclass, field


@dataclass
class Table:
    title: str
    columns: list[str]               # the first heads the row labels
    rows: list[list] = field(default_factory=list)  # a label, then ints or text
    note: str = ""                   # said below the table

    def add(self, label: str, *values) -> None:
        self.rows.append([label, *values])


def cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def numeric(table: Table) -> list[bool]:
    """Which columns hold numbers (aligned to the right); the labels' never does."""
    return [i > 0 and all(isinstance(row[i], (int, float)) or row[i] is None or
                          str(row[i]).replace(",", "").replace(".", "").isdigit() for row in table.rows)
            for i in range(len(table.columns))]


def as_text(tables: list[Table]) -> str:
    """Aligned columns, numbers to the right."""
    out = []
    for table in tables:
        cells = [[cell(v) for v in row] for row in table.rows]
        widths = [max(len(c) for c in column) for column in zip(table.columns, *cells)]
        right = numeric(table)
        out.append(table.title)
        out.append("=" * len(table.title))
        lines = [table.columns] + cells
        for i, line in enumerate(lines):
            out.append("  ".join(text.rjust(width) if right[j] else text.ljust(width)
                                 for j, (text, width) in enumerate(zip(line, widths))).rstrip())
            if i == 0:
                out.append("  ".join("-" * width for width in widths))
        if table.note:
            out.append(table.note)
        out.append("")
    return "\n".join(out)


def as_markdown(tables: list[Table], title: str | None = None) -> str:
    out = [f"# {title}", ""] if title else []
    for table in tables:
        out += [f"## {table.title}", ""]
        out.append("| " + " | ".join(escape(c) for c in table.columns) + " |")
        out.append("|" + "|".join(" ---: " if right else " --- " for right in numeric(table)) + "|")
        for label, *values in table.rows:
            label = "&nbsp;&nbsp;" * level(label) + escape(label.lstrip(" "))
            out.append("| " + " | ".join([label] + [escape(cell(v)) for v in values]) + " |")
        out.append("")
        if table.note:
            out += [table.note, ""]
    return "\n".join(out)


def as_html(tables: list[Table]) -> str:
    """Each table under an <h2>, with the classes of the site's stylesheet (table.stats)."""
    out = []
    for table in tables:
        right = numeric(table)
        head = "".join(f'<th class="count">{html.escape(c)}</th>' if r else f"<th>{html.escape(c)}</th>"
                       for c, r in zip(table.columns, right))
        rows = []
        for label, *values in table.rows:
            cells = ['<span class="indent"></span>' * level(label) + code(html.escape(label.lstrip(" ")))]
            cells += [html.escape(cell(v)) for v in values]
            rows.append("<tr>" + "".join(f'<td class="count">{c}</td>' if r else f"<td>{c}</td>"
                                         for c, r in zip(cells, right)) + "</tr>")
        out.append(f'<h2>{html.escape(table.title)}</h2>\n<table class="stats"><thead><tr>{head}</tr></thead>'
                   "<tbody>\n" + "\n".join(rows) + "\n</tbody></table>")
        if table.note:
            out.append(f'<p class="stats-note">{code(html.escape(table.note, quote=False))}</p>')
    return "\n".join(out)


def code(text: str) -> str:
    """`backticks` (in escaped HTML) -> <code>."""
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", text)


def level(label: str) -> int:
    """How deep a row label is indented."""
    return (len(label) - len(label.lstrip(" "))) // 2


def escape(text: str) -> str:
    return text.replace("|", "\\|")
