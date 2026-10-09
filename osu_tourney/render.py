"""產生 docs/index.html（線上賽事看板）與 drafts/{topic_id}.md（繁中貼文草稿）。

純函式：不碰網路、不讀時鐘。所有時間由呼叫端傳入，確保「相同輸入 → 位元組相同輸出」。

看板上的「站長說明」來自 data/overrides.json（見 store.load_overrides）。爬蟲只讀它，
寫它的是看板自己 —— 頁面拿著站長的 GitHub 權杖直接呼叫 API 提交，所以不需要後端。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import store

try:
    TAIPEI = ZoneInfo("Asia/Taipei")
except ZoneInfoNotFoundError:
    # Windows 的 Python 不帶系統時區資料庫，得靠 tzdata 套件補；沒裝就退回固定 +08:00。
    # 台灣自 1979 年起就沒有日光節約時間，永久 UTC+8，所以這個退路算出來與 ZoneInfo 相同，
    # 不是近似值。這樣一個「只是要顯示台北時間」的需求，就不會讓整支程式起不來。
    TAIPEI = timezone(timedelta(hours=8))

MODE_ORDER = ["std", "taiko", "catch", "mania"]

MODE_LABEL = {
    "std": "osu!standard",
    "taiko": "osu!taiko",
    "catch": "osu!catch",
    "mania": "osu!mania",
}

STATUS_LABEL = {
    "open": "報名開放中",
    "closed": "報名已截止",
    "unknown": "報名狀態未標明",
    # 標題沒說截止，但帖子內文寫的截止時間已經過去。見 effective_status()。
    "expired": "表定已截止",
}

# 收錄／排除的原因，翻成台灣讀者看得懂的話。
REASON_LABEL = {
    "no-region": "無區域限制（全球開放）",
    "explicit-allow": "明確包含台灣／亞洲區域",
    "code-allow": "明確包含台灣／中文圈",
    "named-region": "限定特定國家／區域",
    "code-deny": "限定特定國家（代碼）",
    "language-gate": "限定特定語言",
    "staff-topic": "徵求工作人員，非選手賽事",
    "unknown-code": "區域代碼無法判定（需人工確認）",
    "lan": "線下賽（LAN），需人工確認地點",
    "invitational": "邀請賽／選拔，非公開報名",
    "low-signal": "缺少名次與隊伍資訊，可能是情報帖（需人工確認）",
}


# --------------------------------------------------------------------------
# 時間
# --------------------------------------------------------------------------


def to_taipei(iso: Optional[str], fmt: str = "%Y-%m-%d %H:%M") -> Optional[str]:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(TAIPEI).strftime(fmt)


def _display_date(iso: Optional[str]) -> Optional[str]:
    """2026-10-08 → 10/08"""
    out = to_taipei(iso, "%m/%d")
    return out


def _deadline_passed(deadline_iso: Optional[str], now_iso: Optional[str]) -> bool:
    """截止時間是否已經過去。任一時間缺失或無法解析 → 一律當作還沒到。

    寧可漏報也不要誤報：把還能報名的比賽標成截止，代價是有人因此錯過一整場比賽；
    把已截止的標成開放，代價只是白點一下。所以不確定就往「還沒截止」倒。
    """
    if not deadline_iso or not now_iso:
        return False
    try:
        deadline = datetime.fromisoformat(deadline_iso)
        now = datetime.fromisoformat(now_iso)
    except ValueError:
        return False
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return deadline < now


def effective_status(record: dict[str, Any], now_iso: Optional[str]) -> str:
    """標題寫的報名狀態，用帖子內文的截止時間校正過。

    為什麼需要這一層：`status` 只讀標題（`[Open]` / `[REGS CLOSED]`），但主辦常常
    忘了改標題。真實案例 SMST 83（topic 2246109）：

        標題：  [STD]SMST 83 1-10K [Open]                        ← 沒改
        內文：  ...will end ... at 23:59 September 25th(...)     ← 早就截止了

    看板照標題顯示「報名開放中」，實際上報名已經關了兩週。而且它不會自己消失 ——
    論壇列表依**建立時間**排序，第一頁固定是最近建立的 50 帖，舊帖照樣留在上面。

    規則：標題說截止就信標題；標題說開放／未標明，但內文的截止時間已過，就改標成
    「表定已截止」。用「表定」而不是斷定的「已截止」，是因為 `deadline_iso` 終究是
    從一句話猜出來的 —— 講保守一點，卡片繼續留在看板上（只是變灰），
    人還點得進原帖自己確認。

    刻意**不**寫回 data/tournaments.json：那裡要忠實保留「標題當時怎麼寫」，
    這是判斷主辦有沒有更新標題的依據。
    """
    status = record.get("status") or "unknown"
    if status == "closed":
        return "closed"
    if _deadline_passed(record.get("deadline_iso"), now_iso):
        return "expired"
    return status


# --------------------------------------------------------------------------
# 看板
# --------------------------------------------------------------------------


def _region_note(record: dict[str, Any]) -> str:
    return REASON_LABEL.get(record.get("reason", ""), record.get("reason", ""))


def build_payload(
    tournaments: dict[str, Any],
    meta: dict[str, Any],
    now_iso: str,
    overrides: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """把內部記錄整理成看板要用的資料（只放 include 與 review）。

    顯示條件：仍在報名中，**或**最近還在論壇列表上（`DASHBOARD_WINDOW_DAYS` 內）。
    只有這樣，被新帖擠出第一頁、但還在報名的比賽才不會從看板消失。

    狀態一律經過 `effective_status` 校正 —— 標題沒改、內文早已截止的（例如 SMST 83）
    會變成 "expired"，因此不再算進「報名中」，也不再無條件常駐看板。

    `overrides` 是站長自己寫的說明（見 store.load_overrides）。它**不覆蓋** excerpt，
    而是兩個獨立欄位並存：看板要據此決定那段字是「原帖節錄」還是「站長說明」——
    後者不能標成節錄，那是把別人的話記在我們頭上。
    """
    overrides = overrides or {}
    rows: list[dict[str, Any]] = []

    for record in tournaments.values():
        if record.get("decision") not in ("include", "review"):
            continue
        status = effective_status(record, now_iso)
        if status != "open" and not store.is_fresh(record, now_iso):
            continue

        tid = record["topic_id"]
        own = store.override_for(overrides, tid)
        rows.append(
            {
                "topic_id": tid,
                "name": record.get("name") or record.get("title", ""),
                "mode": record.get("mode", "std"),
                "mode_label": record.get("mode_label") or MODE_LABEL.get(record.get("mode", "std"), "osu!standard"),
                "rank": record.get("rank_compact") or "",
                "teams": record.get("teams") or [],
                "region": _region_note(record),
                "status": status,
                "deadline": to_taipei(record.get("deadline_iso"), "%m/%d %H:%M") or "",
                "deadline_raw": (record.get("deadline_raw") or "")[:200],
                # 給卡片上的倒數用。deadline 是已經算好的台北時間字串（人看的），
                # deadline_iso 是原始 UTC 時間（機器算剩餘時間用的）—— 兩個都需要。
                "deadline_iso": record.get("deadline_iso") or "",
                # 詳細面板才有空間放這些，卡片上塞不下。
                "title": record.get("title", ""),
                "rank_full": record.get("rank_full") or "",
                "author": record.get("author") or "",
                "excerpt": record.get("excerpt") or "",
                # 站長寫的。summary 有值時就取代 excerpt 顯示；excerpt 仍然照傳，
                # 因為編輯面板要用它當預設內容、也要拿它比對「有沒有真的改過」。
                "summary": own.get("summary", ""),
                "note": own.get("note", ""),
                "decision": record.get("decision"),
                "reason": record.get("reason", ""),
                "created_at": record.get("created_at") or "",
                "first_seen_utc": record.get("first_seen_utc") or "",
                "first_seen_display": _display_date(record.get("first_seen_utc")) or "",
                "url": f"https://osu.ppy.sh/community/forums/topics/{tid}",
                "discord": record.get("discord") or "",
                "signup_form": record.get("signup_form") or "",
            }
        )

    # 收錄的排前面，同組內新的排前面。
    # 三次穩定排序，由次要到主要：決策 → 首次發現時間 → topic id。
    # 最後那個 tiebreaker 是必要的，不是裝飾：同一回合發現的賽事 first_seen_utc
    # 完全相同，少了它，平手的順序就取決於 dict 的迭代順序 —— 而「這回合新建的 dict」
    # 與「從 sort_keys 過的 JSON 載回來的 dict」順序不一樣（後者是 id 遞增），
    # 看板每換一次環境就整片跳位，dashboard_sha 也會跟著抖動而產生假提交。
    rows.sort(key=lambda r: r["topic_id"], reverse=True)
    rows.sort(key=lambda r: r["first_seen_utc"], reverse=True)
    rows.sort(key=lambda r: r["decision"] != "include")

    counts = {
        "include": sum(1 for r in rows if r["decision"] == "include"),
        "review": sum(1 for r in rows if r["decision"] == "review"),
        "open": sum(1 for r in rows if r["decision"] == "include" and r["status"] == "open"),
    }

    return {"meta": meta, "counts": counts, "tournaments": rows}


def render_dashboard(payload: dict[str, Any]) -> str:
    data_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    # 內嵌 JSON 時必須把 </ 拆開，否則會提前關掉 <script> 標籤。
    data_json = data_json.replace("</", "<\\/")

    return _TEMPLATE.replace("/*__DATA__*/null", data_json)


# --------------------------------------------------------------------------
# 草稿
# --------------------------------------------------------------------------


def _deadline_line(record: dict[str, Any]) -> str:
    iso = record.get("deadline_iso")
    local = to_taipei(iso, "%m/%d %H:%M") if iso else None
    if local:
        return f"截止 {local}（台北時間）"
    raw = record.get("deadline_raw")
    if raw:
        return f"截止時間請見內文：{raw.strip()}"
    return "報名時間以官方公告為準"


def render_draft(record: dict[str, Any], now_iso: Optional[str] = None) -> str:
    """繁中 Facebook 貼文草稿。標題格式：模式＋賽事名＋報名時間。

    `now_iso` 用來校正報名狀態（見 `effective_status`）。草稿是要貼出去的，
    標題寫「報名開放中」而實際已截止，比看板上標錯更嚴重 —— 那是直接發錯文。
    沒傳 `now_iso` 時退回標題狀態（`_deadline_passed` 會回 False），不會亂猜。
    """
    mode = record.get("mode_label") or MODE_LABEL.get(record.get("mode", "std"), "osu!standard")
    name = record.get("name") or record.get("title", "")
    rank = record.get("rank_compact") or ""
    status = STATUS_LABEL.get(effective_status(record, now_iso), "報名狀態未標明")
    tid = record["topic_id"]
    url = f"https://osu.ppy.sh/community/forums/topics/{tid}"

    title = f"【{mode}】{name}"
    if rank:
        title += f"（{rank}）"
    title += f"｜{status}"

    lines = [
        f"# {title}",
        "",
        "```",
        title,
        "```",
        "",
        f"🎮 模式｜{mode}",
        f"🏆 賽事｜{name}",
    ]
    if rank:
        lines.append(f"📊 名次｜{record.get('rank_full') or rank}")
    teams = record.get("teams") or []
    if teams:
        lines.append(f"👥 形式｜{' / '.join(teams)}")
    lines.append(f"🌏 區域｜{_region_note(record)}")
    lines.append(f"📝 報名｜{status}・{_deadline_line(record)}")
    lines.append(f"🔗 資訊｜{url}")
    if record.get("discord"):
        lines.append(f"💬 Discord｜{record['discord']}")
    if record.get("signup_form"):
        lines.append(f"📋 報名表單｜{record['signup_form']}")
    lines += ["", "#osu #osu標準 #台灣osu #osu比賽", ""]

    if record.get("decision") == "review":
        lines += [
            "> ⚠️ 這筆是**待人工確認**："
            + REASON_LABEL.get(record.get("reason", ""), record.get("reason", "")),
            "> 確認過再發文，或直接刪掉這個檔案。",
            "",
        ]

    return "\n".join(lines)


def render_index_markdown(payload: dict[str, Any]) -> str:
    """drafts/README.md —— 一眼看出有哪些待發草稿。"""
    rows = payload["tournaments"]
    lines = ["# 待發送的貼文草稿", "", f"共 {len(rows)} 筆（收錄 {payload['counts']['include']}／待確認 {payload['counts']['review']}）", ""]
    for r in rows:
        flag = "⚠️ " if r["decision"] == "review" else ""
        lines.append(f"- {flag}[{r['name']}]({r['topic_id']}.md) — {r['mode_label']}｜{STATUS_LABEL.get(r['status'], '')}")
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# HTML 樣板
# --------------------------------------------------------------------------

_TEMPLATE = r"""<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>台灣 osu! 賽事看板</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Saira:wght@400;500;600;700&family=Noto+Sans+TC:wght@400;500;700&display=swap">
<style>
:root{
  --bg:#FBF7F9; --surface:#FFFFFF; --text:#1E1720; --muted:#6E6170;
  --border:#EADFE5; --accent:#D62E77; --accent-soft:#FCE7F0; --on-accent:#FFFFFF;
  --open:#1F7A4D; --open-bg:#E3F4EA;
  --closed:#8A7F8D; --closed-bg:#EFEBF1;
  --unknown:#8A6212; --unknown-bg:#FBF0D9;
  --review:#8A5A12; --review-bg:#FDF0DC;
  --shadow:0 1px 2px rgba(30,23,32,.05), 0 8px 24px -18px rgba(30,23,32,.35);
  color-scheme: light;
}
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]){
    --bg:#15111A; --surface:#1E1824; --text:#F2ECF4; --muted:#A498A9;
    --border:#2E2735; --accent:#FF66AA; --accent-soft:#3A1F2E; --on-accent:#2A0E1C;
    --open:#5FD79B; --open-bg:#12301F;
    --closed:#8E8393; --closed-bg:#241E2A;
    --unknown:#E0B45C; --unknown-bg:#33280F;
    --review:#E0B45C; --review-bg:#33280F;
    --shadow:0 1px 2px rgba(0,0,0,.4), 0 8px 24px -18px rgba(0,0,0,.9);
    color-scheme: dark;
  }
}
:root[data-theme="dark"]{
  --bg:#15111A; --surface:#1E1824; --text:#F2ECF4; --muted:#A498A9;
  --border:#2E2735; --accent:#FF66AA; --accent-soft:#3A1F2E; --on-accent:#2A0E1C;
  --open:#5FD79B; --open-bg:#12301F;
  --closed:#8E8393; --closed-bg:#241E2A;
  --unknown:#E0B45C; --unknown-bg:#33280F;
  --review:#E0B45C; --review-bg:#33280F;
  --shadow:0 1px 2px rgba(0,0,0,.4), 0 8px 24px -18px rgba(0,0,0,.9);
  color-scheme: dark;
}
*{box-sizing:border-box}
body{
  margin:0; background:var(--bg); color:var(--text);
  font-family:"Noto Sans TC","Saira",system-ui,-apple-system,"Segoe UI",sans-serif;
  padding:0 16px env(safe-area-inset-bottom,0px);
  -webkit-font-smoothing:antialiased;
}
.wrap{max-width:1120px; margin:0 auto; padding-block:32px 64px}
header.top{display:flex; flex-wrap:wrap; gap:20px; align-items:flex-end; justify-content:space-between; padding-bottom:20px; border-bottom:1px solid var(--border)}
h1{font-family:"Saira","Noto Sans TC",sans-serif; font-size:clamp(22px,4vw,32px); font-weight:700; margin:0; letter-spacing:-.01em}
h1 .accent{color:var(--accent)}
.sub{color:var(--muted); font-size:13px; margin-top:6px}
.stats{display:flex; gap:24px}
.stat{text-align:right}
.stat b{font-family:"Saira",sans-serif; font-variant-numeric:tabular-nums; font-size:26px; font-weight:700; display:block; line-height:1.1}
.stat span{font-size:12px; color:var(--muted)}
.controls{display:flex; flex-wrap:wrap; gap:12px; align-items:center; margin:24px 0 20px}
.group{display:flex; gap:0; border:1px solid var(--border); border-radius:10px; overflow:hidden; background:var(--surface)}
.group button{
  font:inherit; font-size:13px; padding:8px 13px; border:0; background:transparent; color:var(--muted);
  cursor:pointer; border-right:1px solid var(--border); white-space:nowrap;
}
.group button:last-child{border-right:0}
.group button[aria-pressed="true"]{background:var(--accent); color:var(--on-accent); font-weight:500}
.group button:focus-visible{outline:2px solid var(--accent); outline-offset:-2px}
.toggle{display:flex; align-items:center; gap:8px; font-size:13px; color:var(--muted); cursor:pointer}
.toggle input{accent-color:var(--accent); width:16px; height:16px}
.grid{display:grid; grid-template-columns:repeat(auto-fill,minmax(330px,1fr)); gap:16px}
@media (max-width:420px){ .grid{grid-template-columns:1fr} .wrap{padding-inline:0} }
.card{
  background:var(--surface); border:1px solid var(--border); border-radius:14px;
  padding:18px; display:flex; flex-direction:column; gap:14px; box-shadow:var(--shadow);
  cursor:pointer;
}
.card:focus-visible{outline:2px solid var(--accent); outline-offset:2px}
.card.review{border-style:dashed}
.card-top{display:flex; align-items:center; gap:8px; flex-wrap:wrap}
.chip{
  font-family:"Saira",sans-serif; font-size:11px; font-weight:600; letter-spacing:.04em;
  text-transform:uppercase; padding:4px 9px; border-radius:999px;
  background:var(--accent-soft); color:var(--accent); border:1px solid transparent;
}
.pill{font-size:11px; padding:4px 9px; border-radius:999px; font-weight:500; margin-left:auto}
.pill.open{background:var(--open-bg); color:var(--open)}
.pill.closed{background:var(--closed-bg); color:var(--closed)}
.pill.unknown{background:var(--unknown-bg); color:var(--unknown)}
/* 「表定已截止」跟標題明寫的「已截止」用同一組顏色：對讀者來說兩者都是「不能報了」。
   差別寫在 pill 的文字與卡片上的提示，不靠顏色區分。 */
