"""Small tables of numbers, printed as text or written as Markdown."""

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
        for row in table.rows:
            out.append("| " + " | ".join(escape(cell(v)) for v in row) + " |")
        out.append("")
        if table.note:
            out += [table.note, ""]
    return "\n".join(out)


def escape(text: str) -> str:
    return text.replace("|", "\\|")
