"""唯一碰網路的地方。

設計原則：**失敗就大聲拋錯，絕不回傳空結果。**
呼叫端收到例外時必須中止且不寫檔 —— 用空資料覆蓋 tournaments.json 會毀掉累積的歷史。
"""
from __future__ import annotations

import logging
import time
from typing import Optional

import requests

log = logging.getLogger(__name__)

LISTING_URL = "https://osu.ppy.sh/community/forums/55?sort=created&page={page}"
TOPIC_URL = "https://osu.ppy.sh/community/forums/topics/{topic_id}?start=0"

# 誠實的識別 UA。osu! 在 Cloudflare 後面，禿的 requests UA 是經典的 403 來源。
USER_AGENT = (
    "osu-tw-tournaments/1.0 (+https://github.com/; tournament aggregator for the "
    "Taiwanese osu! community; contact via GitHub issues)"
)

RETRY_STATUS = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 4
MIN_INTERVAL = 1.0  # 秒；併發上限 1，逐次間隔


class FetchError(RuntimeError):
    """抓取失敗。呼叫端應該中止整個回合，不要寫入任何檔案。"""


class Fetcher:
    def __init__(self, min_interval: float = MIN_INTERVAL, timeout: int = 30) -> None:
        self.min_interval = min_interval
        self.timeout = timeout
        self._last_request = 0.0
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.9,zh-TW;q=0.8",
            }
        )

    # ------------------------------------------------------------------

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last_request = time.monotonic()

    def get(self, url: str) -> str:
        last_error: Optional[str] = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            self._throttle()
            try:
                resp = self.session.get(url, timeout=self.timeout)
            except requests.RequestException as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                log.warning("請求失敗（第 %d 次）：%s — %s", attempt, url, last_error)
                time.sleep(2 ** attempt)
                continue

            if resp.status_code == 200:
                html = resp.text
                if _looks_like_challenge(html):
                    raise FetchError(
                        f"osu! 回傳了 Cloudflare 挑戰頁而非內容：{url}\n"
                        "（GitHub Actions 的機房 IP 較容易被擋；請改用本機排程）"
                    )
                return html

            last_error = f"HTTP {resp.status_code}"
            if resp.status_code in RETRY_STATUS:
                wait = _retry_after(resp) or 2 ** attempt
                log.warning("HTTP %s（第 %d 次），等 %.0f 秒後重試：%s",
                            resp.status_code, attempt, wait, url)
                time.sleep(wait)
                continue

            # 403/404 等不重試。
            break

        raise FetchError(f"抓取失敗：{url} — {last_error}")

    # ------------------------------------------------------------------

    def listing(self, page: int = 1) -> str:
        """論壇列表頁。`sort=created` 必須明寫，否則拿到的是最後回覆排序。"""
        return self.get(LISTING_URL.format(page=page))

    def topic(self, topic_id: int) -> str:
        """主題頁。`start=0` 明確釘住第一頁，避免首帖不在預設頁上。"""
        return self.get(TOPIC_URL.format(topic_id=topic_id))


def _retry_after(resp: requests.Response) -> Optional[float]:
    raw = resp.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return min(float(raw), 60.0)
    except ValueError:
        return None


def _looks_like_challenge(html: str) -> bool:
    """辨識 Cloudflare 的阻擋頁 —— 它會用 HTTP 200 回傳。"""
    head = html[:4000].lower()
    return (
        "<title>just a moment" in head
        or "cf-browser-verification" in head
        or "enable javascript and cookies to continue" in head
    )
