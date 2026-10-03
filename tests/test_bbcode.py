"""HTML -> BBCode reconstruction; the HTML snippets are the board's real rendering."""

import pytest

from tapascrape.parse.bbcode import html_to_bbcode

SPOILER = ('<dl class="codebox spoiler"><dt><a href="javascript:void%280%29;" onclick="..."><span>[+]'
           '</span> Spoiler</a></dt><dd style="display:none">{}</dd></dl>')
CODEBOX = ('<div class="codebox"><p class="codebox-header"><span class="codebox-label">Code</span>'
           '<button class="codebox-copy" type="button"><i class="icon fa fa-clone"></i>'
           '<span class="codebox-copy-label">Copy code</span></button></p><pre><code>{}</code></pre></div>')


@pytest.mark.parametrize("html, source", [
    # line breaks: "<br/>\n" is one break
    ("crap.<br/>\nyou posted it<br/>\n..", "crap.\nyou posted it\n.."),
    # simple styles, entities back to text
    ("<strong>Bold</strong> &amp; <em>it</em> <span style=\"text-decoration:underline\">u</span> "
     "<span style=\"text-decoration:line-through\">s</span> <sup>up</sup>",
     "[b]Bold[/b] & [i]it[/i] [u]u[/u] [s]s[/s] [sup]up[/sup]"),
    ('<span style="font-family:Impact"><span style="font-size:13%;line-height:normal">'
     '<span style="color:black">Hi</span></span></span>',
     "[font=Impact][size=13][color=black]Hi[/color][/size][/font]"),
    # malformed tags are rendered as text and stay as they were
    ("[color=RED'&gt;]Red[/color] = route.", "[color=RED'>]Red[/color] = route."),
    # links: bare and shortened URLs, www. autolinks, named links
    ('<a class="postlink" href="http://numa.notdot.net/browse?sort=created&amp;author=X-43bfn">'
     'http://numa.notdot.net/browse?sort=crea ... or=X-43bfn</a>',
     "http://numa.notdot.net/browse?sort=created&author=X-43bfn"),
    ('(like <a class="postlink" href="http://www.filefront.com">www.filefront.com</a> maybe)',
     "(like www.filefront.com maybe)"),
    ('1. <a class="postlink" href="http://numa.notdot.net">NUMA</a>',
     "1. [url=http://numa.notdot.net]NUMA[/url]"),
    ('<img class="lazyload postimage" data-src="http://static.yuku.com/domain/bypass/images/tongue.gif"/>',
     "[img]http://static.yuku.com/domain/bypass/images/tongue.gif[/img]"),
    # quotes, cited or not
    ('<blockquote class="uncited"><div>Whatever</div></blockquote>', "[quote]Whatever[/quote]"),
    ("<blockquote><div><cite>sweep, Oct 4 2005, 03:57 PM wrote:</cite>Whatever</div></blockquote>",
     '[quote="sweep, Oct 4 2005, 03:57 PM"]Whatever[/quote]'),
    # migrated quote tables are kept as tables
    ('<table class="post_content_table"><tbody><tr><td><strong>QUOTE</strong> (Igi)</td></tr>'
     "<tr><td> I know! </td></tr></tbody></table>\nwow",
     "[table][tr][td][b]QUOTE[/b] (Igi)[/td][/tr][tr][td] I know! [/td][/tr][/table]\nwow"),
    # blocks swallow the line break before them
    ("lines...<br/>\n" + SPOILER.format("spoiler stuff") + "\nor...",
     "lines...\n\n[spoiler]spoiler stuff[/spoiler]\nor..."),
    ("is:<br/>\n<br/>\n" + CODEBOX.format("[url]x[/url]") + "\nAnd",
     "is:\n\n\n[code][url]x[/url][/code]\nAnd"),
    # lists
    ("<ul><li>one</li><li>two <em>2</em></li></ul>", "[list][*]one[*]two [i]2[/i][/list]"),
    ('<ol style="list-style-type:decimal"><li>a</li> <li>b</li></ol>', "[list=1][*]a [*]b[/list]"),
    ('<div align="center"><strong>Appendix</strong></div>', "[center][b]Appendix[/b][/center]"),
    ("<sup>x</sup>\n<hr/>It's", "[sup]x[/sup]\n[hr]It's"),
])
def test_html_to_bbcode(html, source):
    assert html_to_bbcode(html) == source
