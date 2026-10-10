"""唯一呼叫 git 的地方。

兩個要擋掉的失敗模式：
1. 執行後資料沒變，卻還是提交了一次 → 用 `git status --porcelain` 守門。
2. 本機排程與 GitHub Actions 同時觸發 → rebase 後再檢查一次，
   若對方已經提交了同樣的資料就放棄 push（不硬推）。
"""
from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

BOT_NAME = "github-actions[bot]"
BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"


class GitError(RuntimeError):
    pass


def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        ("git", *args),
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise GitError(f"git {' '.join(args)} 失敗：{result.stderr.strip()}")
    return result.stdout


def is_repo(cwd: Path) -> bool:
    try:
        _git("rev-parse", "--is-inside-work-tree", cwd=cwd)
        return True
    except (GitError, FileNotFoundError):
        return False


def repo_root(cwd: Path) -> Path | None:
    """這個目錄所屬的 git 倉庫根目錄。不是 repo 就回 None。"""
    try:
        out = _git("rev-parse", "--show-toplevel", cwd=cwd)
    except (GitError, FileNotFoundError):
        return None
    return Path(out.strip()).resolve()


def _same_path(a: Path, b: Path) -> bool:
    # Windows 的大小寫與斜線寫法都可能不同（git 回 "C:/..."，Path 回 "C:\\..."）。
    return os.path.normcase(str(a)) == os.path.normcase(str(b))


def _has_conflicts(cwd: Path) -> bool:
    """索引裡有沒有未解的衝突。

    rebase 撞到衝突時，索引會為同一個檔案留下 stage 1/2/3 三個項目，
    `git ls-files -u` 只列這些。比起解析 `git status` 的字串（UU／AA／DD…），
    這個判斷不會隨 git 版本與語系而變。
    """
    try:
        return bool(_git("ls-files", "-u", cwd=cwd).strip())
    except GitError:
        return False


def _abort_rebase(cwd: Path) -> None:
    """把停在半途的 rebase 收掉，讓工作區回到我們自己的提交。"""
    try:
        _git("rebase", "--abort", cwd=cwd)
        log.warning("已中止半途的 rebase。")
    except GitError:
        pass  # 本來就沒有 rebase 在跑


def publish(cwd: Path, message: str, push: bool = True) -> bool:
    """提交目前的變更。回傳是否真的產生了提交。"""
    if not is_repo(cwd):
        log.warning("不是 git 工作區，跳過提交：%s", cwd)
        return False

    # 這道檢查擋的是最貴的一種意外：專案目錄自己不是 repo，但**上層**有一個。
    # 真實在 Windows 上遇過的情況是家目錄被 git init 過 —— 於是
    #   git rev-parse --is-inside-work-tree  → True（往上找到家目錄那個 repo）
    #   git add -A                           → 從 git 2.0 起是「整個工作區」，不是當前目錄
    # 結果是把 AppData、NTUSER.DAT、瀏覽器 cookie 全部 commit 起來推上公開 repo。
    # 與其祈禱使用者記得先 git init，不如在這裡直接拒絕。
    root = repo_root(cwd)
    here = Path(cwd).resolve()
    if root is None or not _same_path(root, here):
        log.error(
            "拒絕提交：git 倉庫根目錄是 %s，不是專案目錄 %s。\n"
            "  上層有另一個 repo 時，git add -A 會作用於整個上層目錄。\n"
            "  請先在專案目錄執行 `git init`，再重試。",
            root, here,
        )
        return False

    if os.environ.get("CI"):
        _git("config", "user.name", BOT_NAME, cwd=cwd)
        _git("config", "user.email", BOT_EMAIL, cwd=cwd)

    if not _git("status", "--porcelain", cwd=cwd).strip():
        log.info("沒有變更，不提交。")
        return False

    _git("add", "-A", cwd=cwd)
    _git("commit", "-m", message, cwd=cwd)
    log.info("已提交：%s", message)

    if not push:
        log.info("--no-push：不推送。")
        return True

    # 先同步遠端。--autostash 讓未提交的變更不會擋住 rebase。
    try:
        _git("pull", "--rebase", "--autostash", cwd=cwd)
    except GitError as exc:
        if _has_conflicts(cwd):
            # 這裡無論如何都不能往下走。rebase 停在半途時工作區留著 <<<<<<< 標記，
            # 底下那段 `add -A` + `commit` 會**把衝突標記當成內容提交上去** ——
            # data/tournaments.json 就此爛掉（它就是要餵給看板的 JSON），而且會推上線。
            # 寧可整個回合不推：遠端維持原狀，代價只是這一輪資料晚半小時。
            _abort_rebase(cwd)
            log.error(
                "rebase 撞到衝突，已中止並放棄本回合（未推送）：%s\n"
                "  本機與遠端改了同一份生成檔。請手動 `git pull --rebase` 解掉再重跑。",
                exc,
            )
            return False
        # 沒有遠端、遠端還沒有這個分支、暫時連不上 —— 這些不影響後面的 push。
        log.warning("rebase 失敗（可能沒有遠端或首次推送）：%s", exc)

    if _git("status", "--porcelain", cwd=cwd).strip():
        # rebase 之後又冒出變更（對方推了不同資料）→ 再提交一次。
        _git("add", "-A", cwd=cwd)
        _git("commit", "-m", message, cwd=cwd)

    try:
        _git("push", cwd=cwd)
    except GitError as exc:
        log.error("推送失敗：%s", exc)
        return False

    return True