.pill.expired{background:var(--closed-bg); color:var(--closed)}
.card h2{font-size:19px; font-weight:700; margin:0; line-height:1.35; text-wrap:balance}
.meta{display:flex; flex-direction:column; gap:7px; font-size:13px; color:var(--muted); margin:0}
.meta div{display:flex; gap:10px}
.meta dt{min-width:56px; color:var(--muted); opacity:.8}
.meta dd{margin:0; color:var(--text)}
.meta dd.num{font-family:"Saira",sans-serif; font-variant-numeric:tabular-nums}
.review-note,.stale-note{font-size:12px; border-radius:8px; padding:8px 10px; line-height:1.5}
.review-note{color:var(--review); background:var(--review-bg)}
.stale-note{color:var(--closed); background:var(--closed-bg)}
.card-foot{display:flex; gap:14px; align-items:center; margin-top:auto; padding-top:4px; font-size:12px; color:var(--muted)}
.card-foot a{color:var(--accent); text-decoration:none; font-weight:600}
.card-foot a:hover{text-decoration:underline}
.card-foot a:focus-visible{outline:2px solid var(--accent); outline-offset:2px; border-radius:4px}
.more{margin-left:auto; white-space:nowrap; opacity:.75}
/* 剩餘時間。分開一個 span 是為了讓 JS 每分鐘只改這幾個字，
   不必整頁重繪（重繪會把正在讀的卡片捲動位置跳掉）。 */
