"""進入點：抓取 → 抽取 → 判定 → 存檔 → 產看板 → 產草稿 →（可選）提交。

本機與 GitHub Actions 跑的是同一份程式，只靠參數區分：
    python scrape.py --no-push          # 本機驗證（預設）
    python scrape.py --push             # 排程用（Actions 會帶這個）
    python scrape.py --seed-only        # 首次回填：寫資料與看板，但不噴一堆草稿
    python scrape.py --offline          # 用 fixtures/ 跑，完全不碰網路
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from . import parse, render, rules, store
from .fetch import Fetcher, FetchError
from .gitio import publish
from .render import to_taipei

ROOT = Path(__file__).resolve().parent.parent
DATA_PATH = ROOT / "data" / "tournaments.json"
OVERRIDES_PATH = ROOT / "data" / "overrides.json"
CUSTOM_PATH = ROOT / "data" / "custom.json"
DOCS_DIR = ROOT / "docs"
DRAFTS_DIR = ROOT / "drafts"
FIXTURES = ROOT / "fixtures"

log = logging.getLogger("scrape")


class OfflineSource:
    """用 fixtures/ 當資料來源，供本機測試。只認得 fixture 裡真的有的主題。"""

    # topic.html 是 2252361（SMST 84 50K-100K）的真實快照，只對這個 id 誠實。
    FIXTURE_TOPIC_ID = 2252361

    def __init__(self, fixtures: Path) -> None:
        self.fixtures = fixtures

    def listing(self, page: int = 1) -> str:
        return (self.fixtures / "forum55_created.html").read_text(encoding="utf-8")

    def topic(self, topic_id: int) -> Optional[str]:
        named = self.fixtures / f"topic_{topic_id}.html"
        if named.exists():
            return named.read_text(encoding="utf-8")

        # 刻意不做「找不到就一律拿 topic.html 頂替」的退路。那會讓離線跑一次就把
        # SMST 84 的截止時間與 Discord 寫進其他 49 筆；而 details_fetched_at 一旦
        # 寫入，非報名中的賽事就不會再重抓，錯的資料會永久留在資料檔裡。
        if topic_id == self.FIXTURE_TOPIC_ID:
            generic = self.fixtures / "topic.html"
            if generic.exists():
                return generic.read_text(encoding="utf-8")
        return None


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _is_stale(record: dict[str, Any], now: datetime, days: int) -> bool:
    stamp = record.get("details_fetched_at")
    if not stamp:
        return True
    try:
        fetched = datetime.fromisoformat(stamp)
    except ValueError:
        return True
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=timezone.utc)
    return now - fetched > timedelta(days=days)


def _fields_from_row(row: parse.TopicRow, verdict: rules.Verdict) -> dict[str, Any]:
    return {
        "topic_id": row.topic_id,
        "title": row.title,
        "author": row.author,
        "posts": row.posts,
        "views": row.views,
        "last_reply_at": row.last_reply_at,
        "name": verdict.name,
        "mode": verdict.mode,
        "mode_label": verdict.mode_label,
        "mode_confidence": verdict.mode_confidence,
        "mania_keys": verdict.mania_keys,
        "rank": list(verdict.rank) if verdict.rank else None,
        "rank_compact": verdict.rank_compact,
        "rank_full": rules.format_rank_full(verdict.rank),
        "teams": verdict.teams,
        "team_sizes": verdict.team_sizes,
        "status": verdict.status,
        "decision": verdict.decision,
        "reason": verdict.reason,
        "is_staff_topic": verdict.is_staff_topic,
        "kind": verdict.kind,
    }


def _write_drafts(
    tournaments: dict[str, Any],
    baseline: Optional[int],
    drafts_dir: Path,
    now_iso: str,
    overrides: Optional[dict[str, Any]] = None,
    custom: Optional[dict[str, Any]] = None,
    custom_draft_sha: Optional[dict[str, str]] = None,
) -> tuple[list[str], dict[str, str]]:
    """為 baseline 之後的收錄／待確認賽事產生草稿。

    若使用者手改過草稿（檔案雜湊與我們上次寫入的不同），就**不要覆蓋**他的修改。

    `overrides` 是站長在看板上手改的內容。草稿要照手改的走 —— 名次判錯、Discord
    抓錯正是他動手改的原因，草稿卻寫著舊的，貼出去就是發錯文，而且比看板標錯更難查
    （貼出去就收不回來了）。

    `custom` 是站長手動新增的賽事，也要產生草稿。它們的雜湊不能寫回記錄 ——
    記錄住在 data/custom.json，那是使用者的檔案，爬蟲只讀不寫。所以改記在
    回傳的 `custom_draft_sha`（呼叫端存進 tournaments.json 的頂層）。
    """
    overrides = overrides or {}
    custom_draft_sha = dict(custom_draft_sha or {})
    written: list[str] = []
    drafts_dir.mkdir(parents=True, exist_ok=True)

    for record in store.merge_custom(tournaments, custom).values():
        own = store.override_for(overrides, record.get("topic_id"))
        # 收錄判定也可能被手改（把「待確認」直接升成「收錄」、或反過來排除掉）。
        # 這裡要用疊過去的結果，否則會出現「看板收錄了、草稿卻沒產生」這種對不上的狀態。
        if render.apply_override(record, own).get("decision") not in ("include", "review"):
            continue
        tid = int(record["topic_id"])
        is_custom = bool(record.get("custom"))

        # 手動新增的賽事不受 baseline 限制。它們的 id 是負數，而 baseline 是爬蟲那邊
        # （兩百多萬）的 id —— 負數恆小於它，照這條擋的話每一筆都會被靜默跳過，
        # 看板上有、drafts/ 裡卻永遠生不出草稿。
        if not is_custom and baseline is not None and tid <= baseline:
            continue

        # 傳 now_iso：草稿是要貼出去的，標題的報名狀態必須先被截止時間校正過。
        content = render.render_draft(record, now_iso, own)
        path = drafts_dir / f"{tid}.md"
        digest = _sha(content)
        key = str(tid)
        # 上次寫出去的雜湊。爬蟲的記在記錄裡，手動新增的記在呼叫端給的那本帳。
        last = custom_draft_sha.get(key) if is_custom else record.get("draft_sha")

        if path.exists():
            existing = path.read_text(encoding="utf-8")
            if last and _sha(existing) != last:
                log.info("草稿 %s 已被手動修改，保留不覆蓋。", path.name)
                continue
            if existing == content:
                # 內容一樣，不重寫，但把雜湊補上 —— 少了這一步，之後有人手改這個
                # 檔案時我們認不出來（last 還是空的），下一回合就把他改的蓋掉了。
                if is_custom:
                    custom_draft_sha.setdefault(key, digest)
                continue

        with path.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)
        if is_custom:
            custom_draft_sha[key] = digest
        else:
            record["draft_sha"] = digest
        written.append(path.name)

    return written, custom_draft_sha


def run(args: argparse.Namespace) -> int:
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat(timespec="seconds")

    # 0) 站長的覆寫內容。刻意放在最前面、也在抓取之前 —— 檔案壞掉就當場說清楚，
    # 不要先花掉一次請求才發現。這份檔案只有人會寫（看板上的編輯介面），
    # 爬蟲永遠不寫它，所以使用者的修改不會被下一回合蓋掉。
    overrides = store.load_overrides(OVERRIDES_PATH)
    if overrides:
        log.info("讀到 %d 筆站長說明。", len(overrides))

    # 站長手動新增的賽事（看板上的「新增賽事」）。跟 overrides.json 一樣只有人會寫。
    custom = store.load_custom(CUSTOM_PATH)
    if custom:
        log.info("讀到 %d 筆手動新增的賽事。", len(custom))

    # 看板上的「站長編輯」要靠 repo 與分支才能呼叫 GitHub API。Actions 每次執行都會
    # 帶 GITHUB_REPOSITORY／GITHUB_REF_NAME，所以排程產生的頁面自動就有；
    # 本機要測這個功能時自己帶 --repo。
    page_meta = {
        "repo": args.repo,
        "branch": args.branch,
        "last_run_utc": now_iso,
        "last_run_taipei": to_taipei(now_iso) or "",
    }

    source = OfflineSource(FIXTURES) if args.offline else Fetcher()

    # 1) 抓列表。失敗就整個中止 —— 絕不用空資料覆蓋既有歷史。
    try:
        listing_html = source.listing(page=1)
    except FetchError as exc:
        log.error("抓取列表失敗，中止且不寫入任何檔案：%s", exc)
        return 1

    rows = parse.parse_listing(listing_html)
    if not rows:
        log.error("列表解析出 0 筆主題 —— 結構可能改了。中止，不寫入。")
        return 1
    log.info("列表取得 %d 筆主題。", len(rows))

    # 2) 讀既有資料。務必用 detach 取複本 —— 見 store.detach 的說明：
    # 直接拿 old["tournaments"] 會被就地改掉，變更偵測就永遠失效。
    old = store.load(DATA_PATH)
    tournaments: dict[str, Any] = store.detach(old)

    # 3) 逐筆抽取
    detail_budget = args.limit if args.limit else 10_000
    new_count = 0

    for row in rows:
        if args.since_topic_id and row.topic_id <= args.since_topic_id:
            continue

        verdict = rules.analyze(row.title)
        fields = _fields_from_row(row, verdict)

        existing = store.get(tournaments, row.topic_id)
        is_new = existing is None
        # 明細從沒抓成功過（上次抓主題頁失敗、或離線模式沒有該筆的快照）→ 一定要重試。
        # 少了這一條，非「報名中」的賽事永遠不會落到下面的 stale 規則，discord 與
        # 截止時間就永久缺漏。
        missing_details = not (existing or {}).get("details_fetched_at")
        # --refetch-details 是「解析器改版」專用的一次性開關。
        #
        # 為什麼需要它：明細裡的欄位（excerpt、deadline_raw、discord…）都只在下載主題頁
        # 那一刻算出來。解析器修好之後，畫面上看起來「資料都在」，其實每一筆留著的都是
        # 舊解析器的產物 —— 而平常的重抓規則綁在「標題說報名中」上，標題寫 unknown 或
        # 已截止的賽事就永遠不會被重算，錯誤的欄位會一直留在那裡。
        needs_details = (
            args.refetch_details
            or is_new
            or missing_details
            or (
                existing is not None
                and existing.get("status") == rules.STATUS_OPEN
                and _is_stale(existing, now, args.refresh_days)
            )
        )

        if is_new:
            new_count += 1

        if needs_details and detail_budget > 0:
            detail_budget -= 1
            try:
                topic_html = source.topic(row.topic_id)
            except FetchError as exc:
                log.warning("主題 %s 抓取失敗，先跳過明細：%s", row.topic_id, exc)
                topic_html = None

            if topic_html:
                detail = parse.parse_topic(topic_html)
                fields["created_at"] = detail.created_at
                fields["discord"] = detail.discord
                fields["signup_form"] = detail.signup_form
                fields["stream"] = detail.stream
                # 抓不到內文時不要用空字串蓋掉已經存好的摘要 —— 那會讓看板上的說明
                # 因為一次暫時性的解析失敗而整段消失。寧可留著上一次的節錄：
                # 它標著「原帖首段節錄」又附原帖連結，稍微過時遠比整段不見好。
                if detail.excerpt:
                    fields["excerpt"] = detail.excerpt
                if detail.author and not fields.get("author"):
                    fields["author"] = detail.author
                raw, iso = rules.extract_deadline(detail.body_text, detail.created_at)
                fields["deadline_raw"] = raw
                fields["deadline_iso"] = iso
                fields["details_fetched_at"] = now_iso

        # deadline_iso 完全是 deadline_raw 這段文字的純函式，所以就算這回合沒重抓明細，
        # 也要用當前的解析器重算一次 —— 改進解析規則之後，已經抓過的舊資料重跑就會自動
        # 修正，不必等 refresh_days 到期重抓。少了這一步，修好的規則要三天後才生效。
        raw_sentence = (
            fields["deadline_raw"]
            if "deadline_raw" in fields
            else (existing or {}).get("deadline_raw")
        )
        if raw_sentence:
            created = fields.get("created_at") or (existing or {}).get("created_at")
            fields["deadline_iso"] = rules.parse_deadline_iso(raw_sentence, created)

        if is_new:
            log.info(
                "新賽事 %s｜%s｜%s｜%s｜%s",
                row.topic_id, verdict.mode_label, verdict.name,
                verdict.rank_compact or "-", verdict.decision,
            )

        store.upsert(tournaments, fields, now_iso)

    store.mark_off_listing(tournaments, {r.topic_id for r in rows}, now_iso)
    log.info("本回合新增 %d 筆。", new_count)

    # 4) 組出新的資料檔
    baseline = old.get("draft_baseline_topic_id")
    if args.seed_only:
        baseline = max([r.topic_id for r in rows] + ([baseline] if baseline else [0]))

    # 看板本身也要算進「有沒有變更」。
    #
    # 為什麼：只改 render.py（例如修正報名狀態的判定）而資料一個字都沒動時，光看資料
    # 會回報「無變更」→ 不寫檔 → 修正永遠不生效，看板停在舊版直到隔天 heartbeat 才換。
    # 用看板內容的雜湊把產物納入判斷，改 render 就會在下一個回合反映出來。
    #
    # meta 傳空的：last_run_utc 每個回合都不同，留在裡面會變成每 30 分鐘一次假提交。
    # 但 now_iso 要照傳 —— 它會經由 effective_status 影響內容，代表「截止時間一到，
    # 看板自己就會更新」，這正是我們要的行為。
    #
    # repo／branch 也**刻意不傳**：那兩個值只有 Actions 有（GITHUB_REPOSITORY），
    # 本機是空的。放進來的話，本機與排程會對同一個資料算出不同的 dashboard_sha，
    # 兩邊互推、每次換環境就多一次假提交 —— 正是這道守門要擋掉的東西。
    #
    # overrides 則一定要傳 —— 站長在看板上改的那幾個字，就是靠這裡進到 dashboard_sha，
    # 下一回合的爬蟲才會發現「看板內容變了」而去重繪、提交。
    #
    # 前提是 build_payload 對相同輸入必須產生位元組相同的輸出（render.py 是純函式，
    # 排序也補了 topic_id 這個 tiebreaker），否則這裡會變成假提交製造機。
    dashboard_sha = _sha(
        render.render_dashboard(
            render.build_payload(
                tournaments, meta={}, now_iso=now_iso, overrides=overrides, custom=custom
            )
        )
    )

    # 手動新增賽事的草稿雜湊。**一定要從 old 帶過來**：只在 _write_drafts 之後才把
    # 這個 key 加上去的話，old 沒有、new_data 有，store.meaningful 每回合都判定
    # 「有變更」→ 每 30 分鐘一次假提交，永遠不停。
    #
    # 順手修剪成還活著的 id（那是爬蟲自己的帳，不是使用者的檔案）——
    # 刪掉的手動賽事不該在 tournaments.json 裡留下永遠不會再用的 key。
    #
    # key 用 custom_record 算出來的那個（＝記錄自己的 topic_id），不是 custom.json 的
    # 外層 key：_write_drafts 記帳時用的就是前者。兩者只有在人手改過檔案、把外層 key
    # 跟內容的 topic_id 寫得不一致時才會分岔，而那時若用外層 key，每回合都會把這筆
    # 的雜湊當成不存在 → 站長改過的草稿被默默蓋掉。
    live_custom = {
        str(rec["topic_id"])
        for rec in (store.custom_record(k, v) for k, v in custom.items())
        if rec
    }
    custom_draft_sha = {
        k: v for k, v in (old.get("custom_draft_sha") or {}).items() if k in live_custom
    }

    new_data = {
        "schema_version": store.SCHEMA_VERSION,
        "last_run_utc": now_iso,
        "heartbeat_date": now.date().isoformat(),
        "dashboard_sha": dashboard_sha,
        "draft_baseline_topic_id": baseline,
        "custom_draft_sha": custom_draft_sha,
        "tournaments": tournaments,
    }

    changed = store.has_meaningful_change(old, new_data)
    heartbeat_due = old.get("heartbeat_date") != new_data["heartbeat_date"]

    if not changed and not heartbeat_due:
        log.info("資料無變更，不寫檔也不提交。")
        return 0

    # 5) 寫檔（先寫資料，再寫產物）
    store.save(DATA_PATH, new_data)

    payload = render.build_payload(
        tournaments, meta=page_meta, now_iso=now_iso, overrides=overrides, custom=custom
    )
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    with (DOCS_DIR / "index.html").open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(render.render_dashboard(payload))
    log.info("看板已更新：%d 筆（收錄 %d／待確認 %d）",
             len(payload["tournaments"]), payload["counts"]["include"], payload["counts"]["review"])

    # 6) 草稿
    if args.seed_only:
        log.info("--seed-only：不產生草稿（baseline topic id = %s）。", baseline)
    else:
        written, custom_draft_sha = _write_drafts(
            tournaments, baseline, DRAFTS_DIR, now_iso, overrides, custom, custom_draft_sha
        )
        new_data["custom_draft_sha"] = custom_draft_sha
        if written:
            log.info("產生 %d 份草稿：%s", len(written), ", ".join(written))
        with (DRAFTS_DIR / "README.md").open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(render.render_index_markdown(payload))

    # 草稿雜湊寫回資料檔
    new_data["tournaments"] = tournaments
    store.save(DATA_PATH, new_data)

    # 7) 提交
    if args.push:
        message = f"更新賽事資料（新增 {new_count} 筆，共 {len(tournaments)} 筆）"
        # 能走到這裡，代表上面已經確認過有實質變更（changed / heartbeat_due 守門），
        # 所以 publish 回 False 不可能是「無變更可提交」，只可能是真的失敗
        # （不是 git 工作區，或 push 被拒）。這種必須讓排程變紅 ——
        # 否則 Actions 一片綠，看板卻永遠停在舊資料，是最難查的靜默失敗。
        if not publish(ROOT, message, push=True):
            log.error("提交／推送失敗：資料只留在本次執行的暫存檔，沒有進版控。")
            return 1
    else:
        log.info("--no-push：僅寫入本機檔案。")

    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="osu! 台灣可參加賽事彙整")
    parser.add_argument("--push", action="store_true", help="提交並推送（排程用）")
    parser.add_argument("--no-push", dest="push", action="store_false", help="只寫本機檔案（預設）")
    parser.set_defaults(push=False)
    parser.add_argument("--seed-only", action="store_true", help="首次回填：不產生草稿")
    parser.add_argument("--offline", action="store_true", help="用 fixtures/ 跑，不碰網路")
    parser.add_argument("--limit", type=int, default=0, help="本回合最多抓幾個主題頁（測試用）")
    parser.add_argument("--since-topic-id", type=int, default=0, help="只處理 id 大於此值的主題")
    parser.add_argument("--refresh-days", type=int, default=3, help="報名中的賽事幾天後重抓明細")
    parser.add_argument(
        "--refetch-details",
        action="store_true",
        help="忽略新鮮度與報名狀態，重抓本次列表上每個主題的明細（改過解析器之後用一次）",
    )
    # 這兩個只影響看板上的「站長編輯」按鈕能不能用。Actions 每次執行都自動帶
    # GITHUB_REPOSITORY／GITHUB_REF_NAME，所以排程那條路不用設定。
    parser.add_argument(
        "--repo",
        default=os.environ.get("GITHUB_REPOSITORY", ""),
        help="owner/repo，看板的編輯功能用它呼叫 GitHub API（Actions 會自動提供）",
    )
    parser.add_argument(
        "--branch",
        default=os.environ.get("GITHUB_REF_NAME", "main"),
        help="看板編輯要提交到哪個分支（預設 main）",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
        stream=sys.stdout,
    )

    try:
        return run(args)
    except FetchError as exc:
        log.error("抓取失敗，已中止：%s", exc)
        return 1
    except store.OverridesError as exc:
        # 看板上的站長說明全部來自這個檔案。讀不懂就整個停下來、讓排程變紅 ——
        # 當成空檔繼續跑的話，說明會整批從看板上消失，而且因為 dashboard_sha
        # 也跟著變了，這個「消失」還會被當成一次正常的更新提交出去。
        log.error("站長說明讀不進來，已中止（不寫檔、不提交）：%s", exc)
        return 1
    except store.CustomError as exc:
        # 同理：手動新增的賽事全部來自 data/custom.json。
        log.error("手動新增的賽事讀不進來，已中止（不寫檔、不提交）：%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
