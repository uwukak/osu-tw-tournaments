"""HTML → 原始 dict。只做解析，不含任何政策判斷（政策都在 rules.py）。

所有選擇器都對照過 fixtures/ 的真實快照。幾個踩過的坑：

- 列表頁分成 `Pinned Topics` 與 `Topics` 兩個 div.forum-list，
  置頂區是版規帖不是比賽，必須排除。
- 列表頁**沒有發文時間**，只有最後回覆時間。
- 主題頁的作者選擇器 `.forum-post__heading a` 是 0 命中（會靜默回 None），
  要用 `data-post-username` 屬性。
- `.forum-post-content` 在單頁會多出一個（15 vs 14 帖），
  所以取內文一定要 scope 在該帖元素內。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from bs4 import BeautifulSoup, Tag

POSTED_AT = re.compile(r"posted\s+(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2})")

DISCORD_URL = re.compile(r"https?://(?:discord\.gg|discord(?:app)?\.com/invite)/[A-Za-z0-9-]+")
SIGNUP_FORM_URL = re.compile(
    r"https?://(?:docs\.google\.com/forms|forms\.gle|tally\.so|airtable\.com|"
    r"docs\.google\.com/spreadsheets|challonge\.com)[^\s\"'<>]*"
)
STREAM_URL = re.compile(r"https?://(?:www\.)?(?:twitch\.tv|youtube\.com|youtu\.be)/[^\s\"'<>]*")


@dataclass
class TopicRow:
    """論壇列表頁的一列。"""

    topic_id: int
    title: str
    author: Optional[str] = None
    posts: Optional[int] = None
    views: Optional[int] = None
    last_reply_at: Optional[str] = None


@dataclass
class TopicDetail:
    """主題頁的內容。"""

    topic_id: Optional[int] = None
    created_at: Optional[str] = None
    author: Optional[str] = None
    body_text: str = ""
    links: list[str] = field(default_factory=list)

    @property
    def discord(self) -> Optional[str]:
        for url in self.links:
            if DISCORD_URL.match(url):
                return url
        m = DISCORD_URL.search(self.body_text)
        return m.group(0) if m else None

    @property
    def signup_form(self) -> Optional[str]:
        for url in self.links:
            if SIGNUP_FORM_URL.match(url):
                return url
        m = SIGNUP_FORM_URL.search(self.body_text)
        return m.group(0) if m else None

    @property
    def stream(self) -> Optional[str]:
        for url in self.links:
            if STREAM_URL.match(url):
                return url
        return None


def _int_or_none(text: str) -> Optional[int]:
    try:
        return int(re.sub(r"[^\d]", "", text))
    except (TypeError, ValueError):
        return None


def parse_listing(html: str) -> list[TopicRow]:
    """解析 forum 列表頁，只回傳非置頂的主題。"""
    soup = BeautifulSoup(html, "html.parser")
    rows: list[TopicRow] = []

    for section in soup.select("div.forum-list"):
        heading = section.select_one("h2")
        if not heading:
            continue
        if heading.get_text(strip=True) != "Topics":
            continue  # 跳過 Pinned Topics

        for li in section.select("li.forum-topic-entry"):
            tid = li.get("data-topic-id")
            title_el = li.select_one("a.forum-topic-entry__title")
            if not tid or not title_el:
                continue

            if not isinstance(li, Tag):
                continue

            author_el = li.select_one(".forum-topic-entry__col--main a.js-usercard")
            counts = li.select("strong.forum-topic-entry__count")
            time_el = li.select_one("time")

            rows.append(
                TopicRow(
                    topic_id=int(tid),
                    title=title_el.get_text(strip=True),
                    author=author_el.get_text(strip=True) if author_el else None,
                    posts=_int_or_none(counts[0].get_text()) if len(counts) > 0 else None,
                    views=_int_or_none(counts[1].get_text()) if len(counts) > 1 else None,
                    last_reply_at=time_el.get("datetime") if time_el else None,
                )
            )

    return rows


def parse_topic(html: str) -> TopicDetail:
    """解析單一主題頁：發文時間、首帖作者與內文、以及首帖裡的所有連結。"""
    soup = BeautifulSoup(html, "html.parser")
    detail = TopicDetail()

    # 發文時間藏在 .forum-topic-title 的字串裡。
    title_el = soup.select_one(".forum-topic-title")
    if title_el:
        m = POSTED_AT.search(title_el.get_text(" ", strip=True))
        if m:
            detail.created_at = m.group(1)

    # 正規 topic id。
    og = soup.select_one('meta[property="og:url"]')
    if og and og.get("content"):
        m = re.search(r"/topics/(\d+)", og["content"])
        if m:
            detail.topic_id = int(m.group(1))

    # 首帖：用 data-post-position="1"，不要用「第一個 div.js-forum-post」。
    first = None
    for post in soup.select("div.js-forum-post"):
        if post.get("data-post-position") == "1":
            first = post
            break
    if first is None:
        return detail

    detail.author = first.get("data-post-username")

    # 內文要 scope 在這個帖元素內取，否則會抓到別的帖。
    body = first.select_one(".forum-post__body") or first.select_one(".forum-post-content")
    if body is not None:
        detail.body_text = " ".join(body.get_text(" ", strip=True).split())

    # 連結必須從 href 抽 —— get_text() 會把 href 整個丟掉，
    # 上一版就是這樣在 SMST 84 的帖子裡找不到任何 Discord 連結的。
    seen: set[str] = set()
    for a in first.select("a[href]"):
        href = a.get("href")
        if href and href.startswith("http") and href not in seen:
            seen.add(href)
            detail.links.append(href)

    return detail
