"""端到端：手動新增的賽事（data/custom.json）走一遍真正的進入點。

為什麼要另開一個檔案：這條路上有幾段接線**只有在真的跑一次 `scrape.main()` 時才會
執行**，單元測試各蓋一小塊，接線的地方沒人看得到 ——

  * `_write_drafts` 的 baseline 閘門要放行負數 id。手動新增的賽事 id 是 -1、-2…，
    而 baseline 是爬蟲那邊兩百多萬的 id；兩百多萬 > -1，不放行的話每一筆都被
    靜默跳過：看板上有、drafts/ 裡卻永遠生不出草稿。
  * `custom_draft_sha` 要從 old 帶過來再寫回去。沒帶的話 `store.meaningful`
    每回合都判定「有變更」→ 每 30 分鐘一次假提交，永遠不停。
  * 刪掉一筆之後要把它的 key 從 `custom_draft_sha` 修剪掉，但 drafts/ 的檔案要留著
    （使用者的決定：那份草稿可能已經貼出去了）。
  * custom.json 壞掉要讓排程變紅，而且**一個字都不寫**。

全部在暫存目錄裡跑：DATA_PATH／DOCS_DIR／DRAFTS_DIR／CUSTOM_PATH／OVERRIDES_PATH
都換成 tmp 底下的路徑，repo 裡的檔案一個都不碰。

跑法：
    python tests/test_custom_roundtrip.py
    python -m pytest tests/
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from osu_tourney import scrape, store  # noqa: E402

# 先自己裝一個 handler 起來。scrape.main 會呼叫 logging.basicConfig，而它在 root
# 已經有 handler 時是 no-op —— 所以我們設的等級不會被蓋掉，測驗輸出才讀得懂。
logging.basicConfig(level=logging.CRITICAL, stream=sys.stdout)

# 爬蟲那邊的 topic id 是兩百多萬。故意把 baseline 設得比它大，這一輪就只會為手動
# 新增的那筆產生草稿，斷言才看得清楚（不然 50 筆爬蟲賽事會一起摻進來）。
BIG_BASELINE = 99_999_999

CUSTOM = {
    "-1": {
        "topic_id": -1,
        "name": "社群自辦盃",
        "mode": "standard",
        "decision": "include",
        "kind": "player",
        "status": "open",
        # 故意擺在很久以前：手動新增的賽事不受 45 天新鮮度限制，永遠留在看板上。
        "first_seen_utc": "2025-01-01T00:00:00+00:00",
        "url": "https://example.com/signup",
    }
}

PATHS = ("DATA_PATH", "OVERRIDES_PATH", "CUSTOM_PATH", "DOCS_DIR", "DRAFTS_DIR")


@contextlib.contextmanager
def _sandbox():
    """把 scrape 的路徑全部指到一個暫存目錄，跑完無條件還原。"""
    saved = {name: getattr(scrape, name) for name in PATHS}
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        scrape.DATA_PATH = root / "data" / "tournaments.json"
        scrape.OVERRIDES_PATH = root / "data" / "overrides.json"
        scrape.CUSTOM_PATH = root / "data" / "custom.json"
        scrape.DOCS_DIR = root / "docs"
        scrape.DRAFTS_DIR = root / "drafts"
        scrape.DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
        scrape.DATA_PATH.write_text(
            json.dumps(
                {
                    "schema_version": store.SCHEMA_VERSION,
                    "draft_baseline_topic_id": BIG_BASELINE,
                    "custom_draft_sha": {},
                    "tournaments": {},
                }
            ),
            encoding="utf-8",
        )
        try:
            yield root
        finally:
            for name, value in saved.items():
                setattr(scrape, name, value)


def _run() -> int:
    """跑一次真正的進入點 —— 連 main() 的例外處理一起驗。"""
    logging.getLogger().setLevel(logging.CRITICAL)
    return scrape.main(["--offline", "--no-push"])


def _write_custom(root: Path, data) -> None:
    """字串就直接寫進去（給壞 JSON 用），dict 就序列化。"""
    text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    (root / "data" / "custom.json").write_text(text, encoding="utf-8")


def _data(root: Path) -> dict:
    return json.loads((root / "data" / "tournaments.json").read_text(encoding="utf-8"))


def _board(root: Path) -> str:
    return (root / "docs" / "index.html").read_text(encoding="utf-8")


def _sha(text: str) -> str:
    """跟 scrape._sha 同一個算法：草稿內容的 sha256。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _raw(root: Path) -> bytes:
    return (root / "data" / "tournaments.json").read_bytes()


