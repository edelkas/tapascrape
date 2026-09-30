"""User profile page (memberlist.php?mode=viewprofile)."""

from dataclasses import dataclass, field

from bs4 import BeautifulSoup

from tapascrape.models import Group


@dataclass
class Profile:
    rank: str | None
    signature: str | None  # raw HTML
    groups: list[Group] = field(default_factory=list)


def parse_profile(html: str) -> Profile | None:
    """Rank, signature and groups; None if the page isn't a profile (e.g. deleted user)."""
    soup = BeautifulSoup(html, "lxml")
    card = soup.select_one(".member-profile-card")
    if card is None:
        return None

    rank = None
    if (name := card.select_one(".profile-rank-name")) and name.get_text(strip=True):
        rank = name.get_text(strip=True)
    elif img := card.select_one(".rank img, .profile-rank img"):
        rank = img.get("title") or img.get("alt") or None

    signature = None
    if sig := soup.select_one("#viewprofile .signature"):
        signature = sig.decode_contents().strip() or None

    groups = [Group(int(o["value"]), o.get_text(strip=True))
              for o in soup.select("select[name=g] option") if o.get("value", "").isdigit()]
    return Profile(rank, signature, groups)
