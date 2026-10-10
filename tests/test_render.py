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

from osu_tourney import render, store  # noqa: E402

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


def test_payload_carries_the_raw_iso_the_countdown_needs():
    """卡片上的「剩餘幾日」是瀏覽器自己算的，靠的就是 deadline_iso。

    這個欄位不見不會報錯，只會安靜地不顯示倒數 —— 典型的靜默失敗，值得釘住。
    """
    row = render.build_payload({"2246109": SMST83}, META, NOW)["tournaments"][0]
    assert row["deadline_iso"] == "2026-09-25T23:59:00+00:00"
    assert row["deadline"], "同時要有人看的台北時間字串"


def test_payload_carries_what_the_detail_panel_shows():
    rec = dict(SMST83, excerpt="歡迎來到 SMST 83。", author="someone")
    row = render.build_payload({"2246109": rec}, META, NOW)["tournaments"][0]
    assert row["title"] == SMST83["title"]
    assert row["excerpt"] == "歡迎來到 SMST 83。"
    assert row["author"] == "someone"
    assert row["deadline_raw"], "面板要顯示主辦自己寫的那句話"


def test_a_record_without_an_excerpt_still_builds():
    """excerpt 是後加的欄位，既有資料沒有它 —— 面板少一段可以，整頁不能壞。"""
    row = render.build_payload({"2246109": SMST83}, META, NOW)["tournaments"][0]
    assert row["excerpt"] == ""
    assert row["title"], "標題本來就每回合更新，不該跟著摘要一起缺"


# --------------------------------------------------------------------------
# 站長說明（data/overrides.json）
#
# 看板上可以直接改說明：頁面拿著站長的 GitHub 權杖，把 data/overrides.json 提交回
# repo，爬蟲下一回合讀它重繪看板。所以這裡釘的兩件事是整條路能不能通：
# 覆寫有沒有進到 payload，以及它有沒有進到 dashboard_sha。
# --------------------------------------------------------------------------


def test_summary_and_excerpt_both_reach_the_page():
    """站長寫的說明與程式抓的節錄要**並存**，不是後者被換掉。

    編輯面板要拿 excerpt 當預設內容，也要用它比對「這次到底改了沒」；
    少了它，第一次打開編輯器會是空白的，而且「跟原節錄一樣就不建立覆寫」
    這條防呆會失效。
    """
    rec = dict(SMST83, excerpt="Welcome to SMST 83. Registrations will end ...")
    ov = {"2246109": {"summary": "SMST 83，1–10K 分區，1v1 單淘汰。", "note": "沒有表單，要在原帖回覆。"}}
    row = render.build_payload({"2246109": rec}, META, NOW, ov)["tournaments"][0]
    assert row["summary"] == "SMST 83，1–10K 分區，1v1 單淘汰。"
    assert row["note"] == "沒有表單，要在原帖回覆。"
    assert row["excerpt"].startswith("Welcome to SMST 83")


def test_records_without_an_override_are_untouched():
    ov = {"9999999": {"summary": "這是別筆的"}}
    row = render.build_payload({"2246109": SMST83}, META, NOW, ov)["tournaments"][0]
    assert row["summary"] == ""
    assert row["note"] == ""


def test_no_overrides_at_all_still_builds():
    """overrides 是後加的參數，只有三個位置參數的舊呼叫端不能因此壞掉。"""
    payload = render.build_payload({"2246109": SMST83}, META, NOW)
    assert payload["tournaments"][0]["summary"] == ""


def test_a_hand_written_entry_of_the_wrong_shape_is_ignored():
    """這個檔案是給人改的，很容易把整筆寫成字串而不是物件。不該讓整頁炸掉。"""
    row = render.build_payload({"2246109": SMST83}, META, NOW, {"2246109": "一段字"})["tournaments"][0]
    assert row["summary"] == ""


def test_an_override_changes_the_dashboard_bytes():
    """這是站長說明能上線的**唯一**路徑，值得釘死。

    存檔改的是 data/overrides.json，不是 data/tournaments.json —— 資料本身一個字都沒動。
    下一回合的爬蟲全靠 dashboard_sha 看出「看板內容不一樣了」才會重繪、提交。
    這個 sha 沒跟著動的話，站長改的字要等到隔天的 heartbeat 才會出現，
    而且中間那 24 小時的 log 全部顯示「資料無變更」。
    """
    before = render.render_dashboard(render.build_payload({"2246109": SMST83}, META, NOW))
    after = render.render_dashboard(
        render.build_payload({"2246109": SMST83}, META, NOW, {"2246109": {"summary": "站長說明"}})
    )
    assert before != after


