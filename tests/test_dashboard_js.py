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


def test_the_payload_the_editor_reads_is_serialisable():
    """編輯器要的 raw／own 必須真的進得了 JSON，而且值都是字串（或 null）。

    少了 raw，編輯器就沒有比對基準；少了 own，就分不清「值一樣」是巧合還是刻意。
    這兩個都是後加的欄位，很容易在某次改動裡被漏掉。
    """
    html = dashboard_html()
    blob = re.search(r'<script type="application/json" id="data">(.*?)</script>', html, re.S)
    assert blob, "找不到內嵌的資料 JSON"
    data = json.loads(blob.group(1).replace("<\\/", "</"))
    row = data["tournaments"][0]
    assert row["raw"]["name"] == "SMST 84"
    assert row["own"]["name"] == "手改的名字"
    assert row["own"]["discord"] is None, "清空要用 null 表達，編輯器才分得出「沒改」與「要空的」"
    for key, value in row["raw"].items():
        assert isinstance(value, str), f"raw[{key!r}] 不是字串，跟 <input> 的值比不了"


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
