import email.message

from tapascrape.net.api import _Transport
from tapascrape.net.session import Session, board_user_id


def test_session_roundtrip(tmp_path):
    session = Session("Mozilla/5.0 Chrome/154", {"phpbb_metanetfr_u": "9374721", "a": "b"},
                      user_id=9374721, username="Someone")
    session.save(tmp_path)
    assert Session.load(tmp_path) == session
    assert session.cookie_header() == "phpbb_metanetfr_u=9374721; a=b"


def test_board_user_id():
    assert board_user_id("metanetfr", {"phpbb_metanetfr_u": "9374721"}) == 9374721
    assert board_user_id("metanetfr", {"phpbb_metanetfr_u": "1"}) is None  # guest
    assert board_user_id("metanetfr", {"phpbb_other_u": "9374721"}) is None


class FakeConnection:
    def __init__(self):
        self.headers = []

    def putheader(self, name, value):
        self.headers.append((name, value))

    def endheaders(self, *args):
        pass


class FakeResponse:
    def __init__(self, cookies):
        self.msg = email.message.Message()
        for cookie in cookies:
            self.msg["Set-Cookie"] = cookie


def test_transport_sends_session_cookies_and_keeps_new_ones(monkeypatch):
    transport = _Transport(10, Session("Mozilla/5.0 Chrome/154", {"sid": "abc"}))
    assert transport.user_agent == "Mozilla/5.0 Chrome/154"

    monkeypatch.setattr("xmlrpc.client.SafeTransport.parse_response", lambda self, r: ("ok",))
    transport.parse_response(FakeResponse(["sid=def; path=/; HttpOnly", "k=1; path=/"]))
    assert transport.cookies == {"sid": "def", "k": "1"}

    connection = FakeConnection()
    transport.send_headers(connection, [("Content-Type", "text/xml")])
    assert ("Cookie", "sid=def; k=1") in connection.headers


def test_guest_transport_uses_tapatalk_agent():
    transport = _Transport(10)
    assert transport.user_agent == "Tapatalk" and transport.cookies == {}
