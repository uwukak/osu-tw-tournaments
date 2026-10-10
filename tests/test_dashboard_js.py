"""看板 HTML 裡的 inline script 到底能不能被 JS 引擎解析。

為什麼需要這條：docs/index.html 的整支前端是 render.py 裡一個 Python 原始字串
（`_TEMPLATE`）。那裡打錯一個括號或引號，Python 完全不會有意見 —— 測試全綠、
提交照跑、Pages 也照樣部署，壞掉的只有使用者打開的那一頁（而且是一片空白，
連錯誤訊息都只在他的 console 裡）。這正是「測試都過、上線卻壞掉」的典型。

所以：把產出的 HTML 裡那段 script 抽出來，交給 node 做語法檢查。沒有 node 就跳過
（那是本機環境問題，不該讓 CI 變紅 —— CI runner 一定有 node）。

跑法：
    python tests/test_dashboard_js.py
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from osu_tourney import render  # noqa: E402

META = {"last_run_utc": "2026-10-10T08:00:00+00:00", "last_run_taipei": "2026-10-10 16:00"}

# 一筆「什麼欄位都有」的賽事，加上一筆手改過全部的 —— 這樣才測到編輯器那條路
# （raw／own／null 都要出現在 payload 裡，JS 才讀得到）。
RECORD = {
    "topic_id": 2252361,
    "name": "SMST 84",
    "mode": "mania",
    "mode_label": "osu!mania",
    "mania_keys": "7K",
    "rank_compact": "50K–100K",
    "rank_full": "50,000 – 100,000",
    "teams": ["1v1", "2v2"],
    "status": "open",
    "decision": "review",
    "reason": "unknown-code",
    # 工作人員分頁的措辭（招募 vs 報名）走的是另一條路，所以這裡刻意用 staff，
    # 讓那一條也在看板的資料裡出現一次。
    "kind": "staff",
    "title": "[7K]SMST 84 50K-100K [Open]",
    "author": "someone",
    "excerpt": "Welcome to SMST 84.",
    "deadline_iso": "2026-10-16T23:59:00+00:00",
    "deadline_raw": "Registrations will end at 23:59 October 16th",
    "discord": "https://discord.gg/example",
    "signup_form": "https://forms.gle/example",
    "stream": "https://twitch.tv/example",
    "first_seen_utc": "2026-10-09T15:39:43+00:00",
    "last_seen_utc": "2026-10-09T16:08:16+00:00",
}

OVERRIDES = {
    "2252361": {
        "name": "手改的名字",
        "region": "不限區域",
        "decision": "include",
        "teams": "1v1 / 2v2",
        "discord": None,
        "summary": "站長寫的說明",
        "note": "站長補充",
    }
}


def dashboard_html() -> str:
    payload = render.build_payload({"2252361": RECORD}, META, "2026-10-10T08:00:00+00:00", OVERRIDES)
    return render.render_dashboard(payload)


def inline_script(html: str) -> str:
    """抽出最後一段 <script>（沒有 type 的那個）—— 就是整支前端。"""
    blocks = re.findall(r"<script>(.*?)</script>", html, re.S)
    assert blocks, "看板 HTML 裡找不到 inline script"
    return blocks[-1]


def embedded_data(html: str) -> dict:
    """抽出看板內嵌的那份 JSON —— 前端讀的就是這一份。"""
    blob = re.search(r'<script type="application/json" id="data">(.*?)</script>', html, re.S)
    assert blob, "找不到內嵌的資料 JSON"
    return json.loads(blob.group(1).replace("<\\/", "</"))


def test_the_payload_the_editor_reads_is_serialisable():
    """編輯器要的 raw／own 必須真的進得了 JSON，而且值都是字串（或 null）。

    少了 raw，編輯器就沒有比對基準；少了 own，就分不清「值一樣」是巧合還是刻意。
    這兩個都是後加的欄位，很容易在某次改動裡被漏掉。
    """
    row = embedded_data(dashboard_html())["tournaments"][0]
    assert row["raw"]["name"] == "SMST 84"
    assert row["own"]["name"] == "手改的名字"
    assert row["own"]["discord"] is None, "清空要用 null 表達，編輯器才分得出「沒改」與「要空的」"
    for key, value in row["raw"].items():
        assert isinstance(value, str), f"raw[{key!r}] 不是字串，跟 <input> 的值比不了"


def test_a_hand_added_tournament_reaches_the_editor_with_its_link():
    """手動新增的賽事要帶著 custom 與 url 進到看板。

    `url` 同時出現在兩個地方，缺一不可：row 上的那份是卡片與詳細面板的連結，
    `raw.url` 是編輯器那格的比對基準。少了後者，站長一打開表單就看到空白格，
    存一次就把自己填的連結抹掉了。
    """
    hand = {
        "-1": {
            "topic_id": -1,
            "name": "社群自辦盃",
            "mode": "std",
            "decision": "include",
            "kind": "player",
            "status": "unknown",
            "first_seen_utc": "2025-01-01T00:00:00+00:00",
            "last_seen_utc": "2025-01-01T00:00:00+00:00",
            "url": "https://example.com/signup",
        }
    }
    payload = render.build_payload({}, META, "2026-10-10T08:00:00+00:00", None, hand)
    row = embedded_data(render.render_dashboard(payload))["tournaments"][0]

    assert row["custom"] is True
    assert row["url"] == "https://example.com/signup"
    assert row["raw"]["url"] == "https://example.com/signup"
    for key, value in row["raw"].items():
        assert isinstance(value, str), f"raw[{key!r}] 不是字串，跟 <input> 的值比不了"


def test_the_api_calls_opt_out_of_the_browser_cache():
    """每一支 GitHub API 呼叫都要明確關掉瀏覽器快取。

    GitHub 的 API 回應帶 `Cache-Control: private, max-age=60`，而**讀檔**打的是
    `GET /contents/<檔>?ref=main`、**寫檔**打的是 `PUT /contents/<檔>`（沒有 ?ref）。
    網址不一樣，所以寫回不會讓那份讀取的快取失效。

    少了 `no-store`：存一次（sha S1 → S2），60 秒內再存第二次時讀到的還是快取的 S1，
    帶著 S1 去 PUT 被 GitHub 用 409 拒絕，重試一次讀到同一份快取、還是 409 ——
    畫面於是說「有人同時改了這個檔案，請再按一次儲存」，但根本沒有人在改。

    這條特別值得釘住，因為它壞掉時**測試全綠、提交照跑、Pages 照部署**，
    只有實際動手連續編輯的人會看到，而且訊息把人指向完全錯誤的方向。
    """
    script = inline_script(dashboard_html())
    assert "no-store" in script, (
        "api() 沒有關掉瀏覽器快取 —— 連續存檔會被自己剛剛存下的快取用 409 擋下來，"
        "而畫面會說是「有人同時改了這個檔案」"
    )


def test_the_inline_script_parses():
    """整支前端交給 node 做語法檢查。"""
    node = shutil.which("node")
    if not node:
        print("      （沒有 node，跳過語法檢查）")
        return
    script = inline_script(dashboard_html())
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "board.js"
        path.write_text(script, encoding="utf-8")
        done = subprocess.run(
            (node, "--check", str(path)), capture_output=True, text=True, encoding="utf-8", errors="replace"
        )
    assert done.returncode == 0, f"看板的 inline script 有語法錯誤：\n{done.stderr}"


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