.countdown{font-variant-numeric:tabular-nums; opacity:.75; white-space:nowrap}
.countdown::before{content:"· "}
.countdown.past{color:var(--closed); opacity:1}
.countdown:empty::before{content:""}
.empty{text-align:center; color:var(--muted); padding:64px 16px; border:1px dashed var(--border); border-radius:14px}
footer{margin-top:40px; padding-top:20px; border-top:1px solid var(--border); color:var(--muted); font-size:12px; line-height:1.7}
@media (prefers-reduced-motion:no-preference){ .card{transition:box-shadow .15s ease} .card:hover{box-shadow:0 2px 4px rgba(30,23,32,.06),0 14px 32px -20px rgba(30,23,32,.5)} }

/* 點卡片彈出的詳細說明。用原生 <dialog>：Esc 關閉、focus trap、backdrop 都是免費的。 */
.detail{
  border:1px solid var(--border); border-radius:16px; padding:0; background:var(--surface); color:var(--text);
  width:min(560px, calc(100% - 32px)); max-height:min(85vh, 720px); overflow:auto;
  box-shadow:0 24px 64px -28px rgba(30,23,32,.6);
}
.detail::backdrop{background:rgba(20,15,22,.55)}
.detail-inner{padding:20px}
.detail-top{display:flex; align-items:center; gap:8px}
.detail .pill{margin-left:0}
.detail-close{
  margin-left:auto; font:inherit; font-size:14px; line-height:1; padding:6px 9px;
  border:1px solid var(--border); border-radius:8px; background:transparent; color:var(--muted); cursor:pointer;
}
.detail-close:hover{color:var(--text); border-color:var(--muted)}
.detail-close:focus-visible{outline:2px solid var(--accent); outline-offset:2px}
.detail h2{font-size:20px; font-weight:700; margin:12px 0 0; line-height:1.35; text-wrap:balance}
.detail-body{display:flex; flex-direction:column; gap:14px; margin-top:16px}
.quote{
  border-left:3px solid var(--border); padding:2px 0 2px 12px;
  font-size:13px; line-height:1.7; color:var(--text); white-space:pre-wrap; overflow-wrap:anywhere;
}
.quote .src{display:block; margin-top:6px; font-size:11px; color:var(--muted)}
.detail-foot{display:flex; flex-wrap:wrap; gap:16px; margin-top:20px; padding-top:16px; border-top:1px solid var(--border); font-size:13px}
.detail-foot a{color:var(--accent); text-decoration:none; font-weight:600}
.detail-foot a:hover{text-decoration:underline}
.detail-foot a:focus-visible{outline:2px solid var(--accent); outline-offset:2px; border-radius:4px}

/* 站長自己寫的說明。左側直條改用強調色，跟「引自原帖」的中性直條分開 ——
   但顏色只是輔助，來源那行字本身也照實寫「站長說明」，不靠顏色表達身分。 */
