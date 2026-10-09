"""parse.py 的黃金測試：首帖摘要（看板「簡要說明」用的 excerpt）。

摘要有兩個容易寫錯的地方，各釘一條：
1. 短內文不該被加上「…」—— 加了會讓人以為後面還有字。
2. 長內文要切在句尾，而不是剛好第 180 個字元（切在半句話中間讀起來像壞掉）。

跑法：
    python tests/test_parse.py        # 不須 pytest
    python -m pytest tests/            # 有 pytest 也可以
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from osu_tourney import parse  # noqa: E402


def detail(body: str) -> parse.TopicDetail:
    return parse.TopicDetail(topic_id=1, body_text=body)


def test_empty_body_has_no_excerpt():
    assert detail("").excerpt == ""
    assert detail("   ").excerpt == ""


def test_short_body_is_returned_whole_without_ellipsis():
    text = "SMST 83 報名開始，1v1 單淘汰。"
    out = detail(text).excerpt
    assert out == text
    assert "…" not in out


def test_long_body_is_cut_at_a_sentence_end():
    text = "歡迎來到 SMST 83。" + "這是第二句話，用來把長度推過上限。" * 20
    out = detail(text).excerpt
    assert out.endswith("…")
    body = out[:-1]  # 去掉省略號
    assert len(body) <= parse.EXCERPT_CHARS
    assert text.startswith(body)
    # 切點必須落在原文的句尾，不能是半句話。
    assert body.endswith("。") or body.endswith(".")


def test_a_body_with_no_sentence_end_still_yields_something():
    """沒有任何標點（例如整段是網址或表格）時硬切，但不能切出空字串。"""
    out = detail("A" * 500).excerpt
    assert out.endswith("…")
    assert len(out) > parse.EXCERPT_CHARS // 2


def test_a_separator_in_the_first_half_is_ignored():
    """只在前半段出現句號時不該提早收工 —— 那會讓摘要短到沒有資訊。"""
    text = "嗨。" + "B" * 400
    out = detail(text).excerpt
    assert len(out) >= parse.EXCERPT_CHARS


def test_excerpt_never_invents_text():
    """摘要必須是原文的前綴（去掉省略號後）—— 不摘要、不改寫。"""
    text = "第一句。" * 60
    out = detail(text).excerpt
    body = out[:-1]
    assert text.startswith(body)


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
