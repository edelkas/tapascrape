from tapascrape.tables import Table, as_markdown, as_text


def table() -> Table:
    t = Table("Forums", ["", "Shown", "Source"], note="A note.")
    t.add("Total", 1234, "Tapatalk")
    t.add("Lost | found", None, "Forumer")
    return t


def test_as_text():
    assert as_text([table()]) == "\n".join([
        "Forums",
        "======",
        "              Shown  Source",
        "------------  -----  --------",
        "Total         1,234  Tapatalk",
        "Lost | found         Forumer",
        "A note.",
        ""])


def test_as_markdown():
    assert as_markdown([table()], "Stats") == "\n".join([
        "# Stats",
        "",
        "## Forums",
        "",
        "|  | Shown | Source |",
        "| --- | ---: | --- |",
        "| Total | 1,234 | Tapatalk |",
        "| Lost \\| found |  | Forumer |",
        "",
        "A note.",
        ""])
