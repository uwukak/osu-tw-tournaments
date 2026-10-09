"""rules.py 的黃金測試。

對 fixtures/ 裡的兩個真實 osu! 快照做斷言。這些快照是 2026-10-09 抓的，
osu! 之後改 HTML 結構時測試會失敗 —— 那正是它的用途。

跑法：
    python tests/test_rules.py        # 不須 pytest
    python -m pytest tests/           # 有 pytest 也可以
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bs4 import BeautifulSoup  # noqa: E402

from osu_tourney.rules import (  # noqa: E402
    EXCLUDE,
    INCLUDE,
    REVIEW,
    analyze,
    clean_name,
    detect_mode,
    extract_deadline,
    extract_rank_range,
    extract_status,
    extract_team_formats,
    is_staff_topic,
)

FIXTURES = ROOT / "fixtures"


def forum_titles() -> list[str]:
    """取出 forum 55 列表頁裡「Topics」區塊的所有標題（排除 Pinned Topics）。"""
    soup = BeautifulSoup(
        (FIXTURES / "forum55_created.html").read_text(encoding="utf-8"), "html.parser"
    )
    sections = [
        d
        for d in soup.select("div.forum-list")
        if d.select_one("h2") and d.select_one("h2").get_text(strip=True) == "Topics"
    ]
    assert sections, "找不到 Topics 區塊 —— osu! 的 HTML 結構可能改了"
    return [
        li.select_one("a.forum-topic-entry__title").get_text(strip=True)
        for li in sections[0].select("li.forum-topic-entry")
    ]


# --------------------------------------------------------------------------
# 回歸測試：這兩個是真實抓到的 bug，不許再壞掉
# --------------------------------------------------------------------------


def test_bug_capitalised_region_code_is_excluded():
    """Bug 1：正則帶 (?i) 抓到 'BR'，查表沒 .lower() 就查不到而誤入待確認。"""
    for title in [
        "[STD][BR Only] CB6.2 - Coliseu Brasileiro de 6 Dígitos - Segunda Edição",
        "[STD] [BR ONLY] [9k-120.000] TB5-Torneio Brasileiro de 5 Dígitos 2026",
        "[STD] IUOC - Season 1 : Mambo [ID Only] | [1v1] #100k - #inf+",
        "[STD] [1v1/2v2, team size 2] CHaos sYMphony Tournament [#1 - #50k] [VN Only]",
    ]:
        v = analyze(title)
        assert v.decision == EXCLUDE, f"{title!r} 應排除，實際 {v.decision}/{v.reason}"


def test_bug_ru_speaking_language_gate_is_excluded():
    """Bug 2：`[Ru-speaking only]` 抓不到；`SPEAK RU ONLY` 靠代碼規則撿到。"""
    for title in [
        "[STD] Raise It! 2 (Auction) [Open rank] | 3v3 TS6 [Ru-speaking only] | [REGS CLOSED]",
        "[STD, SPEAK RU ONLY] #75 week Shawonovo Pool cup (weekly)",
        "[STD] [lazer] DUALITY trial [日本語, 2v2]",
    ]:
        v = analyze(title)
        assert v.decision == EXCLUDE, f"{title!r} 應排除，實際 {v.decision}/{v.reason}"


# --------------------------------------------------------------------------
# 模式
# --------------------------------------------------------------------------


def test_mode_detection():
    cases = {
        "(Taiko, 1v1, Open Rank) The Taiko Poop Cup": "taiko",
        "[osu!taiko][1v1] Beginner's Taiko Tournament 10": "taiko",
        "[osu!catch] UK Catch Tournament 2026 (Staff Wanted)": "catch",
        "[o!m 4K] The Aphelion 2026 #5,000-40,000": "mania",
        "[PLAYER + STAFF REG OPEN][o!m 4k][3v3 TS6] Newcomers Mania World Cup 2026": "mania",
        "[STD]SMST 84 50K-100K [Open]": "std",
        "[std/stable] [lan] purl's interstellar tales": "std",
    }
    for title, want in cases.items():
        got, _ = detect_mode(title)
        assert got == want, f"{title!r} 模式應為 {want}，實際 {got}"


def test_rank_range_is_not_mistaken_for_mania_keys():
    """[9k-120.000] 是名次，不是 9K 鍵數 —— 所以鍵數只在判定 mania 後才抽。"""
    v = analyze("[STD] [BR ONLY] [9k-120.000] TB5-Torneio Brasileiro 2026")
    assert v.mode == "std"
    assert v.mania_keys is None


def test_mania_keys_extracted():
    v = analyze("[o!m 4K] AusNZ!4K Mania Tournament 2026")
    assert v.mode == "mania"
    assert v.mode_label == "osu!mania 4K"


# --------------------------------------------------------------------------
# 名次區間
# --------------------------------------------------------------------------


def test_rank_range_formats():
    cases = {
        "[STD]SMST 84 50K-100K [Open]": (50000, 100000),
        "[osu!std] Balkan Chibi Cup 2026 [2v2 TS3][#10,000-#99,999]": (10000, 99999),
        "[o!m 4K] Bai Yu Cup 2026[#2500-infinity]": (2500, None),
        "(20.000 - 99.999 rank)": (20000, 99999),
        "[3v3 TS6 Tiered Suiji, 3,333-25,000 BWS]": (3333, 25000),
        "[#1~200k]": (1, 200000),
        # 「1-10K」是 1 ~ 10,000：只有上界帶 K，下界就是 1。不要讀成 1K~10K。
        "[STD]SMST 83 1-10K [Open]": (1, 10000),
    }
    for title, want in cases.items():
        got = extract_rank_range(title)
        assert got == want, f"{title!r} 名次應為 {want}，實際 {got}"


def test_year_range_is_not_a_rank_range():
    """'2026-2027 season' 不是名次區間。"""
    assert extract_rank_range("osu! Cup 2026-2027 season") is None


# --------------------------------------------------------------------------
# 隊伍形式
# --------------------------------------------------------------------------


def test_team_formats_stay_a_list():
    teams, _ = extract_team_formats("[STD] [1v1/2v2, team size 2] CHaos sYMphony Tournament")
    assert teams == ["1v1", "2v2"], teams
    teams, sizes = extract_team_formats("[STD] Hidden Cup | 3v3 Ts5 | #100 - #99.999")
    assert teams == ["3v3"] and sizes == ["5"]


# --------------------------------------------------------------------------
# 開放狀態：Open Rank 不是報名開放
# --------------------------------------------------------------------------


def test_open_rank_is_not_open_registration():
    assert extract_status("(Taiko, 1v1, Open Rank) The Taiko Poop Cup") == "unknown"
    assert extract_status("[STD]SMST 84 50K-100K [Open]") == "open"
    assert extract_status("[STD] Raise It! 2 | [REGS CLOSED]") == "closed"
    assert extract_status("[STD] American Cup [Regs Open!]") == "open"


# --------------------------------------------------------------------------
# 工作人員帖：只排除真正的工作人員帖
# --------------------------------------------------------------------------


def test_staff_topics_excluded_but_player_tournaments_kept():
    assert is_staff_topic("[STAFF REGS] Looking-Glass Mirror: bloom | [SEA, STD]")
    assert is_staff_topic("[o!m 4K][STAFF RECRUITMENT][2v2]Bai Yu Cup 2026")

    # 這些是「選手」賽事，只是順便徵工作人員，不可以被排除。
    for title in [
        "[osu!catch] UK Catch Tournament 2026 (Staff Wanted)",
        "[STD, SEA only] osu!MayniLAN 2026 | Player and Staff Registrations Open",
        "[o!m 4K] The Aphelion 2026 (4K rank) [Staff + Player Regs OPEN]",
        "[STD] Hidden Cup | 3v3 Ts5 | Team, FA & Staff Regs Open!",
    ]:
        assert not is_staff_topic(title), f"{title!r} 不該被當成工作人員帖"


# --------------------------------------------------------------------------
# 名稱清洗
# --------------------------------------------------------------------------


def test_clean_name():
    assert clean_name("[STD]SMST 84 50K-100K [Open]") == "SMST 84"
    assert clean_name("[osu!std] Balkan Chibi Cup 2026 [2v2 TS3][#10,000-#99,999]") == "Balkan Chibi Cup 2026"
    assert clean_name("(Taiko, 1v1, Open Rank) The Taiko Poop Cup") == "The Taiko Poop Cup"


def test_two_brackets_of_same_tournament_collide_on_name_but_not_identity():
    """SMST 84 有兩個主題，清完同名 —— 所以身分必須用 topic_id。"""
    a = analyze("[STD]SMST 84 50K-100K [Open]")
    b = analyze("[STD]SMST 84 100K-Infinity [Open]")
    assert a.name == b.name == "SMST 84"
    assert a.rank_compact == "50K–100K"
    assert b.rank_compact == "100K–∞"


# --------------------------------------------------------------------------
# 截止時間：時:分 不可以被讀成「日」
# --------------------------------------------------------------------------


def test_deadline_time_before_month_is_parsed():
    """bug 回歸：真實句子（SMST 84）。23:59 的 59 被當成「日」→ datetime 爆掉 → None。

    這是實際從主題頁抓到的原文，不是造出來的例子。
    """
    raw, iso = extract_deadline(
        "Registrations will end for both brackets at 23:59 October 16th(Friday UTC+0)",
        "2026-10-08T02:43:20+00:00",
    )
    assert raw == "Registrations will end for both brackets at 23:59 October 16th(Friday UTC+0)"
    assert iso == "2026-10-16T23:59:00+00:00"


def test_deadline_minutes_are_not_read_as_a_day():
    """12:30 October 16th → 日是 16。若讀成 30，會安靜地給出錯誤日期（不是 None）。"""
    _, iso = extract_deadline(
        "Signups close at 12:30 October 16th", "2026-10-01T00:00:00+00:00"
    )
    assert iso == "2026-10-16T12:30:00+00:00"


def test_deadline_month_first_still_works():
    _, iso = extract_deadline(
        "Registrations close on October 20th", "2026-10-01T00:00:00+00:00"
    )
    assert iso == "2026-10-20T23:59:00+00:00"  # 沒寫時間 → 當日結束


def test_deadline_day_first_still_works():
    _, iso = extract_deadline(
        "Registrations end 5 November at 18:00", "2026-10-01T00:00:00+00:00"
    )
    assert iso == "2026-11-05T18:00:00+00:00"


def test_deadline_crossing_new_year_rolls_forward():
    """12 月發的帖寫「1 月 5 日」→ 是隔年。"""
    _, iso = extract_deadline(
        "Registrations end January 5th", "2026-12-20T00:00:00+00:00"
    )
    assert iso == "2027-01-05T23:59:00+00:00"


def test_deadline_without_a_parseable_date_keeps_the_raw_sentence():
    """猜不出來時 ISO 為 None —— 草稿會顯示原句，不捏造時間。"""
    raw, iso = extract_deadline(
        "Signups will close soon, see the Discord for details", "2026-10-01T00:00:00+00:00"
    )
    assert raw == "Signups will close soon, see the Discord for details"
    assert iso is None


def test_no_deadline_sentence_at_all():
    assert extract_deadline("Welcome to the tournament!", None) == (None, None)


# --------------------------------------------------------------------------
# 整體分布（若 osu! 語料沒變，這些數字必須吻合）
# --------------------------------------------------------------------------

EXPECTED_DECISIONS = {INCLUDE: 16, EXCLUDE: 28, REVIEW: 6}


def check_distribution(verbose: bool = True) -> Counter:
    titles = forum_titles()
    verdicts = [analyze(t) for t in titles]

    by_reason = Counter((v.decision, v.reason) for v in verdicts)
    by_decision = Counter(v.decision for v in verdicts)

    if verbose:
        for v in verdicts:
            print(f"  {v.decision:8} {v.reason:15} {v.mode_label:18} {v.status:8} {v.rank_compact:12} | {v.name[:44]}")
        print(f"\n  DECISION: {dict(by_decision)}")
        for k in sorted(by_reason):
            print(f"    {k}: {by_reason[k]}")

    return by_decision


def test_distribution():
    counts = check_distribution(verbose=False)
    assert dict(counts) == EXPECTED_DECISIONS, (
        f"分類分布改變了：{dict(counts)} != {EXPECTED_DECISIONS}\n"
        "若是刻意調整規則，請更新 EXPECTED_DECISIONS；否則表示有回歸。"
    )


def test_fixture_has_50_topics():
    assert len(forum_titles()) == 50


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

    print("\n--- 分類分布 ---")
    check_distribution()

    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
