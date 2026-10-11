"""osu! 台灣可參加賽事彙整。

匯入時就讓輸出容得下非 ASCII，理由見 `_make_console_printable`。
"""
from __future__ import annotations

import sys


def _make_console_printable() -> None:
    """讓這支程式在任何主控台編碼下都不會**死在印字上**。

    這不是為了美觀。跑 `python tests/test_rules.py`（或 `-m osu_tourney.scrape`）時，
    輸出裡有真正的台灣賽事名；Windows 主控台預設是 cp950／cp437，而 `print` 遇到
    編碼不了的字元會拋 `UnicodeEncodeError`。那個例外不是斷言失敗，自寫的測試
    runner 不會把它算成 FAIL —— 它會**直接讓整支程式停在那裡**，後面幾十條測試
    一條都不跑，而唯一的線索是一行 0/38 passed。

    GitHub 的 Ubuntu runner 靠 PEP 538 自動抬成 UTF-8，通常不會踩到；踩到的是在
    自己機器上跑同一個指令的人 —— 而那些人正是最需要看到測試輸出的人。

    `errors="replace"` 只換掉編碼不了的字元（cp950 印得出來的中文照常），所以不是
    「把輸出變英文」，只是不再因為一個 'é' 就整支停擺。

    pytest 底下 sys.stdout 是 capture 物件、沒有 reconfigure，這一步自然跳過 ——
    那邊本來就是 UTF-8（PEP 3120）。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError, OSError):
            # 沒有 reconfigure（pytest 的 capture、被重導向的檔案物件），
            # 或已經關掉的 stream —— 都不是值得讓程式起不來的理由。
            pass


_make_console_printable()
