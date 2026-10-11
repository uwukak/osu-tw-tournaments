"""測試的共用零件：自寫的 runner。

**為什麼不直接用 pytest。** 這個專案跑測試的方式是 `python tests/test_rules.py`，
不需要任何第三方套件 —— 那是有意的，見 README「本機使用」。但那個自寫的 runner
有個真的會咬人的缺點：

    except AssertionError 只接得住斷言失敗。

`test_` 函式裡任何**其他**例外（複製貼上打錯的 `TypeError`、開檔失敗的
`FileNotFoundError`、`pytest.raises` 用的例外）都不會被接住 —— 它會一路往上冒、
**當場結束整支程式**，後面幾十條測試一條都不跑。唯一的線索是 `0/38 passed`：
看起來像「全部失敗」，實際上是「只跑到一半，而且失敗的那條連名字都沒印出來」。

所以這裡接 `Exception`（刻意不含 `KeyboardInterrupt`／`SystemExit` —— Ctrl-C 與
`sys.exit()` 本來就該往上冒）。每一條測試不管怎麼壞都會被列出來，其餘照跑。

`python -m pytest tests/` 也可以用，不需要這個檔案幫任何忙：pytest 自己收集
`test_` 函式，而每個測試檔的模組層級常數（例如 `test_render.py` 的 `SMST83`）
在 pytest 底下照樣解析得到 —— 那些名字就在該模組的命名空間裡。想用 pytest 是為了
`--last-failed`、`-k`、以及完整的回溯；兩個 runner 跑的永遠是同一批測試。
"""
from __future__ import annotations


def run(module_globals: dict) -> int:
    """跑這個模組裡所有 `test_` 函式。回傳 0／1，直接當程式結束碼用。

    收集方式跟 pytest 一致：`test_` 開頭、可呼叫、且**定義在這個模組裡**。
    最後那項（比對 `__module__`）是必要的 —— 少了它，測試檔之間互相 import
    進來的 `test_` 函式會被跑第二次，而那些通常需要不同的前置條件。
    """
    tests = [
        v
        for k, v in sorted(module_globals.items())
        if k.startswith("test_")
        and callable(v)
        and getattr(v, "__module__", None) == module_globals.get("__name__")
    ]
    failed = 0
    for fn in tests:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 —— 就是要接住全部，理由見檔頭
            failed += 1
            print(f"FAIL  {fn.__name__}\n      {type(exc).__name__}: {exc}")
        else:
            print(f"ok    {fn.__name__}")

    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0
