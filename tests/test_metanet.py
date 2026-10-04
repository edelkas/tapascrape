from collections import Counter
from datetime import datetime

import pytest

from tapascrape.boards.metanet import parse
from tapascrape.boards.metanet.dump import archive_position, is_error, kind_of, query_of
from tapascrape.boards.metanet.link import increasing, link
from tapascrape.boards.metanet.schema import TABLES
from tapascrape.db import open_database

# Trimmed from real pages of the dump (people made up).
MEMBER_POST = """<!--Begin Msg Number 388658-->
    <table width='100%' border='0' cellspacing='1' cellpadding='3'>
    <tr>
      <td valign='middle' class='row4' width="1%"><a name='entry388658'></a><span class='normalname'><a href='http://metanet.2.forumer.com/index.php?showuser=5765'>ninja&#33;</a></span></td>
        <td class='row4' valign='top' width="99%">
        <span class='postdetails'><b><a title="Show the link to this post" href="#" onclick="link_to_post(388658); return false;" style="text-decoration:underline">Posted:</a></b> August 26, 2007 08:59 am</span>
        <a href='http://metanet.2.forumer.com/index.php?act=Post&amp;CODE=06&amp;f=19&amp;t=19141&amp;p=388658'><img src='q.png' alt='Quote Post' /></a>
      </td>
    </tr>
    <tr>
      <td valign='top' class='post2'>
        <span class='postdetails'><a href="index.php?showuser=5765"><img src='http://2.forumer.com/uploads/metanet/av-5765.jpg' border='0' width='64' height='64' alt='' /></a><br /><br />
        Advanced Member<br />
        <img src='http://2.forumer.com/uploads/metanet/post-12-1158208212.png' border='0'  alt='*' /><br /><br />
        Group: Members<br />
        Posts: 1,085<br />
        Member No.: 5765<br />
        Joined: December 12, 2006<br />
<script type="text/javascript" language="javascript">
if("Australia"=="") { document.write("Unspecified"); }
</script></span>
        <!--$ author[field_1]-->
      </td>
      <td width='100%' valign='top' class='post2'>
        <!-- THE POST 388658 -->
        <div class='postcolor'> Hello <!--emo&:(--><img src='http://2.forumer.com/html/emoticons/sad.gif' border='0' style='vertical-align:middle' alt='sad.gif' /><!--endemo--> <br /><br /><span class='edit'>This post has been edited by <b>ninja&#33;</b> on August 27, 2007 10:00 pm</span> <!--TEMPLATE: skin_topic, Template Part: Show_attachments-->
<br />
<strong><span class='edit'>Attached File ( Number of downloads: 2,709 )</span></strong>
<br />
<a href='http://metanet.2.forumer.com/index.php?s=4cc7&amp;act=Attach&amp;type=post&amp;id=388658' title='Download attachment' target='_blank'><img src='http://2.forumer.com/html/mime_types/zip.gif' border='0' alt='Attached File' /></a>
&nbsp;<a href='http://metanet.2.forumer.com/index.php?s=4cc7&amp;act=Attach&amp;type=post&amp;id=388658' title='Download attachment' target='_blank'>N_side_scroller.zip</a></div>
        <!--TEMPLATE: skin_global, Template Part: signature_separator-->
<br /><br />--------------------<br />
<div class='signature'>my <b>sig</b></div>
        <!-- THE POST -->
      </td>
    </tr>
    </table>
"""

GUEST_POST_2005 = """<!--Begin Msg Number 96564-->
      <td valign='middle' class='row4' width="1%"><a name='entry96564'></a><span class='unreg'>visitor</span></td>
        <span class='postdetails'><b><a title="Show the link to this post" href="#" onclick="link_to_post(96564); return false;">Posted on</a></b> Oct 2 2005, 04:08 AM</span>
        <span class='postdetails'><a href="index.php?showuser=0"></a><br /><br />
        Unregistered<br />
        <!--$ author[field_1]-->
        <!-- THE POST 96564 -->
        <div class='postcolor'> I actually opt for jam <!--TEMPLATE: skin_topic, Template Part: Show_attachments_img-->
<strong><span class='edit'>Attached Image</span></strong>
<img src='http://2.forumer.com/uploads/metanet/post-19-1174558319.jpg' class='attach' alt='Attached Image' /></div>
        <!-- THE POST -->
"""

