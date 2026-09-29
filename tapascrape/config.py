"""Board-level configuration."""

from dataclasses import dataclass

TAPATALK_ROOT = "https://www.tapatalk.com/groups"


@dataclass(frozen=True)
class BoardConfig:
    board: str
    rate: float = 1.0  # max requests per second, shared by every client
    timeout: float = 30.0
    max_retries: int = 5

    @property
    def base_url(self) -> str:
        return f"{TAPATALK_ROOT}/{self.board}/"

    @property
    def api_url(self) -> str:
        return f"{self.base_url}mobiquo/mobiquo.php"

    def profile_url(self, user_id: int) -> str:
        return f"{self.base_url}memberlist.php?mode=viewprofile&u={user_id}"

    def forum_url(self, forum_id: int) -> str:
        return f"{self.base_url}viewforum.php?f={forum_id}"

    def topic_url(self, topic_id: int) -> str:
        return f"{self.base_url}viewtopic.php?t={topic_id}"