def test_an_override_does_not_leak_into_the_draft():
    """刻意釘住一條界線：`summary`／`note` 是**看板專用**的註解。

    草稿是要貼到 Facebook 的貼文。那兩欄寫的是「這筆為什麼這樣判」「哪裡要留意」，
    貼出去會很突兀。哪天有人「順手」把它們接進 render_draft，這條會擋下來。
    """
    own = store.override_for({"2246109": {"summary": "只有看板看得到的字", "note": "也只有看板"}}, 2246109)
    draft = render.render_draft(SMST83, NOW, own)
    assert "只有看板看得到的字" not in draft
    assert "也只有看板" not in draft


# --------------------------------------------------------------------------
# 站長手改所有欄位（data/overrides.json 的白名單欄位）
#
# 需求是「看板上可以人工改掉所有資訊」：模式判錯、名次抓錯、Discord 抓錯、
# 「待確認」其實該收錄 —— 都要能當場改掉，而且改完不會被下一回合的抓取蓋回來。
# 這裡釘的是那條路的兩端：疊上去的值有沒有生效，以及沒改的欄位有沒有被動到。
# --------------------------------------------------------------------------


def test_a_hand_edited_name_replaces_the_scraped_one():
    row = render.build_payload(
        {"2246109": SMST83}, META, NOW, {"2246109": {"name": "SMST 83（中文名）"}}
    )["tournaments"][0]
    assert row["name"] == "SMST 83（中文名）"
    assert row["title"] == SMST83["title"], "原帖標題要留著 —— 那是狀態判定的依據"


def test_a_hand_edited_mode_also_relabels_it():
    """改了模式卻沒換標籤，看板上會出現 mode=mania 卻寫著 osu!standard 的卡片。"""
    row = render.build_payload(
        {"2246109": SMST83}, META, NOW, {"2246109": {"mode": "mania", "mania_keys": "7K"}}
    )["tournaments"][0]
    assert row["mode"] == "mania"
    assert row["mode_label"] == "osu!mania"
    assert row["mania_keys"] == "7K"


def test_a_hand_edited_decision_moves_the_record_between_the_two_buckets():
    """「待確認」升成「收錄」是站長最常做的一件事 —— 判定要在過濾前就生效。"""
    review = dict(SMST83, decision="review", reason="unknown-code")
    payload = render.build_payload({"2246109": review}, META, NOW, {"2246109": {"decision": "include"}})
    assert payload["counts"]["include"] == 1
    assert payload["counts"]["review"] == 0, "升成收錄之後就不該再算進待確認"

    # 反過來：排除掉就不該出現在看板上，即使它本來是收錄的。
    payload = render.build_payload({"2246109": SMST83}, META, NOW, {"2246109": {"decision": "exclude"}})
    assert payload["tournaments"] == []


def test_a_hand_edited_status_is_not_corrected_by_the_deadline_again():
    """站長說 open 就是 open —— 否則他的修正下一回合就被同一條規則改回去。

    這正是「報名狀態未標明」的痛點：內文的截止時間早就過去了，程式把它算成
    「表定已截止」，但那場比賽其實延長了報名。站長知道，程式不知道。
    """
    assert render.effective_status(SMST83, NOW) == "expired", "前提：沒手改時是 expired"
    assert render.effective_status(SMST83, NOW, {"status": "open"}) == "open"
    # 打錯字不可以當成定論 —— 退回自動判定比照著錯的值顯示好。
    assert render.effective_status(SMST83, NOW, {"status": "oppen"}) == "expired"


def test_a_hand_edited_status_reaches_the_draft_title():
    """草稿的標題是直接貼出去的，改過的狀態一定要跟著走。"""
    title = render.render_draft(SMST83, NOW, {"status": "open"}).splitlines()[0]
    assert "報名開放中" in title
    assert "表定已截止" not in title


def test_a_hand_edited_field_reaches_the_draft():
    """看板改了、貼出去的卻是舊的，比兩邊都錯更難查 —— 貼出去就收不回來了。"""
    own = {"name": "SMST 83（中文名）", "discord": "https://discord.gg/correct", "rank_compact": "1–10K"}
    draft = render.render_draft(SMST83, NOW, own)
    assert "SMST 83（中文名）" in draft
    assert "https://discord.gg/correct" in draft