.quote.own{border-left-color:var(--accent)}
.owner-note{
  background:var(--accent-soft); border-radius:10px; padding:10px 12px;
  font-size:13px; line-height:1.7; white-space:pre-wrap; overflow-wrap:anywhere;
}
.owner-note .owner-src{display:block; font-size:11px; font-weight:700; letter-spacing:.04em; color:var(--accent); margin-bottom:4px}

/* 編輯模式的那一行。畫面上平常沒有這塊 —— 訪客連痕跡都看不到。
   display 寫在這裡，所以 [hidden] 得自己再講一次：hidden 只是 UA 樣式的
   display:none，任何一條設了 display 的作者規則都會蓋掉它。 */
.edit-hint{
  flex-basis:100%; display:flex; flex-wrap:wrap; gap:12px; align-items:center;
  font-size:12px; color:var(--muted);
}
.edit-hint[hidden]{display:none}
.linkbtn{
  font:inherit; font-size:12px; padding:0; border:0; background:none;
  color:var(--accent); cursor:pointer; text-decoration:underline; text-underline-offset:2px;
}
.linkbtn:focus-visible{outline:2px solid var(--accent); outline-offset:2px; border-radius:3px}

/* 編輯器 */
.editor{display:flex; flex-direction:column; gap:8px; border:1px solid var(--border); border-radius:12px; padding:14px}
.ed-label{font-size:12px; font-weight:700}
.ed-input{
  font:inherit; font-size:13px; line-height:1.7; width:100%; resize:vertical;
  padding:10px 12px; border:1px solid var(--border); border-radius:10px;
  background:var(--bg); color:var(--text);
}
.ed-input:focus-visible{outline:2px solid var(--accent); outline-offset:1px}
.ed-hint{font-size:12px; color:var(--muted); line-height:1.6}
.ed-bar{display:flex; flex-wrap:wrap; gap:10px; align-items:center; margin-top:4px}
.ed-save,.ed-revert{font:inherit; font-size:13px; padding:8px 14px; border-radius:10px; cursor:pointer; white-space:nowrap}
.ed-save{border:1px solid var(--accent); background:var(--accent); color:var(--on-accent); font-weight:600}
.ed-save:disabled{opacity:.5; cursor:default}
.ed-revert{border:1px solid var(--border); background:transparent; color:var(--muted)}
.ed-revert:hover{color:var(--text); border-color:var(--muted)}
.ed-save:focus-visible,.ed-revert:focus-visible{outline:2px solid var(--accent); outline-offset:2px}
.edit-status{font-size:12px; color:var(--muted); line-height:1.5}
.edit-status.bad{color:var(--accent); font-weight:500}
.ed-warn{font-size:12px; line-height:1.6; border-radius:8px; padding:8px 10px; color:var(--review); background:var(--review-bg)}

/* 權杖面板（沿用 .detail 的外殼，只是內容不同） */
.detail-top h2{margin:0; font-size:18px}
.tk-help{font-size:12px; line-height:1.75; color:var(--muted); margin:14px 0 0}
.tk-help b{color:var(--text)}
.tk-help code{
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:11px;
  background:var(--bg); border:1px solid var(--border); border-radius:5px; padding:1px 5px; overflow-wrap:anywhere;
}
.tk-field{display:flex; flex-direction:column; gap:6px; margin-top:16px; font-size:12px; font-weight:700}
.tk-field input{
  font:inherit; font-size:13px; font-weight:400; width:100%; padding:10px 12px;
  border:1px solid var(--border); border-radius:10px; background:var(--bg); color:var(--text);
}
.tk-field input:focus-visible{outline:2px solid var(--accent); outline-offset:1px}
.tk-bar{display:flex; flex-wrap:wrap; gap:10px; margin-top:18px; padding-top:16px; border-top:1px solid var(--border)}
</style>
</head>
<body>
<div class="wrap">
  <header class="top">
    <div>
      <h1>台灣 osu! <span class="accent">賽事看板</span></h1>
      <div class="sub" id="sub">自動彙整 osu! 論壇 Tournaments 版中，台灣玩家可報名的錦標賽</div>
    </div>
    <div class="stats">
      <div class="stat"><b id="s-open">–</b><span>報名中</span></div>
      <div class="stat"><b id="s-total">–</b><span>收錄賽事</span></div>
      <div class="stat"><b id="s-review">–</b><span>待確認</span></div>
    </div>
  </header>

  <div class="controls">
    <div class="group" id="f-mode" role="group" aria-label="依模式篩選"></div>
    <div class="group" id="f-status" role="group" aria-label="依報名狀態篩選"></div>
    <label class="toggle"><input type="checkbox" id="f-review" autocomplete="off" checked> 顯示待確認</label>
    <!-- 站長的入口。畫面上沒有任何按鈕 —— 訪客看不到編輯功能的痕跡。
         要進來有兩條路，都不留東西在頁面上：
           1. 在頁面上直接打 edit（焦點不在輸入框時）
           2. 網址後面加 #edit
         進來之後才會長出這一行，並附上出口。 -->
    <div class="edit-hint" id="edit-hint" hidden>
      <span id="eh-text"></span>
      <button type="button" class="linkbtn" id="eh-token">更換權杖</button>
      <button type="button" class="linkbtn" id="eh-exit">結束編輯</button>
    </div>
  </div>

  <div class="grid" id="grid"></div>
  <div class="empty" id="empty" hidden>沒有符合條件的賽事。</div>

  <dialog class="detail" id="detail" aria-labelledby="d-name">
    <div class="detail-inner">
      <div class="detail-top">
        <span class="chip" id="d-chip"></span>
        <span class="pill" id="d-pill"></span>
        <button type="button" class="detail-close" id="d-close">✕ 關閉</button>
      </div>
      <h2 id="d-name"></h2>
      <div class="detail-body" id="d-body"></div>
      <div class="detail-foot" id="d-foot"></div>
    </div>
  </dialog>

  <dialog class="detail" id="token" aria-labelledby="t-title">
    <div class="detail-inner">
      <div class="detail-top">
        <h2 id="t-title">編輯用的 GitHub 權杖</h2>
        <button type="button" class="detail-close" id="t-close">✕ 關閉</button>
      </div>
      <p class="tk-help">
        看板上<b>沒有任何編輯按鈕</b>，訪客看到的就只是一條賽事看板。要打開編輯模式，
        在頁面上直接打 <code>edit</code>（焦點不在輸入框時），或在網址後面加
        <code>#edit</code>。兩個都是切換 —— 再打一次就退出。
      </p>
      <p class="tk-help">
        看板是 GitHub Pages 的靜態頁，沒有後端。「在看板上直接改」＝ 這個頁面拿著一把你的
        GitHub 權杖，直接呼叫 GitHub API 把 <code>data/overrides.json</code> 提交回 repo。
        權杖<b>只存在這個瀏覽器</b>（localStorage），不會寫進 repo；其他訪客的頁面沒有它，
        他們看到的永遠是已提交的內容。
      </p>
      <p class="tk-help">
        到 GitHub → Settings → Developer settings → <b>Fine-grained tokens</b> 建一把：
        Repository access 只勾 <code id="t-repo"></code>，
        Permissions → <b>Contents: Read and write</b> 就好。想讓存檔後看板馬上更新，
        再加 <b>Actions: Read and write</b>（非必要）。其他權限都不用給。
      </p>
      <p class="tk-help" id="t-warn"></p>
      <label class="tk-field">權杖
        <input type="password" id="t-input" autocomplete="off" spellcheck="false" placeholder="github_pat_…">
      </label>
      <div class="tk-bar">
        <button type="button" class="ed-save" id="t-save">儲存並開始編輯</button>
        <button type="button" class="ed-revert" id="t-clear">清除權杖</button>
      </div>
    </div>
  </dialog>

  <footer>
    <div id="foot-meta"></div>
    <div>資料來源：<a href="https://osu.ppy.sh/community/forums/55?sort=created" style="color:var(--accent)">osu! 論壇 Tournaments 版</a>。
    區域與欄位以關鍵字規則自動判定，可能有誤 —— 標示「待確認」者請自行核對原帖。<br>
    時間一律換算成 <b>UTC+8</b>（台北）；「剩餘…」由你的瀏覽器即時計算，會隨時間自己更新。<br>
    「<b>表定已截止</b>」表示標題仍寫著報名開放，但帖內文寫的截止時間已經過去（主辦忘了改標題）。<br>
    點卡片可以看完整的說明、主辦的原話與所有欄位。</div>
  </footer>
