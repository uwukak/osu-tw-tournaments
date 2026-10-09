"""產生 docs/index.html（線上賽事看板）與 drafts/{topic_id}.md（繁中貼文草稿）。

純函式：不碰網路、不讀時鐘。所有時間由呼叫端傳入，確保「相同輸入 → 位元組相同輸出」。
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
    tournaments: dict[str, Any], meta: dict[str, Any], now_iso: str
) -> dict[str, Any]:
    """把內部記錄整理成看板要用的資料（只放 include 與 review）。

    顯示條件：仍在報名中，**或**最近還在論壇列表上（`DASHBOARD_WINDOW_DAYS` 內）。
    只有這樣，被新帖擠出第一頁、但還在報名的比賽才不會從看板消失。

    狀態一律經過 `effective_status` 校正 —— 標題沒改、內文早已截止的（例如 SMST 83）
    會變成 "expired"，因此不再算進「報名中」，也不再無條件常駐看板。
    """
    rows: list[dict[str, Any]] = []

    for record in tournaments.values():
        if record.get("decision") not in ("include", "review"):
            continue
        status = effective_status(record, now_iso)
        if status != "open" and not store.is_fresh(record, now_iso):
            continue

        tid = record["topic_id"]
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
  --border:#EADFE5; --accent:#D62E77; --accent-soft:#FCE7F0;
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
    --border:#2E2735; --accent:#FF66AA; --accent-soft:#3A1F2E;
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
  --border:#2E2735; --accent:#FF66AA; --accent-soft:#3A1F2E;
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
.group button[aria-pressed="true"]{background:var(--accent); color:#fff; font-weight:500}
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

  <footer>
    <div id="foot-meta"></div>
    <div>資料來源：<a href="https://osu.ppy.sh/community/forums/55?sort=created" style="color:var(--accent)">osu! 論壇 Tournaments 版</a>。
    區域與欄位以關鍵字規則自動判定，可能有誤 —— 標示「待確認」者請自行核對原帖。<br>
    時間一律換算成 <b>UTC+8</b>（台北）；「剩餘…」由你的瀏覽器即時計算，會隨時間自己更新。<br>
    「<b>表定已截止</b>」表示標題仍寫著報名開放，但帖內文寫的截止時間已經過去（主辦忘了改標題）。<br>
    點卡片可以看原帖首段的節錄與完整欄位。</div>
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
    c.setAttribute('aria-label', r.name + '，顯示詳細說明');
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
    foot.appendChild(el('span','more','詳細說明 ▸'));
    c.appendChild(foot);
    return c;
  }

  var dlg = document.getElementById('detail');

  function openDetail(r){
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

    // 摘要＝原帖首段原文，只截不改。加註出處，避免被當成我們的轉述。
    if(r.excerpt){
      var q = el('div','quote');
      q.appendChild(document.createTextNode(r.excerpt));
      q.appendChild(el('span','src','— 原帖首段節錄'));
      body.appendChild(q);
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

  render();
  // 只改倒數那幾個字，不重繪整頁 —— 重繪會把正在讀的卡片與捲動位置跳掉。
  setInterval(tickCountdowns, 60000);
})();
</script>
</body>
</html>
"""
