"""gitio.py 的危險路徑測試。

只驗一件事情：**rebase 撞到衝突時，絕不把衝突標記提交出去**。

生成檔（data/tournaments.json）是直接餵給看板的 JSON。一旦提交了 `<<<<<<<`
就等於整頁壞掉，而且會推上線。這條路徑平常跑不到 —— Actions 的 concurrency
擋掉了兩個回合重疊 —— 只有「本機落後遠端一格、兩邊又剛好都改了同一份資料」
時才會踩到，正是最不容易發現的那一種。

所以它值得一個真的 git 倉庫來測，而不是靠讀程式碼推論。

跑法：
    python tests/test_gitio.py
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from osu_tourney.gitio import publish  # noqa: E402

DATA = "data/tournaments.json"


def git(*args: str, cwd: Path) -> str:
    """在臨時倉庫裡跑 git。失敗就讓測試爆掉，不安靜吞掉。"""
    done = subprocess.run(
        ("git", *args),
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert done.returncode == 0, f"git {' '.join(args)} 失敗：{done.stderr.strip()}"
    return done.stdout


def init_repo(path: Path, bare: bool = False) -> Path:
    """開一個臨時倉庫。

    身分要自己設：CI runner 上沒有全域 git 身分，不設就 commit 不了。
    """
    path.mkdir(parents=True)
    git("init", "-b", "main", *(["--bare"] if bare else []), cwd=path)
    git("config", "user.name", "test", cwd=path)
    git("config", "user.email", "test@example.com", cwd=path)
    git("config", "commit.gpgsign", "false", cwd=path)
    return path


def write_file(repo: Path, relpath: str, text: str) -> None:
    target = repo / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8", newline="\n")


def seed(tmp: Path) -> tuple[Path, Path]:
    """建立 remote（裸倉庫）與 work（本機），並推一份初始資料上去。"""
    remote = init_repo(tmp / "remote", bare=True)
    work = init_repo(tmp / "work")
    git("remote", "add", "origin", str(remote), cwd=work)
    write_file(work, DATA, '{"1": "v1"}\n')
    git("add", "-A", cwd=work)
    git("commit", "-m", "初始資料", cwd=work)
    git("push", "-u", "origin", "main", cwd=work)
    return remote, work


def someone_else_pushes(tmp: Path, remote: Path, relpath: str, text: str) -> None:
    """模擬遠端已經被推走一格。

    用 clone 而不是直接改 work —— 這樣 work 才會真的落後一個提交，
    publish 裡的 `pull --rebase` 才有東西要重播。
    """
    other = tmp / "other"
    git("clone", str(remote), str(other), cwd=tmp)
    git("config", "user.name", "other", cwd=other)
    git("config", "user.email", "other@example.com", cwd=other)
    git("config", "commit.gpgsign", "false", cwd=other)
    write_file(other, relpath, text)
    git("add", "-A", cwd=other)
    git("commit", "-m", "別人推的", cwd=other)
    git("push", cwd=other)


def remote_file(remote: Path, relpath: str, cwd: Path) -> str:
    return git("--git-dir", str(remote), "show", f"main:{relpath}", cwd=cwd)


# --------------------------------------------------------------------------


def test_a_conflicting_rebase_does_not_commit_conflict_markers():
    """兩邊都改了同一份生成檔 → publish 必須放棄，而不是提交衝突標記。

    這是這個檔案存在的理由。修好之前，publish 會把 `<<<<<<<` 當成內容
    commit 起來再推上去 —— 看板整頁壞掉，而且沒人會立刻發現。
    """
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        remote, work = seed(tmp)
        someone_else_pushes(tmp, remote, DATA, '{"1": "別人推的"}\n')

        write_file(work, DATA, '{"1": "本機抓的"}\n')
        published = publish(work, "更新賽事資料", push=True)

        assert published is False, "有衝突時不該回報成功"

        text = (work / DATA).read_text(encoding="utf-8")
        assert "<<<<<<<" not in text, f"衝突標記被留在檔案裡：\n{text}"
        assert text == '{"1": "本機抓的"}\n', f"rebase --abort 之後該回到本機版本，實際 {text!r}"

        assert not git("ls-files", "-u", cwd=work).strip(), "rebase 沒有收乾淨"

        assert remote_file(remote, DATA, tmp) == '{"1": "別人推的"}\n', "不該推任何東西上去"


def test_a_clean_rebase_still_publishes():
    """遠端動了、但動的是別的檔案 → 照常重播、照常發佈。

    這條是煞車：上面的修正不可以把「本機落後遠端」這種**正常**情況也一起擋掉。
    改壞的話，每 30 分鐘的排程會從此不再提交。
    """
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        remote, work = seed(tmp)
        someone_else_pushes(tmp, remote, "docs/index.html", "<html>別人的</html>\n")

        write_file(work, DATA, '{"1": "本機抓的"}\n')
        published = publish(work, "更新賽事資料", push=True)

        assert published is True, "沒有衝突時應該發佈成功"
        assert remote_file(remote, DATA, tmp) == '{"1": "本機抓的"}\n', "本機的資料沒有推上去"
        assert (
            remote_file(remote, "docs/index.html", tmp) == "<html>別人的</html>\n"
        ), "別人推的那個檔案不該被蓋掉"


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