def test_a_hand_edited_region_wins_over_the_rule():
    """區域是規則最常判錯的一欄（`MN` 是 Minnesota 還是 Mongolia？）。"""
    own = {"region": "不限區域（站長已確認）"}
    row = render.build_payload({"2246109": SMST83}, META, NOW, {"2246109": own})["tournaments"][0]
    assert row["region"] == "不限區域（站長已確認）"
    assert "不限區域（站長已確認）" in render.render_draft(SMST83, NOW, own)


def test_teams_are_split_back_into_a_list():
    """表單上是一個文字框，但卡片與草稿要的是清單。"""
    row = render.build_payload(
        {"2246109": SMST83}, META, NOW, {"2246109": {"teams": "1v1 / 2v2"}}
    )["tournaments"][0]
    assert row["teams"] == ["1v1", "2v2"]


def test_null_clears_a_field_instead_of_being_ignored():
    """清空一個自動抓到的值，是手改的常見需求（Discord 抓錯、截止時間猜錯）。

    沒有 null 這一種寫法，「清空」只會被當成「沒覆寫」—— 下一回合那個值又自己
    長回來，而且從畫面上完全看不出原因。
    """
    rec = dict(SMST83, discord="https://discord.gg/wrong")
    own = store.override_for({"2246109": {"discord": None, "deadline_iso": None}}, 2246109)
    assert own == {"discord": None, "deadline_iso": None}, "null 要在 override_for 存活下來"
    row = render.build_payload({"2246109": rec}, META, NOW, {"2246109": own})["tournaments"][0]
    assert row["discord"] == ""
    assert row["deadline_iso"] == "", "清掉猜錯的截止時間，卡片才會退回顯示主辦的原句"
    assert row["deadline_raw"], "原句要留著"


def test_null_on_an_enum_field_is_treated_as_no_override():
    """name／mode／status 沒有「空」這個狀態，寫 null 只是噪音，不該生效。"""
    assert store.override_for({"1": {"name": None, "mode": None}}, 1) == {"name": None, "mode": None}
    row = render.build_payload({"2246109": SMST83}, META, NOW, {"2246109": {"name": None}})["tournaments"][0]
    assert row["name"] == "SMST 83", "清掉名字只會退回自動判定，不該變成空白卡片"


def test_a_typo_in_a_hand_edited_value_is_ignored_not_fatal():
    """檔案是手寫的。打錯字只該忽略那一欄，不該讓整頁壞掉或那筆消失。"""
    row = render.build_payload(
        {"2246109": SMST83}, META, NOW, {"2246109": {"mode": "standard", "decision": "inclue"}}
    )["tournaments"][0]
    assert row["mode"] == "std"
    assert row["decision"] == "include"


def test_the_payload_carries_what_the_editor_needs_to_diff():
    """編輯器靠 raw（自動判定的原值）比對「有沒有真的改過」。

    少了它，打開來看一眼、順手按儲存，就會把當下自動判定的結果整批凍結成手改值 ——
    之後規則修好也改不動，而且看不出原因。own 則用來分辨「值一樣」是巧合還是刻意。
    """
    ov = {"2246109": {"name": "手改的名字", "region": None}}
    row = render.build_payload({"2246109": SMST83}, META, NOW, ov)["tournaments"][0]
    assert row["raw"]["name"] == "SMST 83"
    assert row["raw"]["mode"] == "std"
    assert row["raw"]["region"] == "無區域限制（全球開放）", "raw.region 是規則算出來的那個標籤"
    assert row["own"] == {"name": "手改的名字", "region": None}


def test_apply_override_does_not_touch_the_original_record():
    """疊在複本上，原記錄不准動 —— 否則 data/tournaments.json 會被寫進手改值。"""
    before = dict(SMST83)
    render.apply_override(SMST83, {"name": "改過的", "status": "closed"})
    assert SMST83 == before


def test_a_hand_edited_field_changes_the_dashboard_bytes():
    """手改的欄位也要走 dashboard_sha 這條路，否則排程看不到「該重繪了」。"""
    before = render.render_dashboard(render.build_payload({"2246109": SMST83}, META, NOW))
    after = render.render_dashboard(
        render.build_payload({"2246109": SMST83}, META, NOW, {"2246109": {"name": "改過的"}})
    )
    assert before != after


# --------------------------------------------------------------------------
# 分頁：選手報名／工作人員報名
#
# 分頁是**檢視**，不是把資料切成兩半：同時徵選手又徵工作人員的賽事兩頁都要出現，
# 所以 kind 有三個值（player／staff／both），而不是一個布林。
# --------------------------------------------------------------------------


