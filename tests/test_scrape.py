"""明細什麼時候該重抓 —— 也就是看板上的資料能有多新。

為什麼需要這條：明細（內文節錄、截止時間、Discord、報名表單）只在下載主題頁
那一刻算得出來，所以 `_needs_details` 是整個專案裡「資料會不會過期」的單一決策點。

它原本的條件寫死 `status == open` —— 標題沒標報名狀態（`unknown`）的賽事
**永遠不會再抓第二次**。主辦把報名延長、換掉報名表單、補上 Discord，看板全部
停在第一次抓到的那個版本，而且畫面上看起來完全正常。

這條特別值得釘住，因為它壞掉時：測試全綠、提交照跑、Pages 照部署，只有
「照著看板去報名、結果發現表單早就換了」的人會踩到。

跑法：
    python tests/test_scrape.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# tests/ 自己也要在路徑上：`python tests/x.py` 跑的時候，
# sys.path[0] 是 tests/（直譯器放的），但 pytest 底下不是。
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _runner  # noqa: E402

from osu_tourney import scrape  # noqa: E402

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)


def record(status: str, days_ago: int = 0) -> dict:
    """資料檔裡已經有的一筆賽事，它的明細是 `days_ago` 天前抓的。"""
    return {
        "status": status,
        "details_fetched_at": (NOW - timedelta(days=days_ago)).isoformat(),
    }


def needs(existing, refresh_days: int = 3, refetch_all: bool = False,
          listing_status: str | None = None) -> bool:
    """照 `run()` 的呼叫方式問一次 —— 包括那個由 existing 推出來的 missing_details
    與當回合從標題算出來的 `listing_status`。"""
    return scrape._needs_details(
        existing,
        missing_details=not (existing or {}).get("details_fetched_at"),
        now=NOW,
        refresh_days=refresh_days,
        refetch_all=refetch_all,
        listing_status=listing_status,
    )


def test_a_brand_new_tournament_always_fetches_details():
    assert needs(None) is True


def test_a_hidden_entry_is_never_touched():
    """`existing["status"]` 是列舉外的值（舊資料、或人直接在 GitHub 上改壞）→ 不重抓。

    這裡刻意不猜「大概是 open 吧」：猜錯就是把下載額度花在一筆排除掉的帖子上。
    """
    assert needs(record("nonsense", 999), listing_status="unknown") is False


def test_a_record_that_never_had_details_fetched_is_retried():
    """上次抓主題頁失敗（或離線跑時沒有那份快照）→ 這回合一定要再試一次。

    少了這一條，那一筆的 discord 與截止時間就永久缺漏，而且因為它的狀態可能
    不是 open，連帶也不會落到 stale 規則上。
    """
    assert needs({"status": "open"}) is True
    assert needs({"status": "closed"}) is True


def test_refetch_all_overrides_everything():
    """`--refetch-details` 是改過解析器之後的一次性重跑，什麼都要抓。"""
    assert needs(record("closed", 999), refetch_all=True) is True
    assert needs(record("open", 0), refetch_all=True) is True


def test_an_open_tournament_is_refreshed_on_the_normal_schedule():
    assert needs(record("open", 2)) is False, "3 天內還在保鮮期，不該重抓"
    assert needs(record("open", 5)) is True, "超過 3 天就該重抓"


def test_an_untagged_tournament_is_no_longer_ignored_forever():
    """← 這個檔案存在的理由。

    舊條件寫死 `status == open`，所以 `unknown` 的賽事不管放多久都不會再抓一次。
    一筆放到 30 天前的紀錄，在舊寫法下是 False —— 主辦早就改過內文了，
    看板卻還是第一天的版本。
    """
    assert needs(record("unknown", 30)) is True, (
        "標題沒標報名狀態的賽事放了 30 天還是不重抓 —— "
        "主辦延期、換報名表單、補 Discord，看板會永遠停在第一次抓到的版本"
    )


def test_untagged_tournaments_still_wait_longer_than_open_ones():
    """放寬不是無上限：`unknown` 的間隔是 open 的 3 倍（預設 3 天 → 9 天）。

    不拉長的話，一堆待確認與已排除的帖子會跟「報名中」搶下載額度 ——
    而它們真正會變動的機率低得多。這裡把兩邊的邊界都釘住。
    """
    assert needs(record("unknown", 8)) is False, "9 天內不該重抓"
    assert needs(record("unknown", 10)) is True, "超過 9 天就該重抓"
    # 間隔是跟著 --refresh-days 縮放的，不是寫死的 9 天。
    assert needs(record("unknown", 4), refresh_days=1) is True


def test_a_reopened_tournament_refetches_its_details():
    """← 這個檔案存在的第二個理由。主辦把標題改回 [Open]，明細就該跟著重抓。

    舊寫法只看 `existing["status"]`（**上一次**寫進資料檔的值），所以
    `[REGS CLOSED]` → `[REGS OPEN]` 這條路上：看板立刻顯示「報名開放中」
    （`effective_status` 讀的是標題），明細卻永遠不會重抓 —— 舊值還是 closed，
    在這裡就 return False 了。重新開放報名的比賽，卡片上掛著舊的截止時間、
    舊的報名表單、舊的 Discord，而畫面上完全看不出來。

    這一條只有把當回合從標題算出來的新鮮狀態傳進來才過得了 ——
    也就是說它守著的是 `run()` 的接線，不只是這個純函式。
    """
    assert needs(record("closed", 999), listing_status="open") is True, (
        "標題改回 [Open] 了，明細卻還是不重抓 —— 看板會一直掛著舊的截止時間與表單"
    )
    # 反向刻意不做：open → closed 的內文早就定稿了，硬抓只是多花一次下載額度。
    assert needs(record("open", 1), listing_status="closed") is False
    # 標題本來就說 open 的，照原本的節奏走，不要每次都抓。
    assert needs(record("open", 1), listing_status="open") is False
    # 已經是新鮮狀態的 unknown 也一樣。
    assert needs(record("unknown", 1), listing_status="unknown") is False


def test_a_closed_tournament_is_never_refreshed_automatically():
    """已截止的重開機會太低，不值得每回合重抓；真要重算就下 --refetch-details。"""
    assert needs(record("closed", 999)) is False
    assert needs(record("closed", 999), refetch_all=True) is True


def test_an_unparseable_timestamp_counts_as_stale():
    """`details_fetched_at` 壞掉時要重抓，不是當成「剛抓過」而永遠跳過。"""
    assert needs({"status": "open", "details_fetched_at": "not-a-date"}) is True


# --------------------------------------------------------------------------


def main() -> int:
    return _runner.run(globals())


if __name__ == "__main__":
    raise SystemExit(main())
