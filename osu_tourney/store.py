"""讀寫 data/tournaments.json。

關鍵不變量：**相同輸入 → 位元組相同輸出**。這是「無變更就不提交」能成立的前提，
也是讓本機與 GitHub Actions 兩個環境不會互相比對出假差異的基礎。

所以：key 排序、`ensure_ascii=False`、`newline="\\n"`、不在此處讀時鐘
（時間一律由呼叫端傳入）。
"""
from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

SCHEMA_VERSION = 1

# 每次執行都會變、但不算「資料變更」的欄位。
VOLATILE_KEYS = {"last_seen_utc", "details_fetched_at"}

# 掉出論壇第一頁之後，看板還保留多久（天）。
# 仍在報名的賽事不受此限，永遠顯示。
DASHBOARD_WINDOW_DAYS = 45


def load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return empty()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        # 壞掉的檔案不要默默吞掉，也不要在這裡覆蓋 —— 讓呼叫端決定。
        raise
    data.setdefault("schema_version", SCHEMA_VERSION)
    data.setdefault("tournaments", {})
    return data


def empty() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "last_run_utc": None, "heartbeat_date": None, "tournaments": {}}


def save(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


# --------------------------------------------------------------------------
# 變更偵測
# --------------------------------------------------------------------------


def detach(data: dict[str, Any]) -> dict[str, Any]:
    """取出可以安全就地修改的 tournaments 複本。

    `upsert` 與 `mark_off_listing` 都是**就地修改**，而 `data["tournaments"]`
    正是同一個 dict 物件。直接把 `data["tournaments"]` 傳進去，等於連 `data`
    本身一起改掉 —— 之後 `has_meaningful_change(data, ...)` 就是拿修改後的
    狀態跟自己比，**永遠回報「無變更」**。下場是 changed 恆為 False，
    「有變更才提交」的守門失效，只剩換日的 heartbeat 會寫檔，
    當天發現的新賽事全部被吞到隔天才出現。

    所以：改資料之前一定要先 detach。
    """
    return copy.deepcopy(data.get("tournaments", {}))


def meaningful(data: dict[str, Any]) -> dict[str, Any]:
    """去掉每次都會變的時間戳，只留下真正代表資料的內容。

    沒有這一層，「有變更才提交」的守門就會失效 ——
    last_seen_utc 每次執行都不同，會導致每 30 分鐘產生一次假提交。
    """
    d = copy.deepcopy(data)
    d.pop("last_run_utc", None)
    for record in d.get("tournaments", {}).values():
        for key in VOLATILE_KEYS:
            record.pop(key, None)
    return d


def has_meaningful_change(old: dict[str, Any], new: dict[str, Any]) -> bool:
    return meaningful(old) != meaningful(new)


# --------------------------------------------------------------------------
# 寫入
# --------------------------------------------------------------------------


def upsert(tournaments: dict[str, Any], fields: dict[str, Any], now_iso: str) -> bool:
    """新增或更新一筆賽事。回傳是否有實際變更。

    記錄標題歷史：osu! 的標題是「可變的」，主辦會把 `[Open]` 改成 `[REGS CLOSED]`，
    所以狀態更新可以單純靠列表頁的標題，不必額外抓主題頁。
    """
    tid = str(fields["topic_id"])
    old = tournaments.get(tid)

    if old is None:
        record = dict(fields)
        record["first_seen_utc"] = now_iso
        record["last_seen_utc"] = now_iso
        record["title_history"] = [fields["title"]]
        tournaments[tid] = record
        return True

    changed = False
    record = dict(old)

    for key, value in fields.items():
        if key == "title":
            if record.get("title") != value:
                history = list(record.get("title_history", []))
                if value not in history:
                    history.append(value)
                record["title_history"] = history
                record["title"] = value
                changed = True
        elif record.get(key) != value:
            record[key] = value
            changed = True

    record["last_seen_utc"] = now_iso

    # 重新出現在列表上（先前掉出去過）→ 取消標記。
    if record.pop("off_listing_since", None) is not None:
        changed = True

    tournaments[tid] = record
    return changed


def mark_off_listing(tournaments: dict[str, Any], seen_ids: set[int], now_iso: str) -> bool:
    """主題掉出列表第一頁（或已在論壇被刪除）→ 標記，但**不刪除資料**。

    這裡刻意不把它從看板直接隱藏：論壇第一頁只有 50 帖，還在報名中的比賽
    很快就會被新帖擠掉。要不要顯示由 render 依「是否仍在報名／最後出現時間」決定。
    """
    changed = False
    for tid, record in tournaments.items():
        if int(tid) not in seen_ids and "off_listing_since" not in record:
            record["off_listing_since"] = now_iso
            changed = True
    return changed


def is_fresh(
    record: dict[str, Any], now_iso: str, days: int = DASHBOARD_WINDOW_DAYS
) -> bool:
    """這筆資料最近還在論壇列表上嗎（或剛被發現）？"""
    stamp = record.get("last_seen_utc") or record.get("first_seen_utc")
    if not stamp:
        return False
    try:
        seen = datetime.fromisoformat(stamp)
        now = datetime.fromisoformat(now_iso)
    except ValueError:
        return False
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now - seen <= timedelta(days=days)


def get(tournaments: dict[str, Any], topic_id: int) -> Optional[dict[str, Any]]:
    return tournaments.get(str(topic_id))


# --------------------------------------------------------------------------
# data/overrides.json：站長自己寫的說明
# --------------------------------------------------------------------------


class OverridesError(ValueError):
    """data/overrides.json 壞掉，沒辦法解讀。"""


def load_overrides(path: Path) -> dict[str, Any]:
    """讀站長的覆寫內容（key 是 topic id）。

    這個檔案跟 tournaments.json 的關係是**刻意單向**的：只有人會寫它
    （看板上的「站長編輯」，或你直接在 GitHub 上改），爬蟲只讀不寫。
    所以你的修改不會被下一回合的抓取蓋掉 —— 那正是它存在的理由。

    壞掉時直接拋錯、讓排程變紅，不當成空檔默默帶過。當成空檔的下場很惡毒：
    看板上的站長說明會整批消失，而且因為 dashboard_sha 跟著變了，
    這個「消失」還會被當成一次正常的更新提交出去 —— 從 log 到看板都看不出異狀。
    """
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise OverridesError(
            f"{path} 不是有效的 JSON（第 {exc.lineno} 行第 {exc.colno} 欄：{exc.msg}）。"
            f"修好它，或直接刪掉這個檔案 —— 看板就會退回程式自動抓的原帖節錄。"
        ) from exc
    if not isinstance(data, dict):
        raise OverridesError(f"{path} 的最外層必須是一個物件（topic id → 覆寫內容）。")
    return data


def override_for(overrides: dict[str, Any], topic_id: Any) -> dict[str, str]:
    """取出某一筆的覆寫，順手把格式不對的部分濾掉。

    檔案是給人改的，所以要有這層：`{"2252361": "一段字"}` 這種寫法（整筆寫成字串
    而不是物件）不該讓整個看板炸掉，也不該讓那段字變成半個欄位。
    空的字串一律當作「沒有覆寫」—— 那正是看板上「清空再儲存」的語意。
    """
    entry = overrides.get(str(topic_id))
    if not isinstance(entry, dict):
        return {}
    out: dict[str, str] = {}
    for key in ("summary", "note"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            out[key] = value.strip()
    return out