def test_the_payload_carries_the_tab_a_record_belongs_to():
    rec = dict(SMST83, kind="staff")
    row = render.build_payload({"2246109": rec}, META, NOW)["tournaments"][0]
    assert row["kind"] == "staff"


def test_records_from_before_the_tabs_existed_are_players():
    """kind 是後加的欄位，既有的資料檔裡一筆都沒有它。

    少了這個預設值，前端的兩個分頁都不收它們 —— 整批既有賽事會從看板上消失，
    而資料一個字都沒錯。這種「改版把舊資料掃掉」的失敗最難查，值得釘住。
    """
    row = render.build_payload({"2246109": SMST83}, META, NOW)["tournaments"][0]
    assert row["kind"] == "player"


def test_a_hand_edited_kind_moves_a_record_to_the_other_tab():
    """分頁判定是啟發式（`(Staff Wanted)` 這種順便徵人的算選手賽事），
    一定有站長不認同的時候 —— 所以它跟 decision 一樣是手改得動的列舉欄位。"""
    row = render.build_payload(
        {"2246109": SMST83}, META, NOW, {"2246109": {"kind": "staff"}}
    )["tournaments"][0]
    assert row["kind"] == "staff"
    assert row["raw"]["kind"] == "player", "原值要留著，編輯器才比對得出「這次改了沒」"


def test_a_typo_in_a_hand_edited_kind_is_ignored():
    """打錯字只該忽略那一欄（跟 mode／decision 同一條規矩）。

    不濾的話 `stafff` 會進到 payload，前端兩個分頁都不收它 —— 那筆就從看板上
    無聲無息地消失了，而且資料檔裡看起來一切正常。
    """
    row = render.build_payload(
        {"2246109": SMST83}, META, NOW, {"2246109": {"kind": "stafff"}}
    )["tournaments"][0]
    assert row["kind"] == "player"


def test_a_staff_post_is_worded_as_a_recruitment_in_the_draft():
    """徵人帖的草稿寫「報名開放中」，讀者會以為是去報名比賽 —— 那是發錯文。

    草稿是直接貼出去的，措辭錯了比看板上標錯更難補救。
    """
    rec = dict(SMST83, kind="staff", deadline_iso="2026-11-01T23:59:00+00:00")
    draft = render.render_draft(rec, NOW)
    assert "工作人員招募中" in draft
    assert "🙋 招募｜" in draft
    assert "報名開放中" not in draft


def test_a_both_kind_keeps_the_player_wording_and_says_staff_are_wanted_too():
    """兩者都徵的賽事本體是比賽：措辭照選手走，另外加一行說明也在缺人手。

    少了那一行，讀者只知道報名，而工作人員分頁上那張卡片會顯得跟這裡兜不起來。
    """
    rec = dict(SMST83, kind="both", deadline_iso="2026-11-01T23:59:00+00:00")
    draft = render.render_draft(rec, NOW)
    assert "報名開放中" in draft
    assert "同時徵求工作人員" in draft
    assert "工作人員招募中" not in draft, "本體是比賽，標題不該寫成招募帖"


def test_the_staff_tab_is_flagged_in_the_draft_index():
    """純徵人帖在索引（drafts/README.md）上也要一眼看得出來是徵人。

    索引是「有哪些待發草稿」的清單，一整個工作人員分頁的草稿混在裡面卻沒有任何記號，
    就得一份一份點開才知道哪些是徵人帖。
    """
    rec = dict(SMST83, kind="staff", deadline_iso="2026-11-01T23:59:00+00:00")
    payload = render.build_payload({"2246109": rec}, META, NOW)
    index = render.render_index_markdown(payload)
    assert "🙋" in index
    assert "工作人員招募中" in index


# --------------------------------------------------------------------------
# 手動新增的賽事（data/custom.json）
#
# 跟爬蟲抓來的差在兩件事：id 是負數（看板配發的），而且**不受 45 天的新鮮度
# 限制** —— 站長要的是「一直留著，只能手動刪除」，實作點就是下面那條閘門放行
# `custom` 旗標。
# --------------------------------------------------------------------------

