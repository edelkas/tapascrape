"""A logged-in board session: browser profile + exported cookies.

Logging in happens by hand in a Chrome window (`tapascrape login`), so any
login method works (Tapatalk ID, Google, legacy forum account) and the tool
never handles passwords. The Chrome profile keeps the login for later browser
use; `session.json` carries its cookies and User-Agent to curl_cffi and the
API. phpBB ties a session to the User-Agent, so both must be sent together.

The session directory holds live credentials: keep it private.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_ROOT = Path.home() / ".tapascrape"


def session_dir(board: str, override: str | None = None) -> Path:
    return Path(override) if override else DEFAULT_ROOT / board


@dataclass
class Session:
    user_agent: str
    cookies: dict[str, str] = field(default_factory=dict)
    user_id: int | None = None
    username: str | None = None

    @staticmethod
    def path(directory: Path) -> Path:
        return directory / "session.json"

    @classmethod
    def load(cls, directory: Path) -> "Session":
        path = cls.path(directory)
        if not path.exists():
            raise FileNotFoundError(
                f"no saved session in {directory}; run `tapascrape login <board>` first")
        return cls(**json.loads(path.read_text(encoding="utf-8")))

    def save(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        self.path(directory).write_text(json.dumps(self.__dict__, indent=2), encoding="utf-8")

    def cookie_header(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self.cookies.items())


def board_user_id(board: str, cookies: dict[str, str]) -> int | None:
    """Logged-in phpBB user id from the board's cookies; None for guests (id 1)."""
    value = cookies.get(f"phpbb_{board}_u", "")
    return int(value) if value.isdigit() and int(value) > 1 else None