TOPIC_PAGE = f"""<html><head><title>Metanet Forums -> Jelly</title></head><body>
<div class='maintitle'><img src='style_images/Invision_Power_Board/nav_m.gif' border='0'  alt='>' width='8' height='8' />&nbsp;<b>The world wants to know...</b>, Jelly or jam?</div>
	<!--TEMPLATE: skin_poll, Template Part: poll_header-->
<table><tr>
 <td colspan='3' align='center'><b>Which is better, jelly or jam?</b></td>
</tr><tr>
    <td class='row1'>Jelly</td>
    <td class='row1'> [ <b>5</b> ] </td>
</tr><tr>
    <td class='row1'>Jam</td>
    <td class='row1'> [ <b>10</b> ] </td>
</tr><tr><td class='row1' colspan='3' align='center'><strong>Total Votes: 15</strong></td></tr>
<!--TEMPLATE: skin_poll, Template Part: ShowPoll_footer-->
<a href='http://metanet.2.forumer.com/index.php?act=Track&amp;f=5&amp;t=5031'>Track this topic</a>
{MEMBER_POST}{GUEST_POST_2005}
Powered by forumer.com</body></html>"""


def test_topic_page():
    page = parse.topic_page(TOPIC_PAGE)
    assert (page.topic_id, page.forum_id) == (5031, 5)
    assert (page.title, page.description) == ("The world wants to know...", "Jelly or jam?")
    assert page.poll == {"question": "Which is better, jelly or jam?",
                         "options": [["Jelly", 5], ["Jam", 10]], "votes": 15}
    member, guest = page.posts
    assert (member.id, member.topic_id, member.forum_id) == (388658, 19141, 19)
    assert member.posted_at == datetime(2007, 8, 26, 8, 59)
    assert member.author == parse.Author(5765, "ninja!", "http://2.forumer.com/uploads/metanet/av-5765.jpg",
                                         "Advanced Member", "Members", 1085, datetime(2006, 12, 12),
                                         "Australia")
    assert member.html.startswith("Hello <!--emo&:(-->") and "edited" not in member.html
    assert member.html.endswith("<br /><br />")
    assert (member.edited_by, member.edited_at) == ("ninja!", datetime(2007, 8, 27, 22, 0))
    assert member.attachments == [parse.Attached("file", 388658, "N_side_scroller.zip", 2709)]
    assert member.signature == "my <b>sig</b>"
    assert (guest.author.member_id, guest.author.name) == (None, "visitor")
    assert guest.posted_at == datetime(2005, 10, 2, 4, 8)
    assert guest.html == "I actually opt for jam"
    assert guest.attachments == [parse.Attached(
        "image", None, url="http://2.forumer.com/uploads/metanet/post-19-1174558319.jpg")]
    assert parse.emoticons(TOPIC_PAGE) == [(":(", "http://2.forumer.com/html/emoticons/sad.gif")]


@pytest.mark.parametrize("value, expected", [
    ("August 26, 2007 08:59 pm", datetime(2007, 8, 26, 20, 59)),
    ("Oct 2 2005, 04:08 AM", datetime(2005, 10, 2, 4, 8)),
    ("Today, 04:08 AM", None),
])
def test_parse_datetime(value, expected):
    assert parse.parse_datetime(value) == expected


@pytest.mark.parametrize("value, expected", [
    ("December 12, 2006", datetime(2006, 12, 12)), ("22-August 05", datetime(2005, 8, 22)),
    ("05-22-2007", datetime(2007, 5, 22)), ("19 March 1990", datetime(1990, 3, 19)),
])
def test_parse_day(value, expected):
    assert parse.parse_day(value) == expected