</div>

<script type="application/json" id="data">/*__DATA__*/null</script>
<script>
(function(){
  var payload = JSON.parse(document.getElementById('data').textContent);
  var rows = payload.tournaments || [];
  // 「顯示待確認」這一格刻意**不放進 state**，一律直接讀 DOM 的 checkbox。
  // 原因：瀏覽器重新載入時會還原表單控制項的狀態，而且是在這支 inline script
  // 跑完之後才還原。若用 JS 變數另外記一份，兩者就會分岔 —— 勾勾看起來是取消的，
  // 卡片卻還在（變數仍是 true）；再點一下反而變成「已勾選」，畫面毫無反應，
  // 整個開關就像壞掉。讓真相只剩一個地方，就不會分岔。
  var state = { mode: 'all', status: 'all' };
  var reviewToggle = document.getElementById('f-review');

  // ------------------------------------------------------------------
  // 站長在網站上直接編輯
  // ------------------------------------------------------------------
  //
  // 看板是靜態頁，沒有任何後端。要能「在網頁上改字並存下來」，唯一的辦法是讓這個
  // 頁面拿著一把有 Contents 寫入權限的 GitHub 權杖，直接呼叫 GitHub API 產生 commit。
  //
  // 幾個刻意的決定：
  //
  // * 權杖只放在**你自己的瀏覽器**（localStorage），不進 repo、不進 HTML，
  //   其他訪客拿不到。所以「誰能改」＝「誰有那把權杖」，不需要另外做帳號系統。
  // * 權杖不放在記憶體變數裡，而是每次用之前重讀 —— 這樣開兩個分頁、在其中一個
  //   換權杖，另一個不會繼續用舊的。
  // * 寫的是 data/overrides.json，不是 data/tournaments.json。爬蟲只讀不寫這個檔案，
  //   所以你的修改不會被下一回合的抓取蓋掉。
  // * 存檔只改了 overrides.json，看板 HTML 要等爬蟲重繪才會帶上新說明 ——
  //   所以存完會順手觸發一次排程（需要權杖額外有 Actions 權限，沒有也照常運作）。
  var TOKEN_KEY = 'osu-tw-board-token';
  var OVERRIDES_PATH = 'data/overrides.json';
  var REPO = (payload.meta && payload.meta.repo) || '';
  var BRANCH = (payload.meta && payload.meta.branch) || 'main';
  var editOn = false;
  var current = null;      // 詳細面板正在顯示的那一筆
  var lastDispatch = 0;

  function readToken(){ try{ return localStorage.getItem(TOKEN_KEY) || ''; }catch(e){ return ''; } }
  function writeToken(t){
    try{ t ? localStorage.setItem(TOKEN_KEY, t) : localStorage.removeItem(TOKEN_KEY); }catch(e){}
  }

  // GitHub 的 contents API 用 base64 傳內容，而 btoa 只吃 Latin-1 —— 直接把中文
  // 丟進去會拋 InvalidCharacterError。所以自己編成 UTF-8 位元組再轉字串；
  // 一次 apply 整個大陣列會爆 call stack，切 32K 一段。
  function b64Encode(text){
    var bytes = new TextEncoder().encode(text), bin = '', CH = 0x8000;
    for(var i=0;i<bytes.length;i+=CH){
      bin += String.fromCharCode.apply(null, bytes.subarray(i, i+CH));
    }
    return btoa(bin);
  }

  function b64Decode(text){
    var bin = atob(String(text).replace(/\s/g,'')), bytes = new Uint8Array(bin.length);
    for(var i=0;i<bin.length;i++) bytes[i] = bin.charCodeAt(i);
    return new TextDecoder().decode(bytes);
  }

  function api(path, opts){
    var opt = opts || {};
    opt.headers = Object.assign({
      'Authorization': 'Bearer ' + readToken(),
      'Accept': 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28'
    }, opt.headers || {});
    return fetch('https://api.github.com/repos/' + REPO + path, opt).then(function(res){
      // 先把 body 讀成文字再試著 parse：GitHub 出錯時回的不一定是 JSON
      // （proxy 擋掉會回 HTML），直接 res.json() 會拋在一個與原因無關的地方。
      return res.text().then(function(body){
        var json = null;
        try{ json = JSON.parse(body); }catch(e){}
        return { status: res.status, ok: res.ok, json: json };
      });
    });
  }

  function explainStatus(s){
    if(s === 401) return '權杖無效或已經過期，請重新輸入（🔑）。';
    if(s === 403) return '權杖權限不足：需要這個 repo 的 Contents 讀寫權限。';
    if(s === 404) return '找不到這個 repo，或這把權杖沒有它的權限。';
    return 'GitHub 回了 ' + s + '，請稍後再試。';
  }

  function fetchOverrides(){
    return api('/contents/' + OVERRIDES_PATH + '?ref=' + encodeURIComponent(BRANCH), {method:'GET'})
      .then(function(res){
        if(res.status === 404) return { sha:null, data:{} };  // 還沒建檔，正常
        if(!res.ok) throw new Error(explainStatus(res.status));
        var text = b64Decode((res.json && res.json.content) || '');
        var data = {};
        try{
          var parsed = JSON.parse(text);
          if(parsed && typeof parsed === 'object') data = parsed;
        }catch(e){ data = {}; }
        return { sha:(res.json && res.json.sha) || null, data:data };
      });
  }

  function putOverrides(data, sha, message){
    // 依 topic id 數字排序再寫回去。不清的話，每次存檔新 key 都往後追加，
    // 幾次之後這個檔案就沒人讀得懂了 —— 而它本來是設計成可以手改的。
    var sorted = {};
    Object.keys(data).sort(function(a,b){ return Number(a) - Number(b); })
      .forEach(function(k){ sorted[k] = data[k]; });

    var body = {
      message: message,
      content: b64Encode(JSON.stringify(sorted, null, 2) + '\n'),
      branch: BRANCH
    };
    if(sha) body.sha = sha;
    return api('/contents/' + OVERRIDES_PATH, {
      method:'PUT',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify(body)
    });
  }

  // 存檔只改了 overrides.json；看板 HTML 是爬蟲產的，要等它下一回合重繪才會帶上
  // 新說明。手動觸發一次排程能把 30 分鐘縮到約 1 分鐘，但那需要權杖額外有 Actions
  // 的寫入權限 —— 所以是盡力而為：失敗就照實說「最多 30 分鐘」，不要騙人。
  function dispatchRun(){
    if(Date.now() - lastDispatch < 60000) return Promise.resolve(false);
    return api('/actions/workflows/scrape.yml/dispatches', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ref: BRANCH})
    }).then(function(res){
      if(res.status === 204){ lastDispatch = Date.now(); return true; }
      return false;
    }).catch(function(){ return false; });
  }

  // 讀 sha → 改 → 帶 sha 寫回。中間只要有人（或你另一個分頁）先寫了一次，
  // 寫回就會被 GitHub 拒絕。所以整段重試一次，而不是直接把錯誤丟給使用者。
  //
  // 回傳 {wrote}：內容跟原本一字不差時**不送出**。GitHub 的 contents API 不做去重，
  // 內容一樣照樣產生一個 commit —— 打開來看一下、順手按儲存，就會在歷史裡留一筆
  // 什麼都沒改的提交。
  function saveOverride(r, summary, note){
    function attempt(left){
      return fetchOverrides().then(function(cur){
        var data = cur.data, key = String(r.topic_id);
        var before = JSON.stringify(data[key] || {});
        // 逐欄合併，不用整個物件覆蓋 —— 這樣未來多出的欄位（或你手寫的註解欄位）
        // 不會被這次存檔默默吃掉。
        var entry = Object.assign({}, data[key] || {});
        if(summary) entry.summary = summary; else delete entry.summary;
        if(note) entry.note = note; else delete entry.note;
        if(JSON.stringify(entry) === before) return { wrote:false };
        if(Object.keys(entry).length) data[key] = entry; else delete data[key];

        return putOverrides(data, cur.sha, '看板編輯：' + r.name + '（topic ' + key + '）')
          .then(function(res){
            if(res.status === 409 || res.status === 422){
              if(left > 0) return attempt(left - 1);
              throw new Error('有人同時改了這個檔案，請再按一次儲存。');
            }
            if(!res.ok) throw new Error(explainStatus(res.status));
            return { wrote:true };
          });
      });
    }
    return attempt(1);
  }

  function setEditStatus(msg, bad){
    var n = document.getElementById('e-status');
    if(n){ n.textContent = msg; n.className = 'edit-status' + (bad ? ' bad' : ''); }
  }

  var STATUS = {open:'報名開放中', closed:'報名已截止', unknown:'報名狀態未標明', expired:'表定已截止'};
  var MODES = [['all','全部'],['std','Standard'],['taiko','Taiko'],['catch','Catch'],['mania','Mania']];
  var STATUSES = [['all','全部狀態'],['open','報名中'],['closed','已截止'],['unknown','未標明']];

  function el(tag, cls, text){ var e=document.createElement(tag); if(cls) e.className=cls; if(text!=null) e.textContent=text; return e; }

  function metaRow(meta, label, value, node, numeric){
    if(!value && !node) return;
    var d = el('div');
    d.appendChild(el('dt', null, label));
    var dd = el('dd', numeric?'num':null);
    if(node) dd.appendChild(node); else dd.textContent = value;
    d.appendChild(dd); meta.appendChild(d);
  }

  // 倒數是**唯一由瀏覽器現算**的東西。看板是靜態頁，可能好幾小時前就產生了；
  // 若用產生時間去算「剩餘幾小時」，一打開就已經是錯的（而且是愈用愈錯）。
  // 所以：絕對時間由 Python 算好（台北 = UTC+8），JS 只負責「還有多久」。
  function countdownText(iso, now){
    var t = Date.parse(iso);
    if(isNaN(t)) return '';
    var ms = t - now;
    if(ms <= 0) return '已截止';
    var mins = Math.floor(ms / 60000);
    if(mins < 1) return '剩餘不到 1 分';
    var days = Math.floor(mins / 1440);
    if(days >= 1) return '剩餘 ' + days + ' 天';
    var hrs = Math.floor(mins / 60);
    if(hrs >= 1) return '剩餘 ' + hrs + ' 小時 ' + (mins % 60) + ' 分';
    return '剩餘 ' + mins + ' 分';
  }

  function tickCountdowns(){
    var now = Date.now();
    var nodes = document.querySelectorAll('[data-deadline]');
    for(var i=0;i<nodes.length;i++){
      var txt = countdownText(nodes[i].getAttribute('data-deadline'), now);
      if(nodes[i].textContent !== txt) nodes[i].textContent = txt;
      var past = (txt === '已截止');
      if(nodes[i].classList.contains('past') !== past) nodes[i].classList.toggle('past', past);
    }
  }

  // 截止時間那一格的內容：絕對時間（台北）＋ 倒數。
  // 沒有可用的 ISO 時間時退回主辦寫的原句 —— 寧可顯示模糊的原話，
  // 也不要捏造一個看起來很精確的時刻。
  function deadlineNode(r){
    if(!r.deadline && !r.deadline_raw) return null;
    var frag = document.createDocumentFragment();
    if(r.deadline){
      frag.appendChild(document.createTextNode(r.deadline + '（UTC+8）'));
      if(r.deadline_iso){
        var s = el('span','countdown','');
        s.setAttribute('data-deadline', r.deadline_iso);
        frag.appendChild(s);
      }
    } else {
      frag.appendChild(document.createTextNode(r.deadline_raw));
    }
    return frag;
  }

  function buildGroup(host, options, key){
    options.forEach(function(opt){
      var b = el('button', null, opt[1]);
      b.type='button';
      b.setAttribute('aria-pressed', String(state[key]===opt[0]));
      b.addEventListener('click', function(){
        state[key]=opt[0];
        Array.prototype.forEach.call(host.children, function(c){ c.setAttribute('aria-pressed', String(c===b)); });
        render();
      });
      host.appendChild(b);
    });
  }

  // 「表定已截止」在篩選上跟標題明寫的「已截止」算同一格 —— 對使用者來說兩者都只是
  // 「不能報了」，多開一格只會讓篩選列更難懂。
  function statusBucket(s){ return s==='expired' ? 'closed' : s; }

  function visible(){
    return rows.filter(function(r){
      if(r.decision==='review' && !reviewToggle.checked) return false;
      if(state.mode!=='all' && r.mode!==state.mode) return false;
      if(state.status!=='all' && statusBucket(r.status)!==state.status) return false;
      return true;
    });
  }

  // 卡片裡的連結要自己吃掉點擊事件，否則點 Discord 會同時彈出詳細面板。
  function link(href, text){
    var a = el('a', null, text);
    a.href = href; a.target='_blank'; a.rel='noopener';
    a.addEventListener('click', function(e){ e.stopPropagation(); });
    return a;
  }

  function card(r){
    var c = el('article','card'+(r.decision==='review'?' review':''));
    // 整張卡片就是「看詳細」的按鈕。role/tabindex 是為了鍵盤與讀屏，
    // 裡面還有真的連結，所以不能用 <button> 包起來（互動元素不能嵌套）。
    c.tabIndex = 0;
    c.setAttribute('role','button');
    c.setAttribute('aria-label', r.name + (editOn ? '，編輯說明' : '，顯示詳細說明'));
    function show(e){ if(e) e.preventDefault(); openDetail(r); }
    c.addEventListener('click', show);
    c.addEventListener('keydown', function(e){
      // 只有焦點在卡片本身時才算。少了這一行的話，在卡片內的連結上按 Enter
      // 會同時開啟連結**和**詳細面板 —— keydown 會從連結往上冒泡，
      // 而 link() 的 stopPropagation 只擋得住 click。
      if(e.target !== c) return;
      if(e.key === 'Enter' || e.key === ' ' || e.key === 'Spacebar') show(e);
    });

    var top = el('div','card-top');
    top.appendChild(el('span','chip', r.mode_label));
    top.appendChild(el('span','pill '+r.status, STATUS[r.status]||''));
    c.appendChild(top);

    c.appendChild(el('h2', null, r.name));

    var meta = el('dl','meta');
    metaRow(meta, '名次', r.rank, null, true);
    metaRow(meta, '形式', (r.teams||[]).join(' / '));
    metaRow(meta, '區域', r.region);
    metaRow(meta, '截止', null, deadlineNode(r));
    metaRow(meta, '發現', r.first_seen_display);
    c.appendChild(meta);

    if(r.status==='expired'){
      c.appendChild(el('div','stale-note','⚠️ 內文寫的截止時間已過，標題卻沒更新 —— 請點進原帖確認還能不能報名。'));
    }

    if(r.decision==='review'){
      c.appendChild(el('div','review-note','⚠️ 待人工確認：' + (r.region||'')));
    }

    var foot = el('div','card-foot');
    foot.appendChild(link(r.url, '查看 osu! 原帖 →'));
    if(r.discord) foot.appendChild(link(r.discord, 'Discord'));
    foot.appendChild(el('span','more', editOn ? '編輯 ▸' : '詳細說明 ▸'));
    c.appendChild(foot);
    return c;
  }

  // 編輯模式下的「說明」欄位。刻意讓 textarea **預先填好現在顯示的那段字**
  // （你自己的說明，沒有的話就是程式抓的原帖節錄），因為需求是「刪減修改裡面的字」——
  // 從空白開始等於要你重打一次。
  function editorFor(r){
    var box = el('div','editor');

    if(!REPO){
      box.appendChild(el('div','ed-warn',
        '這個看板的 HTML 是在沒有 repo 資訊的情況下產生的（本機跑 --no-push 會這樣），'
        + '所以存不回去。在 GitHub Actions 產生的版本上就會正常。'));
    }

    var lab1 = el('label','ed-label','說明');
    lab1.htmlFor = 'e-summary';
    box.appendChild(lab1);
    var ta = document.createElement('textarea');
    ta.className = 'ed-input';
    ta.id = 'e-summary';
    ta.rows = 5;
    ta.value = r.summary || r.excerpt || '';
    ta.placeholder = '留空 ＝ 用程式自動抓的原帖首段節錄';
    box.appendChild(ta);

    box.appendChild(el('div','ed-hint', r.summary
      ? '目前顯示的是你寫的說明。清空再儲存就會退回自動節錄。'
      : '這格現在是程式抓的原帖首段。直接改成中文就好 —— 內容若跟原節錄一字不差，不會建立覆寫。'));

    var lab2 = el('label','ed-label','站長補充（附加在下面，可留空）');
    lab2.htmlFor = 'e-note';
    box.appendChild(lab2);
    var nb = document.createElement('textarea');
    nb.className = 'ed-input';
    nb.id = 'e-note';
    nb.rows = 2;
    nb.value = r.note || '';
    nb.placeholder = '例如：報名要在原帖回覆，沒有表單。';
    box.appendChild(nb);

    var bar = el('div','ed-bar');
    var save = el('button','ed-save','儲存到 GitHub');
    save.type = 'button';
    save.disabled = !REPO;
    var revert = el('button','ed-revert','還原成原帖節錄');
    revert.type = 'button';
    var status = el('span','edit-status','');
    status.id = 'e-status';
    bar.appendChild(save);
    if(r.summary) bar.appendChild(revert);
    bar.appendChild(status);
    box.appendChild(bar);

    revert.addEventListener('click', function(){
      // 只把內容填回去，不動 repo —— 要按「儲存」才會寫。否則這個按鈕會變成
      // 一顆沒有確認步驟的刪除鍵。
      ta.value = r.excerpt || '';
      nb.value = r.note || '';
      setEditStatus('按「儲存到 GitHub」才會生效。');
    });

    save.addEventListener('click', function(){
      var summary = ta.value.trim(), note = nb.value.trim();
      // 跟自動節錄一字不差時不建立覆寫。少了這條，使用者只是打開來看一下、順手按了
      // 儲存，就會凍結一段覆寫 —— 之後原帖更新、程式重抓了新的節錄，看板上卻還是
      // 這一句，而且從畫面上完全看不出原因。
      if(summary === (r.excerpt || '').trim()) summary = '';

      save.disabled = true; revert.disabled = true;
      setEditStatus('儲存中…');
      saveOverride(r, summary, note).then(function(res){
        // 先更新自己畫面上的那一筆，再重畫面板 —— 存檔的人不該等 30 分鐘才看到結果。
        r.summary = summary;
        r.note = note;
        if(!res.wrote){
          openDetail(r);
          setEditStatus('內容沒有變，沒有送出。');
          return;
        }
        return dispatchRun().then(function(ok){
          openDetail(r);
          setEditStatus(ok
            ? '已儲存 ✓ 已觸發更新，看板大約 1 分鐘後跟著變。'
            : '已儲存 ✓ 看板最多 30 分鐘後跟著變。');
        });
      }).catch(function(err){
        save.disabled = false; revert.disabled = false;
        setEditStatus((err && err.message) || '儲存失敗。', true);
      });
    });

    return box;
  }

  var dlg = document.getElementById('detail');

  function openDetail(r){
    current = r;
    document.getElementById('d-chip').textContent = r.mode_label;
    var pill = document.getElementById('d-pill');
    pill.className = 'pill ' + r.status;
    pill.textContent = STATUS[r.status] || '';
    document.getElementById('d-name').textContent = r.name;

    var body = document.getElementById('d-body');
    body.textContent = '';

    var meta = el('dl','meta');
    metaRow(meta, '名次', r.rank_full || r.rank, null, true);
    metaRow(meta, '形式', (r.teams||[]).join(' / '));
    metaRow(meta, '區域', r.region);
    metaRow(meta, '截止', null, deadlineNode(r));
    metaRow(meta, '主辦', r.author);
    metaRow(meta, '發現', r.first_seen_display);
    body.appendChild(meta);

    if(r.decision === 'review'){
      body.appendChild(el('div','review-note','⚠️ 待人工確認：' + (r.region||'')));
    }
    if(r.status === 'expired'){
      body.appendChild(el('div','stale-note','⚠️ 內文寫的截止時間已過，標題卻沒更新 —— 請點進原帖確認還能不能報名。'));
    }

    if(editOn){
      body.appendChild(editorFor(r));
    } else {
      // 摘要＝原帖首段原文，只截不改。加註出處，避免被當成我們的轉述。
      // 站長自己寫的說明用同一種排版但換來源字樣與直條顏色 —— 那種情況下
      // 它**不是**節錄，標成節錄就是把我們的話記在原帖作者頭上。
      var own = !!r.summary, text = r.summary || r.excerpt;
      if(text){
        var q = el('div','quote' + (own ? ' own' : ''));
        q.appendChild(document.createTextNode(text));
        q.appendChild(el('span','src', own ? '— 站長說明' : '— 原帖首段節錄'));
        body.appendChild(q);
      }
      if(r.note){
        var nb2 = el('div','owner-note');
        nb2.appendChild(el('span','owner-src','站長補充'));
        nb2.appendChild(document.createTextNode(r.note));
        body.appendChild(nb2);
      }
    }
    if(r.deadline_raw){
      var dq = el('div','quote');
      dq.appendChild(document.createTextNode(r.deadline_raw));
      dq.appendChild(el('span','src','— 原帖關於報名時間的說法'));
      body.appendChild(dq);
    }
    if(r.title){
      var tq = el('div','quote');
      tq.appendChild(document.createTextNode(r.title));
      tq.appendChild(el('span','src','— 原帖標題（狀態判定依此為準）'));
      body.appendChild(tq);
    }

    var foot = document.getElementById('d-foot');
    foot.textContent = '';
    foot.appendChild(link(r.url, '查看 osu! 原帖 →'));
    if(r.discord) foot.appendChild(link(r.discord, 'Discord'));
    if(r.signup_form) foot.appendChild(link(r.signup_form, '報名表單'));

    tickCountdowns();
    if(!dlg.open) dlg.showModal();
  }

  document.getElementById('d-close').addEventListener('click', function(){ dlg.close(); });
  // 點 backdrop 關閉。內容包在 .detail-inner 裡，所以點內文空白處不會誤關
  // （事件 target 會是那個 div，不是 <dialog> 本身）。
  dlg.addEventListener('click', function(e){ if(e.target === dlg) dlg.close(); });

  // ------------------------------------------------------------------
  // 編輯模式的開關與權杖面板
  // ------------------------------------------------------------------

  var editHint = document.getElementById('edit-hint');
  var ehText = document.getElementById('eh-text');
  var tdlg = document.getElementById('token');

  // 用 DOM 組而不是 innerHTML：這裡唯一的字串是 repo 名稱，它來自 Actions 的
  // GITHUB_REPOSITORY（或你自己下的 --repo），不是外人能控的值 —— 但正在編輯
  // 別人的內容時插入 HTML 字串，這種習慣不值得養。
  (function paintTokenPanel(){
    document.getElementById('t-repo').textContent = REPO || '（這個看板沒有 repo 資訊）';
    if(!REPO) return;
    var warn = document.getElementById('t-warn');
    warn.appendChild(document.createTextNode('⚠️ '));
    warn.appendChild(el('b', null, REPO.split('/')[0] + '.github.io'));
    warn.appendChild(document.createTextNode(
      ' 是這個帳號所有 Pages 專案共用的網域，而 localStorage 以網域為界、不分路徑。'
      + '日後若在別的 repo 開 Pages 又載入了別人的 JS，那個 JS 讀得到這把權杖 ——'
      + '所以那把權杖只給這個 repo 的 Contents 權限就好。'));
  })();

  // 畫面上沒有任何編輯按鈕 —— 訪客看到的就只是一條賽事看板。要進來有兩條路，
  // 兩條都不會在頁面上留痕跡，選哪條純看當下哪個順手：
  //
  //   1. 在頁面上直接打 edit（焦點不在輸入框時）—— 桌機最快，不用離開看板
  //   2. 網址後面加 #edit —— 手機、或直接把這個網址存成書籤
  //
  // 兩個都是**切換**：再打一次 edit（或把 #edit 拿掉）就退出。
  //
  // 這純粹是門面，不是安全機制 —— 藏起來的入口擋不住任何人，真正擋住的是那把權杖：
  // 沒有它，呼叫 GitHub API 一律 401，改不動 repo 裡任何一個位元。
  function paintEdit(){
    editHint.hidden = !editOn;
    if(!editOn) return;
    ehText.textContent = REPO
      ? '編輯模式：點任一張卡片就能改它的說明。'
      : '編輯模式：可以改字，但這個看板沒有 repo 資訊，存不回去。';
  }

  function openTokenDialog(){
    // 刻意**不**關掉詳細面板：使用者常常是編輯到一半發現權杖不對才來換，
    // 關掉等於把他剛打的字丟掉。兩張都是 modal <dialog>，第二張會疊在上面，
    // Esc 先關最上面那張，順序是對的。
    document.getElementById('t-input').value = readToken();
    if(!tdlg.open) tdlg.showModal();
  }

  function redrawDetail(){
    if(current && dlg.open) openDetail(current);   // 面板開著就當場換成對應的版本
  }

  function enterEdit(){
    if(!readToken()){ openTokenDialog(); return; }
    editOn = true; paintEdit(); render(); redrawDetail();
  }

  function exitEdit(){
    if(!editOn) return;
    editOn = false; paintEdit(); render(); redrawDetail();
  }

  function toggleEdit(){ if(editOn) exitEdit(); else enterEdit(); }

  // 在頁面上直接打 edit。焦點在輸入框裡時不算 —— 少了這道守門，在編輯器的
  // 文字框裡打這四個字母會把自己踢出編輯模式，而且游標還會卡在半路。
  var typed = '';
  window.addEventListener('keydown', function(e){
    var t = e.target;
    if(t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return;
    if(e.ctrlKey || e.metaKey || e.altKey) return;
    // 只累積單一字母，其餘按鍵（Enter、方向鍵、Esc…）一律把進度清掉。
    typed = (e.key && e.key.length === 1) ? (typed + e.key.toLowerCase()).slice(-4) : '';
    if(typed === 'edit'){ typed = ''; toggleEdit(); }
  });

  document.getElementById('eh-token').addEventListener('click', openTokenDialog);
  document.getElementById('eh-exit').addEventListener('click', exitEdit);

  document.getElementById('t-close').addEventListener('click', function(){ tdlg.close(); });
  tdlg.addEventListener('click', function(e){ if(e.target === tdlg) tdlg.close(); });
  document.getElementById('t-save').addEventListener('click', function(){
    writeToken(document.getElementById('t-input').value.trim());
    tdlg.close();
    // 存了一把空權杖＝清掉它：順著退出編輯模式。留著一個存不回去的畫面，
    // 只會讓人打到一半才發現白做。反過來，存了有效權杖就直接進編輯模式 ——
    // 都已經打開面板貼上權杖了，不該還要再打一次 edit。
    if(!readToken()){ exitEdit(); return; }
    enterEdit();
  });
  document.getElementById('t-clear').addEventListener('click', function(){
    writeToken('');
    document.getElementById('t-input').value = '';
    tdlg.close();
    exitEdit();
  });

  // 當場貼上／拿掉 #edit 就生效，不必重新載入。面板開著的話 redrawDetail 會
  // 順手換成對應的版本（可編輯 ⇄ 唯讀），免得畫面上那份跟新的模式對不起來。
  window.addEventListener('hashchange', function(){
    if(location.hash === '#edit') enterEdit();
    else exitEdit();
  });

  function render(){
    var grid = document.getElementById('grid');
    grid.textContent='';
    var list = visible();
    document.getElementById('empty').hidden = list.length>0;
    list.forEach(function(r){ grid.appendChild(card(r)); });

    var inc = rows.filter(function(r){return r.decision==='include';});
    document.getElementById('s-open').textContent = inc.filter(function(r){return r.status==='open';}).length;
    document.getElementById('s-total').textContent = inc.length;
    document.getElementById('s-review').textContent = rows.filter(function(r){return r.decision==='review';}).length;

    var m = payload.meta||{};
    document.getElementById('foot-meta').textContent =
      '賽事總筆數 ' + rows.length + '　·　最後更新 ' + (m.last_run_taipei||'—') + '（台北時間）';

    // 卡片是重新產生的，倒數的 span 也跟著換新 —— 補算一次。
    tickCountdowns();
  }

  buildGroup(document.getElementById('f-mode'), MODES, 'mode');
  buildGroup(document.getElementById('f-status'), STATUSES, 'status');
  // 不再用 JS 把 checked 設回 true：那會跟瀏覽器還原的狀態打架，而且誰贏要看時機。
  // 交給 HTML 的 checked 屬性決定預設值就好。
  reviewToggle.addEventListener('change', render);

  paintEdit();
  render();
  // 帶著 #edit 進來的只可能是站長本人。直接進編輯模式 —— 還沒有權杖的話
  // enterEdit 會把權杖面板打開，而那正是他這一步要的東西。
  if(location.hash === '#edit') enterEdit();
  // 只改倒數那幾個字，不重繪整頁 —— 重繪會把正在讀的卡片與捲動位置跳掉。
  setInterval(tickCountdowns, 60000);
})();
</script>
</body>
</html>
"""