HAND_ADDED = {
    "topic_id": -1,
    "name": "社群自辦盃",
    "mode": "std",
    "decision": "include",
    "kind": "player",
    "status": "unknown",
    # 一年前。同樣的時間戳若是爬蟲抓來的，早就掉出看板了（見上面
    # test_long_forgotten_expired_card_drops_off）—— 差別只在 custom 這個旗標。
    "first_seen_utc": "2025-01-01T00:00:00+00:00",
    "last_seen_utc": "2025-01-01T00:00:00+00:00",
    "url": "https://example.com/signup",
}


def test_a_hand_added_tournament_never_ages_off_the_board():
    """站長決定的生命週期：一直留著，只能手動刪除。"""
    crawled = dict(
        SMST83,
        first_seen_utc="2025-01-01T00:00:00+00:00",
        last_seen_utc="2025-01-01T00:00:00+00:00",
    )
    assert render.build_payload({"2246109": crawled}, META, NOW)["tournaments"] == [], "前提"

    rows = render.build_payload({}, META, NOW, None, {"-1": HAND_ADDED})["tournaments"]
    assert [r["name"] for r in rows] == ["社群自辦盃"]


def test_a_hand_added_tournament_is_marked_and_keeps_its_own_link():
    """沒有論壇原帖（id 是負數，連過去只會 404），網址由站長自己填。"""
    row = render.build_payload({}, META, NOW, None, {"-1": HAND_ADDED})["tournaments"][0]
    assert row["custom"] is True
    assert row["url"] == "https://example.com/signup"


def test_a_crawled_tournament_still_links_to_its_forum_topic():
    row = render.build_payload({"2246109": SMST83}, META, NOW)["tournaments"][0]
    assert row["custom"] is False
    assert row["url"] == "https://osu.ppy.sh/community/forums/topics/2246109"


def test_a_hand_added_tournament_without_a_link_gets_none():
    """沒填網址時整條收起來 —— 寧可沒有連結，也不要一個連到 404 的。

    負數的 topic id 拼出來的論壇網址看起來完全正常，只是點進去是 404。
    這種壞連結比沒有連結更糟，所以 `_record_url` 對 custom 一律不自己拼。
    """
    hand = dict(HAND_ADDED, url="")
    row = render.build_payload({}, META, NOW, None, {"-1": hand})["tournaments"][0]
    assert row["url"] == ""

    draft = render.render_draft(store.custom_record("-1", hand), NOW)
    assert "osu.ppy.sh" not in draft
    assert "🔗 資訊" not in draft


def test_a_hand_added_tournament_reaches_the_draft_with_its_link():
    draft = render.render_draft(store.custom_record("-1", HAND_ADDED), NOW)
    assert "🔗 資訊｜https://example.com/signup" in draft


def test_the_editor_shows_the_existing_link_of_a_hand_added_tournament():
    """編輯器的「資訊連結」那格讀的是 raw.url。

    少了它，站長打開表單會看到一格空白，而卡片上明明有一條連結 ——
    按一次儲存就把那條連結抹掉了（比對基準是空的、畫面上也是空的）。
    """
    row = render.build_payload({}, META, NOW, None, {"-1": HAND_ADDED})["tournaments"][0]
    assert row["raw"]["url"] == "https://example.com/signup"


def test_a_crawled_row_has_an_empty_url_in_its_raw_fields():
    """爬蟲那批的網址是從 topic id 算出來的，不存檔。

    前端靠 `f.custom` 把那格藏起來；這裡釘住它至少不會是一個會寫進
    overrides.json 的壞基準值。
    """
    row = render.build_payload({"2246109": SMST83}, META, NOW)["tournaments"][0]
    assert row["raw"]["url"] == ""


def test_hand_added_tournaments_change_the_dashboard_bytes():
    """手動新增也要走 dashboard_sha 這條路，否則排程看不到「該重繪了」。

    少了它，站長新增的比賽只會在他自己那台機器上出現 —— 排程下一回合算出
    一模一樣的 sha，判定「無變更」，整個 repo 都不會被更新。
    """
    before = render.render_dashboard(render.build_payload({}, META, NOW))
    after = render.render_dashboard(
        render.build_payload({}, META, NOW, None, {"-1": HAND_ADDED})
    )
    assert before != after


def test_a_malformed_hand_added_entry_does_not_take_the_board_down():
    """單筆形狀不對只濾掉那一筆，好的一筆照常顯示（跟 override_for 同一條規矩）。"""
    payload = render.build_payload(
        {}, META, NOW, None, {"-1": HAND_ADDED, "-2": "整筆寫成字串"}
    )
    assert [r["name"] for r in payload["tournaments"]] == ["社群自辦盃"]


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