PROFILE = """<div id="profilename">someone</div>
<a href='http://metanet.2.forumer.com/index.php?act=Search&amp;CODE=getalluser&amp;mid=1836'>Find all posts</a>
<td class="row3" width='30%' valign='top'><b>Total Cumulative Posts</b></td>
<td align='left' width='70%' class='row1'><b>628</b><br />( 0.16% of total forum posts )</td>
<td class="row3" valign='top'><b>Joined</b></td>
<td align='left' class='row1'><b>November 16, 2005</b></td>
<td class="row3" valign='top'><b>AIM Name</b></td>
<td align='left' class='row1'><i>No Information</i></td>
<td class="row3" valign='top'><b>MSN Identity</b></td>
<td align='left' class='row1'>someone@example.com</td>
<td class="row3" valign='top'><b>Birthday</b></td>
<td align='left' class='row1'>19 March 1990</td>
<td class="row3" valign='top'><b>Location</b></td>
<td align='left' class='row1'>Australia
<script type="text/javascript">document.write("<img src='flag.png' />")</script></td>
<td class="row3" valign='top'><b>Avatar</b></td>
<td align='left' class='row1'><img src='http://example.com/a.jpg' border='0' alt='' /></td>
<td class="row3" valign='top'><b>Signature</b></td>
<td align='left' class='row1'><b>hi</b><br>there</td>
"""


def test_profile():
    assert parse.profile(PROFILE) == {
        "member_id": 1836, "name": "someone", "post_count": 628, "joined_at": datetime(2005, 11, 16),
        "msn": "someone@example.com", "birthday": "19 March 1990", "location": "Australia",
        "avatar_url": "http://example.com/a.jpg", "signature": "<b>hi</b><br>there"}


def test_lists():
    members = ("<a href='http://metanet.2.forumer.com/a/user_ozzy_2381.html'>-ozZy</a>"
               "<a href='http://metanet.2.forumer.com/a/user__4703.html'>&#33;&#33;</a>")
    assert parse.archive_members(members) == [(2381, "-ozZy"), (4703, "!!")]
    archive = ("<div class='phead'><b>Olcadan</b>- 05-22-2007</div><div class='postshell'>"
               "<div class='postcolor'> first<br><script>ad</script></div></div><br />"
               "<div class='phead'><b>apg</b>- 05-23-2007</div><div class='postshell'>"
               "<div class='postcolor'> second</div></div>")
    assert parse.archive_posts(archive) == [("Olcadan", datetime(2007, 5, 22), "first"),
                                            ("apg", datetime(2007, 5, 23), "second")]
    forum = ("<td><b>Pinned:  <a href='http://metanet.2.forumer.com/index.php?showtopic=5000' "
             "class='linkthru' title='This topic was started: September 28, 2005 07:49 pm'>The "
             "&quot;Leavers&quot; Thread</a></b>  <span class='small'>(Pages 1)</span>\n"
             "<br /><span class='desc'>Tears, tantrums</span></td>")
    assert parse.forum_topics(forum) == [{"topic_id": 5000, "started_at": datetime(2005, 9, 28, 19, 49),
                                          "title": 'The "Leavers" Thread', "description": "Tears, tantrums",
                                          "pinned": True}]


def test_dump_names():
    query = query_of("index.php_s=0059ee&amp;act=Attach&amp;type=post&amp;id=95402")
    assert query == {"s": "0059ee", "act": "Attach", "type": "post", "id": "95402"}
    assert kind_of(query) == "attach"
    assert kind_of(query_of("index.php_&act=ST&f=14&t=8830")) == "topic"
    assert kind_of(query_of("index.php_s=&showuser=1836")) == "profile"
    assert archive_position("2d-appreciation-thread33_post17443-105.html") == (17443, 105)
    assert archive_position("73_post3207.html") == (3207, 0)
    assert is_error("<title>Board Message</title> forumer")
    assert is_error("Can't connect to local MySQL server through socket")
    assert not is_error(TOPIC_PAGE)


def test_increasing():
    assert increasing([(0, 1), (1, 5), (2, 2), (3, 3), (4, 0), (5, 6)]) == [(0, 1), (2, 2), (3, 3), (5, 6)]


