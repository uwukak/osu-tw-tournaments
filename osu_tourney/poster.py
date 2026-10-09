"""Facebook 發文的接縫 —— 本期刻意不實作。

使用者選擇「先草稿、人工複製貼上」，所以這裡只留介面。
之後要接 Facebook Graph API 時，只需要：

1. 建立 Meta App，取得粉絲專頁的長期存取權杖（需要 pages_manage_posts 權限）
2. 設定環境變數 FB_PAGE_ID 與 FB_PAGE_TOKEN
3. 把 post() 補上對
   https://graph.facebook.com/v21.0/{page_id}/feed
   的 POST（message=草稿內文）

在那之前，dry_run 一律為 True，絕不對外送出任何東西。
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


class Poster:
    def __init__(self, dry_run: bool = True) -> None:
        self.dry_run = dry_run

    def post(self, title: str, body: str) -> None:
        if self.dry_run:
            log.info("（dry run）不對外發文：%s", title)
            return
        raise NotImplementedError(
            "Facebook Graph API 尚未接上。請見 README 的『之後接自動發文』一節。"
        )
