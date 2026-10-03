"""Website post HTML -> BBCode source (best effort).

For posts whose source the API can't return. The board's renderer (phpBB's
s9e TextFormatter) turns well-formed tags into HTML and leaves malformed ones
(e.g. Yuku's `[color=RED'>]`) as literal text, so reversing the HTML gives
back most sources. What the HTML can't tell is approximated:

- a link whose text is its URL becomes a bare URL (the renderer links those),
  and [url='x'] (Yuku's quoted form) comes back as [url=x];
- quotes become [quote="author"] whatever form the author's name had;
- spoiler titles aren't rendered, so spoilers come back untitled;
- the renderer drops the line break right before a block (quote, list,
  table...) that follows a rendered line break; it's restored.
"""

import re

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

LIST_TYPES = {"decimal": "1", "lower-alpha": "a", "upper-alpha": "A",
              "lower-roman": "i", "upper-roman": "I"}
BLOCKS = {"table", "dl", "ul", "ol", "blockquote"}
SIMPLE = {"strong": "b", "b": "b", "em": "i", "i": "i", "u": "u", "s": "s",
          "strike": "s", "sup": "sup", "sub": "sub"}
QUOTE_CITE = re.compile(r"(.*) wrote:\s*$", re.DOTALL)


def style_of(tag: Tag) -> dict[str, str]:
    style = {}
    for part in tag.get("style", "").split(";"):
        name, _, value = part.partition(":")
        if value.strip():
            style[name.strip().lower()] = value.strip()
    return style


def is_shortened(text: str, url: str) -> bool:
    """phpBB shows long URLs as "start ... end"."""
    start, sep, end = text.partition(" ... ")
    return bool(sep) and url.startswith(start) and url.endswith(end)


class _Writer:
    def __init__(self):
        self.parts: list[str] = []
        self.after_br = False  # the last thing written is a rendered line break

    def text(self, text: str) -> None:
        if self.after_br and text.startswith("\n"):
            text = text[1:]  # "<br/>\n" is a single line break
        if text:
            self.parts.append(text)
            self.after_br = False

    def tag(self, markup: str) -> None:
        self.parts.append(markup)
        self.after_br = False

    def br(self) -> None:
        self.parts.append("\n")
        self.after_br = True

    def block_start(self) -> None:
        if self.after_br:
            self.parts.append("\n")
        self.after_br = False

    def result(self) -> str:
        return "".join(self.parts)


def html_to_bbcode(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    out = _Writer()
    _children(soup, out)
    return out.result()


def _children(node: Tag, out: _Writer) -> None:
    for child in node.children:
        if isinstance(child, Comment):
            continue
        if isinstance(child, NavigableString):
            out.text(str(child))
        elif isinstance(child, Tag):
            _element(child, out)


def _wrap(node: Tag, out: _Writer, open_tag: str, close_tag: str) -> None:
    out.tag(open_tag)
    _children(node, out)
    out.tag(close_tag)


def _element(el: Tag, out: _Writer) -> None:
    name = el.name
    classes = el.get("class", [])
    if name in ("script", "style", "button"):
        return
    if name in BLOCKS or (name == "div" and ("codebox" in classes or el.get("align"))):
        out.block_start()

    if name == "br":
        out.br()
    elif name in SIMPLE:
        _wrap(el, out, f"[{SIMPLE[name]}]", f"[/{SIMPLE[name]}]")
    elif name == "span":
        _span(el, out)
    elif name == "a":
        _link(el, out)
    elif name == "img":
        if "smilies" in classes and el.get("alt"):
            out.text(el["alt"])
        elif src := el.get("data-src") or el.get("src"):
            out.tag(f"[img]{src}[/img]")
    elif name == "hr":
        out.tag("[hr]")
    elif name == "dl" and "spoiler" in classes:
        body = el.find("dd")
        out.tag("[spoiler]")
        if body is not None:
            _children(body, out)
        out.tag("[/spoiler]")
    elif name == "blockquote":
        _quote(el, out)
    elif name == "div" and "codebox" in classes:
        code = el.find("code")
        out.tag("[code]")
        out.text(code.get_text() if code is not None else "")
        out.tag("[/code]")
    elif name == "div" and el.get("align"):
        align = el["align"].lower()
        tag = "center" if align == "center" else f"align={align}"
        _wrap(el, out, f"[{tag}]", f"[/{tag.split('=')[0]}]")
    elif name == "table":
        _wrap(el, out, "[table]", "[/table]")
    elif name == "tr":
        _wrap(el, out, "[tr]", "[/tr]")
    elif name in ("td", "th"):
        _wrap(el, out, "[td]", "[/td]")
    elif name in ("ul", "ol"):
        kind = LIST_TYPES.get(style_of(el).get("list-style-type", ""))
        if name == "ol" and kind is None:
            kind = "1"
        _wrap(el, out, f"[list={kind}]" if kind else "[list]", "[/list]")
    elif name == "li":
        out.tag("[*]")
        _children(el, out)
    else:
        _children(el, out)  # tbody, p, div, unknown wrappers


def _span(el: Tag, out: _Writer) -> None:
    style = style_of(el)
    opened = []
    if "font-size" in style:
        opened.append(("size", style["font-size"].rstrip("%")))
    if "color" in style:
        opened.append(("color", style["color"]))
    if "font-family" in style:
        opened.append(("font", style["font-family"]))
    decoration = style.get("text-decoration", "")
    if "underline" in decoration:
        opened.append(("u", None))
    if "line-through" in decoration:
        opened.append(("s", None))
    for tag, value in opened:
        out.tag(f"[{tag}={value}]" if value is not None else f"[{tag}]")
    _children(el, out)
    for tag, _ in reversed(opened):
        out.tag(f"[/{tag}]")


def _link(el: Tag, out: _Writer) -> None:
    href = el.get("href", "")
    if not href or href.startswith("javascript:"):
        _children(el, out)
        return
    text = el.get_text()
    if el.find(True) is None and (text == href or is_shortened(text, href)):
        out.text(href)  # a bare URL; the renderer links it again
    elif el.find(True) is None and href == "http://" + text:
        out.text(text)  # a bare "www." address, linked the same way
    else:
        _wrap(el, out, f"[url={href}]", "[/url]")


def _quote(el: Tag, out: _Writer) -> None:
    body = el.find("div", recursive=False) or el
    cite = body.find("cite", recursive=False)
    author = None
    if cite is not None:
        match = QUOTE_CITE.match(cite.get_text())
        author = match.group(1) if match else cite.get_text()
        cite.extract()
    out.tag(f'[quote="{author}"]' if author is not None else "[quote]")
    _children(body, out)
    out.tag("[/quote]")