@pytest.fixture
def db():
    with open_database(":memory:") as db:
        db.create_schema()
        db.create_schema(TABLES)
        yield db


def test_link(db):
    db.upsert_many("users", [{"id": 100, "name": "Keron"}, {"id": 101, "name": "101"},
                             {"id": 102, "name": "102"}, {"id": 103, "name": "ska"},
                             {"id": 104, "name": "late"}])
    db.upsert_many("forumer_members", [{"old_id": 1, "name": "Keron"}, {"old_id": 5, "name": "7!"},
                                       {"old_id": 6, "name": "-Gc-"}, {"old_id": 9, "name": "ska"}])
    db.upsert_many("topics", [{"id": 50, "forum_id": 1, "name": "t", "stickied": False, "locked": False}])
    posts = [(10, 103, 1, "2007-08-26 08:59:55"), (11, 100, 2, "2007-08-26 09:08:06"),
             (12, 0, 3, "2007-08-26 09:08:40"), (13, 103, 4, "2007-08-26 10:00:00"),
             (14, 100, 5, "2007-08-26 11:00:00"), (15, 100, 6, "2007-08-26 12:00:00")]
    db.upsert_many("posts", [{"id": i, "topic_id": 50, "user_id": u, "index": n, "timestamp": t,
                              "content": "", "source": "x"} for i, u, n, t in posts])
    db.update_many("posts", [{"id": 13, "source": "a --------------------[url=/attach/ma/post-1-1.zip]"
                                                  "Click here to view the attachment[/url]",
                              "source_fixed": "see [ts:topic=50 old_post=505]here[/ts:topic]"}])
    db.upsert_many("attachments", [{"id": 7, "kind": "upload", "name": "post-1-1.zip", "old_attach_id": None,
                                    "url": "http://2.forumer.com/uploads/metanet/post-1-1.zip"},
                                   {"id": 8, "kind": "attach-id", "name": None, "old_attach_id": 504,
                                    "url": "http://metanet.2.forumer.com/index.php?act=Attach&type=post&id=504"}])
    db.set_state("attachment-missing:7", "not archived")
    old = [(500, 9, "ska", "2007-08-26 08:59:00"), (501, 1, "Keron", "2007-08-26 09:08:00"),
           (502, None, "visitor", "2007-08-26 09:08:00"), (504, 9, "ska", "2007-08-26 10:00:00"),
           (506, 1, "Keron", "2007-08-26 12:00:00")]
    db.upsert_many("forumer_posts", [{"id": i, "topic_id": 50, "member_id": m, "author": a,
                                      "posted_at": t, "html": "h"} for i, m, a, t in old])
    db.upsert_many("forumer_attachments", [{"old_post_id": 504, "ref": "504", "kind": "file",
                                            "name": "level.zip", "data": b"PK\x03\x04",
                                            "source_file": "index.php_act=Attach&id=504"}])
    report = link(db)
    assert db.query("SELECT old_id, user_id, match FROM forumer_members ORDER BY old_id") == [
        (1, 100, "name"), (5, 101, "position"), (6, 102, "position"), (9, 103, "name")]
    assert report["users named by their id, now named"] == 2
    assert db.query("SELECT id, post_id, match FROM forumer_posts ORDER BY id") == [
        (500, 10, "time"), (501, 11, "time+author"), (502, 12, "time+author"), (504, 13, "time"),
        (505, 14, "interpolated"), (506, 15, "time")]
    assert report["guest posts given an author"] == 1
    assert db.query("SELECT attachment_id FROM forumer_attachments") == [(7,)]
    assert db.query("SELECT id, name, size, recovered_from FROM attachments ORDER BY id") == [
        (7, "post-1-1.zip", 4, "forumer-dump:index.php_act=Attach&id=504"),
        (8, "level.zip", 4, "forumer-dump:index.php_act=Attach&id=504")]
    assert db.states("attachment-missing:") == {}
    again = link(db)  # rerunnable: same mappings, files already stored
    assert again["attachment files stored from the dump"] == 0
    assert again - Counter({"attachment files stored from the dump": 2}) == report - Counter(
        {"attachment files stored from the dump": 2})
