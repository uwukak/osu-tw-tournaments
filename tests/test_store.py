"""store.py 的變更偵測測試。

重點在 detach：`upsert` 與 `mark_off_listing` 都是**就地修改**。若直接拿
`data["tournaments"]` 去改，`has_meaningful_change` 就會拿修改後的狀態跟自己比，
永遠回報「無變更」—— 這是真的發生過的 bug，下場是 changed 恆為 False，
只剩換日的 heartbeat 會寫檔，當天發現的新賽事全部被吞到隔天才出現。

後半段是 data/overrides.json（看板上的站長說明）。那份檔案只有人會寫，
所以重點全在「讀到壞掉的内容時怎麼辦」。

跑法：
    python tests/test_store.py
    python -m pytest tests/
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from osu_tourney import store  # noqa: E402

NOW = "2026-10-09T12:00:00+00:00"
LATER = "2026-10-09T12:30:00+00:00"
TODAY = "2026-10-09"


def _data() -> dict:
    return {
        "schema_version": store.SCHEMA_VERSION,
        "last_run_utc": None,
        "heartbeat_date": TODAY,
        "tournaments": {},
    }


def _fields(topic_id: int, title: str = "[STD] Cup 1-10K [Open]") -> dict:
    return {"topic_id": topic_id, "title": title, "name": "Cup", "status": "open"}


def _wrap(tournaments: dict, last_run: str = NOW) -> dict:
    return {
        "schema_version": store.SCHEMA_VERSION,
        "last_run_utc": last_run,
        "heartbeat_date": TODAY,
        "tournaments": tournaments,
    }


# --------------------------------------------------------------------------
# 別名：這整個檔案存在的理由
# --------------------------------------------------------------------------


def test_detach_does_not_alias_the_source():
    """detach 出來的複本被改，原資料不可以跟著動。"""
    data = _data()
    tournaments = store.detach(data)
    store.upsert(tournaments, _fields(1), NOW)
    assert data["tournaments"] == {}, "detach 後改動竟然回寫到原資料 —— 別名沒有切斷"


def test_new_tournament_is_detected_as_a_change():
    data = _data()
    tournaments = store.detach(data)
    store.upsert(tournaments, _fields(1), NOW)
    assert store.has_meaningful_change(data, _wrap(tournaments, LATER))


def test_aliasing_would_hide_the_change():
    """把這個坑釘住：不 detach（別名）就會永遠回報無變更。

    這**不是**期望的行為，而是記錄 detach 為什麼不能拿掉。
    哪天有人把 detach「簡化」掉，test_new_tournament_is_detected_as_a_change 會失敗，
    這個測試則說明原因。
    """
    data = _data()
    tournaments = data["tournaments"]  # ← 別名，錯誤示範
    store.upsert(tournaments, _fields(1), NOW)
    assert not store.has_meaningful_change(data, _wrap(tournaments, LATER)), (
        "別名竟然偵測得到變更？那 detach 的存在意義要重新檢視。"
    )


def test_going_off_listing_is_detected_as_a_change():
    """掉出列表是實質變更（off_listing_since 會寫進資料），不可被吃掉。"""
    data = _data()
    tournaments = store.detach(data)
    store.upsert(tournaments, _fields(1), NOW)
    before = _wrap(tournaments)

    detached = store.detach(before)
    store.mark_off_listing(detached, seen_ids=set(), now_iso=LATER)
    assert store.has_meaningful_change(before, _wrap(detached, LATER))


# --------------------------------------------------------------------------
# 什麼算變更、什麼不算
# --------------------------------------------------------------------------


def test_title_change_is_detected():
    """osu! 標題是可變的（[Open] → [REGS CLOSED]），必須算變更。"""
    data = _data()
    t1 = store.detach(data)
    store.upsert(t1, _fields(1, "[STD] Cup 1-10K [Open]"), NOW)
    before = _wrap(t1)

    t2 = store.detach(before)
    store.upsert(t2, _fields(1, "[STD] Cup 1-10K [REGS CLOSED]"), LATER)
    assert store.has_meaningful_change(before, _wrap(t2, LATER))


def test_only_volatile_timestamps_changing_is_not_a_change():
    """last_seen_utc 每回合都變，但不算變更 —— 否則每 30 分鐘假提交一次。"""
    data = _data()
    t1 = store.detach(data)
    store.upsert(t1, _fields(1), NOW)
    before = _wrap(t1)

    t2 = store.detach(before)
    store.upsert(t2, _fields(1), LATER)  # 內容完全相同，只有 last_seen_utc 換了
    assert not store.has_meaningful_change(before, _wrap(t2, LATER))


def test_last_run_utc_alone_is_not_a_change():
    """last_run_utc 是每次執行都會變的時鐘，本身不構成提交理由。"""
    data = _data()
    tournaments = store.detach(data)
    store.upsert(tournaments, _fields(1), NOW)
    assert not store.has_meaningful_change(_wrap(tournaments, NOW), _wrap(tournaments, LATER))


def test_upsert_reports_whether_it_changed_anything():
    tournaments: dict = {}
    assert store.upsert(tournaments, _fields(1), NOW) is True
    assert store.upsert(tournaments, _fields(1), LATER) is False
    assert store.upsert(tournaments, _fields(1, "[STD] Cup 1-10K [Closed]"), LATER) is True


def test_title_history_keeps_every_distinct_title():
    tournaments: dict = {}
    store.upsert(tournaments, _fields(1, "[Open]"), NOW)
    store.upsert(tournaments, _fields(1, "[Closed]"), NOW)
    store.upsert(tournaments, _fields(1, "[Open]"), NOW)  # 改回來不重複記
    assert tournaments["1"]["title_history"] == ["[Open]", "[Closed]"]


# --------------------------------------------------------------------------
# data/overrides.json
#
# 這個檔案跟 tournaments.json 的關係是單向的：只有人會寫它（看板上的編輯介面，
# 或直接在 GitHub 上改），爬蟲只讀。所以「讀壞掉時怎麼辦」是這裡唯一的重點。
# --------------------------------------------------------------------------


def _overrides(text: str):
    """把內容寫進暫存檔，回傳路徑。"""
    d = tempfile.TemporaryDirectory()
    path = Path(d.name) / "overrides.json"
    path.write_text(text, encoding="utf-8")
    return d, path


def test_a_missing_overrides_file_is_simply_empty():
    """還沒有人編輯過時，這個檔案根本不存在 —— 那是正常狀態，不是錯誤。"""
    with tempfile.TemporaryDirectory() as d:
        assert store.load_overrides(Path(d) / "overrides.json") == {}


def test_load_overrides_reads_what_is_there():
    d, path = _overrides('{"2252361": {"summary": "站長寫的說明"}}')
    try:
        assert store.load_overrides(path) == {"2252361": {"summary": "站長寫的說明"}}
    finally:
        d.cleanup()


def test_a_broken_overrides_file_raises_instead_of_reading_as_empty():
    """壞掉時必須拋錯，不能默默當成空檔。

    當成空檔的下場很惡毒：看板上的站長說明整批消失，而且因為 dashboard_sha
    跟著變了，這個「消失」還會被當成一次正常的更新提交出去 ——
    從 log 到看板都看不出異狀，使用者只會發現自己寫的字不見了。
    """
    d, path = _overrides('{"2252361": {"summary": "少了收尾的括號"')
    try:
        try:
            store.load_overrides(path)
        except store.OverridesError as exc:
            assert "overrides.json" in str(exc)
        else:
            raise AssertionError("壞掉的 JSON 竟然沒有拋錯")
    finally:
        d.cleanup()


def test_a_json_array_is_rejected():
    """最外層一定要是「topic id → 覆寫內容」的物件。"""
    d, path = _overrides('["2252361"]')
    try:
        try:
            store.load_overrides(path)
        except store.OverridesError:
            pass
        else:
            raise AssertionError("最外層是陣列竟然被接受了")
    finally:
        d.cleanup()


def test_override_for_drops_anything_that_is_not_a_string():
    """檔案是給人改的，形狀可能不對 —— 濾掉就好，不必讓整個看板陪葬。"""
    ov = {"1": {"summary": "好的", "note": 42, "extra": "不認識的欄位"}}
    assert store.override_for(ov, 1) == {"summary": "好的"}
    assert store.override_for(ov, 99) == {}
    assert store.override_for({"2": "整筆寫成字串"}, 2) == {}


def test_override_for_only_keeps_whitelisted_fields():
    """白名單，不是「除了這幾個之外都行」。

    overrides.json 是手寫的檔案，打錯字（`regionn`）要能被濾掉，而不是讓一個
    不存在的欄位靜靜流進看板。`topic_id` 與 `title` 更是刻意不在名單裡 ——
    前者是這筆賽事的身分，後者是「原帖怎麼寫」的證據。
    """
    ov = {"1": {"decision": "include", "region": "亞洲", "regionn": "打錯字", "topic_id": "999", "title": "改標題"}}
    assert store.override_for(ov, 1) == {"decision": "include", "region": "亞洲"}


def test_blank_overrides_count_as_no_override():
    """把欄位清空＝退回自動判定，所以空白等於沒有覆寫。"""
    assert store.override_for({"1": {"summary": "   ", "note": "\n", "discord": ""}}, 1) == {}


def test_null_means_clear_this_field():
    """清空一個自動抓到的值是手改的常見需求（解析錯的 Discord、猜錯的截止時間）。

    沒有 null 這一種寫法，「清空」只會被當成「沒覆寫」—— 下一回合那個值又自己長回來，
    而且從畫面上完全看不出原因。所以空白與 null 的語意必須分開。
    """
    ov = {"1": {"discord": None, "signup_form": ""}}
    # 空字串＝沒覆寫（key 被丟掉），null＝明確清空（key 留著、值是 None）。
    assert store.override_for(ov, 1) == {"discord": None}


# --------------------------------------------------------------------------
# data/custom.json
#
# 站長在看板上手動新增的賽事。跟 overrides.json 同一種關係（只有人會寫），
# 但多兩件事要釘：id 一定是負整數，而且 merge 進去看板時爬蟲記錄必須是
# **同一個物件**。
# --------------------------------------------------------------------------


def _custom(text: str):
    d = tempfile.TemporaryDirectory()
    path = Path(d.name) / "custom.json"
    path.write_text(text, encoding="utf-8")
    return d, path


def test_a_missing_custom_file_is_simply_empty():
    """還沒有人手動新增過時，這個檔案不存在 —— 正常狀態，不是錯誤。"""
    with tempfile.TemporaryDirectory() as d:
        assert store.load_custom(Path(d) / "custom.json") == {}


def test_load_custom_reads_what_is_there():
    d, path = _custom('{"-1": {"topic_id": -1, "name": "社群自辦盃"}}')
    try:
        assert store.load_custom(path) == {"-1": {"topic_id": -1, "name": "社群自辦盃"}}
    finally:
        d.cleanup()


def test_a_broken_custom_file_raises_instead_of_reading_as_empty():
    """壞掉必須拋錯，理由跟 overrides.json 一字不差：

    當成空檔的話，看板上每一筆手動新增的賽事會整批消失，而且因為 dashboard_sha
    跟著變了，這個「消失」還會被當成一次正常更新提交出去。
    """
    d, path = _custom('{"-1": {"name": "少了收尾的括號"')
    try:
        try:
            store.load_custom(path)
        except store.CustomError as exc:
            assert "custom.json" in str(exc)
        else:
            raise AssertionError("壞掉的 JSON 竟然沒有拋錯")
    finally:
        d.cleanup()


def test_a_custom_json_array_is_rejected():
    d, path = _custom('["-1"]')
    try:
        try:
            store.load_custom(path)
        except store.CustomError:
            pass
        else:
            raise AssertionError("最外層是陣列竟然被接受了")
    finally:
        d.cleanup()


def test_custom_record_normalises_one_entry():
    """單筆整理成跟爬蟲記錄同構的 dict。

    topic_id 一定要是**整數**：看板的排序是 `int(r["topic_id"])`，身分比對也是，
    而 custom.json 是手寫得出來的檔案，有人把 id 寫成字串是完全可能的。
    """
    rec = store.custom_record("-1", {"topic_id": "-1", "name": "  社群自辦盃  "})
    assert rec["topic_id"] == -1 and isinstance(rec["topic_id"], int)
    assert rec["name"] == "社群自辦盃", "兩邊的空白要清掉，不然卡片標題會歪掉"
    assert rec["custom"] is True
    # 這三個是看板與草稿都讀的欄位，沒有就補預設值。
    assert rec["decision"] == "include"
    assert rec["kind"] == "player"
    assert rec["status"] == "unknown"
    assert rec["first_seen_utc"] == ""


def test_custom_record_key_is_the_fallback_id():
    """記錄本身沒有 topic_id 時，用外層的 key 當 id。"""
    assert store.custom_record("-7", {"name": "只有名字"})["topic_id"] == -7


def test_custom_record_drops_entries_that_cannot_be_a_card():
    """形狀不對的單筆只濾掉那一筆，不讓整個看板陪葬（跟 override_for 一樣）。"""
    assert store.custom_record("-1", "整筆寫成字串") is None
    assert store.custom_record("-1", {"topic_id": "不是數字", "name": "X"}) is None
    # 沒有名字 = 一張沒有標題的卡片。那是壞掉的一筆，不是「還沒填完」——
    # 看板上根本存不了半成品（儲存前就擋掉了）。
    assert store.custom_record("-1", {"topic_id": -1}) is None
    assert store.custom_record("-1", {"topic_id": -1, "name": "   "}) is None


def test_merge_custom_keeps_crawled_records_by_identity():
    """**這一條是整個設計的關鍵。**

    `_write_drafts` 會把 `record["draft_sha"]` 寫回記錄，而那個記錄正是稍後要存進
    tournaments.json 的那一個物件。merge_custom 若做 deep copy，雜湊會寫進一個
    用完就丟的複本 —— tournaments.json 永遠記不住自己寫過哪份草稿，於是每次跑都
    認為草稿被人手改過，或反過來把手改的草稿蓋掉。
    """
    crawled = {"2252361": {"topic_id": 2252361, "name": "SMST 84"}}
    merged = store.merge_custom(crawled, {"-1": {"topic_id": -1, "name": "社群自辦盃"}})
    assert merged["2252361"] is crawled["2252361"], "爬蟲記錄被複製了 —— 雜湊會寫不回資料檔"
    assert merged["-1"]["custom"] is True
    assert set(merged) == {"2252361", "-1"}


def test_merge_custom_skips_an_id_that_collides_with_a_crawled_one():
    """手動新增的 id 是負數，爬蟲的是正數，正常不會撞到。

    真撞到了（有人把爬蟲的 id 抄進來）也不該讓那一筆悄悄蓋掉爬蟲的資料 ——
    要改爬蟲那筆請用 overrides.json。
    """
    crawled = {"2252361": {"topic_id": 2252361, "name": "爬蟲的"}}
    merged = store.merge_custom(crawled, {"2252361": {"topic_id": 2252361, "name": "手動的"}})
    assert merged["2252361"]["name"] == "爬蟲的"


def test_merge_custom_without_a_file_is_just_the_crawled_records():
    crawled = {"2252361": {"topic_id": 2252361}}
    assert store.merge_custom(crawled, None) == crawled
    assert store.merge_custom(crawled, {}) == crawled


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