# --------------------------------------------------------------------------


def test_a_hand_added_tournament_gets_a_draft_and_a_row():
    with _sandbox() as root:
        _write_custom(root, CUSTOM)
        assert _run() == 0

        draft = root / "drafts" / "-1.md"
        assert draft.exists(), (
            "手動新增的賽事沒有產生草稿 —— _write_drafts 的 baseline 閘門沒放行負數 id"
        )
        assert "社群自辦盃" in draft.read_text(encoding="utf-8")
        assert "社群自辦盃" in _board(root), "手動新增的賽事沒有出現在看板上"


def test_the_link_is_carried_onto_the_board():
    with _sandbox() as root:
        _write_custom(root, CUSTOM)
        assert _run() == 0
        assert "https://example.com/signup" in _board(root)


def test_the_draft_hash_is_recorded_and_the_next_run_writes_nothing():
    """這條是「不製造假提交」的核心：第二回合必須完全不寫檔。

    沒把 custom_draft_sha 從 old 帶過來的話，old 沒有、new_data 有，
    store.meaningful 每回合都判定有變更 → 每 30 分鐘一次假提交。
    """
    with _sandbox() as root:
        _write_custom(root, CUSTOM)
        assert _run() == 0

        digest = _sha((root / "drafts" / "-1.md").read_text(encoding="utf-8"))
        assert _data(root)["custom_draft_sha"] == {"-1": digest}

        first = _raw(root)
        assert _run() == 0
        assert _raw(root) == first, "第二回合又寫了一次檔 —— 這就是假提交"


def test_removing_it_takes_the_row_off_the_board_but_keeps_the_draft():
    with _sandbox() as root:
        _write_custom(root, CUSTOM)
        assert _run() == 0
        assert "社群自辦盃" in _board(root)

        _write_custom(root, {})
        assert _run() == 0

        assert "社群自辦盃" not in _board(root)
        assert _data(root)["custom_draft_sha"] == {}, "刪掉之後這本帳要修剪乾淨"
        assert (root / "drafts" / "-1.md").exists(), (
            "草稿要留著 —— 使用者的決定是它可能已經貼出去了"
        )


def test_a_hand_edited_draft_is_not_overwritten():
    """手改過的草稿不覆蓋。這條帳（custom_draft_sha）只有在真的跑過一次之後才存在，
    所以要先把 url 改掉逼出一次實質變更，才會走到 _write_drafts。"""
    with _sandbox() as root:
        _write_custom(root, CUSTOM)
        assert _run() == 0

        draft = root / "drafts" / "-1.md"
        draft.write_text("我自己改的內容\n", encoding="utf-8")

        _write_custom(root, {"-1": dict(CUSTOM["-1"], url="https://example.com/changed")})
        assert _run() == 0

        assert draft.read_text(encoding="utf-8") == "我自己改的內容\n", (
            "站長手改過的草稿被覆蓋掉了 —— custom_draft_sha 的 key 對不上"
        )
        # 但看板本身照樣跟著改。
        assert "https://example.com/changed" in _board(root)


def test_a_broken_custom_file_stops_the_run_without_writing():
    with _sandbox() as root:
        _write_custom(root, CUSTOM)
        assert _run() == 0
        before = _raw(root)

        _write_custom(root, "{ 這不是 JSON")
        assert _run() == 1, "壞掉的 custom.json 必須讓排程變紅"
        assert _raw(root) == before, "中止了卻還是動了資料檔"
        assert "社群自辦盃" in _board(root)


def test_a_custom_file_whose_top_level_is_an_array_stops_the_run():
    with _sandbox() as root:
        _write_custom(root, "[1, 2, 3]")
        assert _run() == 1


def test_a_single_malformed_entry_is_skipped_without_taking_the_board_down():
    """單筆形狀不對只濾掉那一筆（跟 override_for 一樣），不該讓整個看板陪葬。"""
    with _sandbox() as root:
        _write_custom(root, dict(CUSTOM, **{"-9": "這整筆寫成字串"}))
        assert _run() == 0
        assert "社群自辦盃" in _board(root)
        assert not (root / "drafts" / "-9.md").exists()


# --------------------------------------------------------------------------

def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in tests:
        try:
            fn()
        except AssertionError as exc:
            failed += 1
            print(f"FAIL  {fn.__name__}\n      {exc}")
        else:
            print(f"ok    {fn.__name__}")

    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
