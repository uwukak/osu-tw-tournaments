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
    }


def _write_drafts(
    tournaments: dict[str, Any],
    baseline: Optional[int],
    drafts_dir: Path,
) -> list[str]:
    """為 baseline 之後的收錄／待確認賽事產生草稿。

    若使用者手改過草稿（檔案雜湊與我們上次寫入的不同），就**不要覆蓋**他的修改。
    """
    written: list[str] = []
    drafts_dir.mkdir(parents=True, exist_ok=True)

    for record in tournaments.values():
        if record.get("decision") not in ("include", "review"):
            continue
        tid = int(record["topic_id"])
        if baseline is not None and tid <= baseline:
            continue

        content = render.render_draft(record)
        path = drafts_dir / f"{tid}.md"
        digest = _sha(content)

        if path.exists():
            existing = path.read_text(encoding="utf-8")
            if record.get("draft_sha") and _sha(existing) != record["draft_sha"]:
                log.info("草稿 %s 已被手動修改，保留不覆蓋。", path.name)
                continue
            if existing == content:
                continue

        with path.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)
        record["draft_sha"] = digest
        written.append(path.name)

    return written


def run(args: argparse.Namespace) -> int:
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat(timespec="seconds")

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
        needs_details = is_new or missing_details or (
            existing is not None
            and existing.get("status") == rules.STATUS_OPEN
            and _is_stale(existing, now, args.refresh_days)
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

    new_data = {
        "schema_version": store.SCHEMA_VERSION,
        "last_run_utc": now_iso,
        "heartbeat_date": now.date().isoformat(),
        "draft_baseline_topic_id": baseline,
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
        tournaments,
        meta={
            "last_run_utc": now_iso,
            "last_run_taipei": to_taipei(now_iso) or "",
        },
        now_iso=now_iso,
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
        written = _write_drafts(tournaments, baseline, DRAFTS_DIR)
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


if __name__ == "__main__":
    raise SystemExit(main())
