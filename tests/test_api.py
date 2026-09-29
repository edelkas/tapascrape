import xmlrpc.client
from datetime import datetime

from tapascrape.config import BoardConfig
from tapascrape.net.api import PAGE_SIZE, TapatalkApi, decode, to_datetime


class FakeApi(TapatalkApi):
    """TapatalkApi with canned responses instead of network calls."""

    def __init__(self, responses):
        super().__init__(BoardConfig("test", rate=0))
        self.responses = responses
        self.calls = []

    def call(self, method, *params):
        self.calls.append((method, params))
        return self.responses(method, *params)


def raw_topic(tid, forum="54", sticky=False, posts=1, views=10, **extra):
    return {"topic_id": str(tid), "forum_id": forum, "topic_title": f"T{tid}",
            "topic_author_id": "9", "is_sticky": sticky, "is_closed": False, "total_post_num": posts,
            "view_number": views, **extra}


def test_decode_binary_recursively():
    raw = {"a": xmlrpc.client.Binary("Café".encode()), "b": [xmlrpc.client.Binary(b"x"), 1]}
    assert decode(raw) == {"a": "Café", "b": ["x", 1]}


def test_to_datetime_is_naive_utc():
    assert to_datetime("1080945342") == datetime(2004, 4, 2, 22, 35, 42)
    assert to_datetime("") is None


def test_iter_topics_merges_modes_and_pages():
    normal = [raw_topic(i) for i in range(1, 61)]  # two pages

    def responses(method, fid, start, end, mode=""):
        if mode == "TOP":
            return {"total_topic_num": 1, "topics": [raw_topic(100, sticky=True, posts=418)]}
        if mode == "ANN":
            # A global announcement living in another forum is skipped.
            return {"total_topic_num": 1, "topics": [raw_topic(200, forum="7")]}
        return {"total_topic_num": len(normal), "topics": normal[start:end + 1]}

    api = FakeApi(responses)
    topics = list(api.iter_topics(54))
    assert [t.id for t in topics] == [100] + list(range(1, 61))
    assert topics[0].stickied and topics[0].post_count == 418
    assert not topics[1].stickied
    normal_calls = [c for c in api.calls if len(c[1]) == 3]
    assert [c[1][1] for c in normal_calls] == [0, PAGE_SIZE]


def test_iter_topics_skips_moved_stubs():
    def responses(method, fid, start, end, mode=""):
        if mode:
            return {"total_topic_num": 0, "topics": []}
        return {"total_topic_num": 2, "topics": [raw_topic(1), raw_topic(2, is_moved=True)]}

    assert [t.id for t in FakeApi(responses).iter_topics(54)] == [1]


def test_iter_posts_uses_position():
    def responses(method, tid, start, end, html):
        posts = [{"post_id": str(1000 + i), "post_author_id": "9", "position": i + 1,
                  "timestamp": "1231906327", "post_content": f"<b>{i}</b>"}
                 for i in range(start, min(end + 1, 55))]
        return {"total_post_num": 55, "posts": posts}

    posts = list(FakeApi(responses).iter_posts(6364))
    assert len(posts) == 55
    assert posts[0].index == 1 and posts[-1].index == 55
    assert posts[0].timestamp == datetime(2009, 1, 14, 4, 12, 7)


def test_get_user():
    def responses(method, name, uid):
        return {"user_id": "9370387", "username": "Keron Cyst", "usergroup_id": ["5", "4", "2"],
                "post_count": 7652, "timestamp_reg": "1080945342", "timestamp": "1574790215",
                "icon_url": "https://groups.tapatalk-cdn.com/avatar/19289/9370387_1574790139.jpg"}

    user = FakeApi(responses).get_user(9370387)
    assert user.name == "Keron Cyst" and user.group_ids == [5, 4, 2]
    assert user.joined_at == datetime(2004, 4, 2, 22, 35, 42)
    assert user.last_active_at == datetime(2019, 11, 26, 17, 43, 35)
