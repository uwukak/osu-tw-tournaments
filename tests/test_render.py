"""render.py 的黃金測試：報名狀態校正、看板資料、草稿標題。

這裡釘住的是一個真實案例，不是造出來的：SMST 83（topic 2246109）主辦忘了把標題的
`[Open]` 改掉，帖子內文卻明寫 9/25 截止 —— 看板照標題顯示「報名開放中」，
實際上報名已經關了兩週。

跑法：
    python tests/test_render.py        # 不須 pytest
    python -m pytest tests/            # 有 pytest 也可以
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from osu_tourney import render  # noqa: E402

# 真實資料，取自 data/tournaments.json 的 2246109（欄位只留測試用得到的）。
SMST83 = {
    "topic_id": 2246109,
    "name": "SMST 83",
    "mode": "std",
    "mode_label": "osu!standard",
    "rank_compact": "1–10K",
    "teams": [],
    "status": "open",  # ← 標題寫的
    "decision": "include",
    "reason": "no-region",
    "title": "[STD]SMST 83 1-10K [Open]",
    "deadline_iso": "2026-09-25T23:59:00+00:00",
    "deadline_raw": "Registrations will end for both brackets at 23:59 September 25th(Friday UTC+0)",
    "first_seen_utc": "2026-10-09T15:39:43+00:00",
    "last_seen_utc": "2026-10-09T16:08:16+00:00",
}

NOW = "2026-10-10T08:00:00+00:00"

META = {"last_run_utc": NOW, "last_run_taipei": "2026-10-10 16:00"}


# --------------------------------------------------------------------------
# 狀態校正
# --------------------------------------------------------------------------


def test_title_open_but_deadline_passed_is_expired():
    """真實案例：標題沒改，內文 9/25 就截止了。"""
    assert render.effective_status(SMST83, NOW) == "expired"


def test_the_same_record_before_its_deadline_stays_open():
    """同一筆資料搬到截止前 —— 校正不能反過來誤殺還能報名的比賽。"""
    assert render.effective_status(SMST83, "2026-09-20T00:00:00+00:00") == "open"


def test_a_future_deadline_is_not_touched():
    """SMST 84 的另一個 bracket（50K-100K）10/16 才截止，不該被動到。"""
    rec = dict(SMST83, topic_id=2252361, deadline_iso="2026-10-16T23:59:00+00:00")
    assert render.effective_status(rec, NOW) == "open"


def test_title_declared_closed_wins_over_the_deadline():
    """標題自己說截止了，就信標題 —— 不必等內文日期。"""
    rec = dict(SMST83, status="closed", deadline_iso="2099-01-01T00:00:00+00:00")
    assert render.effective_status(rec, NOW) == "closed"


def test_unknown_status_with_past_deadline_is_expired():
    rec = dict(SMST83, status="unknown")
    assert render.effective_status(rec, NOW) == "expired"


def test_missing_deadline_is_never_expired():
    rec = {k: v for k, v in SMST83.items() if k != "deadline_iso"}
    assert render.effective_status(rec, NOW) == "open"


def test_unparseable_deadline_is_never_expired():
    """猜不出來的時間不當證據 —— 不確定就往「還沒截止」倒。"""
    rec = dict(SMST83, deadline_iso="soon-ish")
    assert render.effective_status(rec, NOW) == "open"


def test_no_now_means_no_correction():
    """沒傳 now_iso 就退回標題狀態，不猜。"""
    assert render.effective_status(SMST83, None) == "open"


# --------------------------------------------------------------------------
# 看板資料
# --------------------------------------------------------------------------


def test_expired_is_not_counted_as_open():
    payload = render.build_payload({"2246109": SMST83}, META, NOW)
    assert [r["status"] for r in payload["tournaments"]] == ["expired"]
    assert payload["counts"]["open"] == 0, "已過截止的不該算進『報名中』"
    assert payload["counts"]["include"] == 1


def test_expired_card_stays_on_the_board():
    """校正不等於隱藏 —— 卡片要留著（變灰），人才找得到、點得進去。"""
    payload = render.build_payload({"2246109": SMST83}, META, NOW)
    assert len(payload["tournaments"]) == 1


def test_long_forgotten_expired_card_drops_off():
    """已經掉出論壇列表、又荒廢很久的，就讓它自然消失，不要淤在看板上。"""
    stale = dict(
        SMST83,
        first_seen_utc="2026-01-01T00:00:00+00:00",
        last_seen_utc="2026-01-01T00:00:00+00:00",
    )
    payload = render.build_payload({"2246109": stale}, META, NOW)
    assert payload["tournaments"] == []


def test_dashboard_is_byte_identical_regardless_of_dict_order():
    """`dashboard_sha` 能成立的前提：相同資料 → 位元組相同的 HTML。

    同一回合發現的賽事 first_seen_utc 完全相同，排序一定平手。若平手時的順序取決於
    dict 的迭代順序，「這回合剛寫入的 dict」與「從 sort_keys 過的 JSON 載回來的 dict」
    就會排出不同結果 → dashboard_sha 每回合都變 → 每 30 分鐘一次假提交。
    """
    other = dict(SMST83, topic_id=2246000, name="另一場賽事")
    forward = render.render_dashboard(
        render.build_payload({"2246000": other, "2246109": SMST83}, META, NOW)
    )
    backward = render.render_dashboard(
        render.build_payload({"2246109": SMST83, "2246000": other}, META, NOW)
    )
    assert forward == backward


def test_dashboard_html_changes_when_a_deadline_passes():
    """dashboard_sha 必須真的跟著狀態改變 —— 否則它就只是個常數，擋不住任何東西。"""
    before = render.render_dashboard(
        render.build_payload({"2246109": SMST83}, META, "2026-09-20T00:00:00+00:00")
    )
    after = render.render_dashboard(render.build_payload({"2246109": SMST83}, META, NOW))
    assert before != after


# --------------------------------------------------------------------------
# 草稿標題
# --------------------------------------------------------------------------


def test_draft_title_says_what_the_body_says():
    """草稿是要貼出去的 —— 標題說「報名開放中」而實際截止，比看板標錯更嚴重。"""
    title = render.render_draft(SMST83, NOW).splitlines()[0]
    assert "表定已截止" in title
    assert "報名開放中" not in title


def test_draft_title_without_now_keeps_the_title_status():
    title = render.render_draft(SMST83).splitlines()[0]
    assert "報名開放中" in title


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
