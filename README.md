# 台灣 osu! 賽事彙整

自動巡 osu! 論壇的 **Tournaments 版**，挑出台灣玩家能報名的錦標賽，並：

- 維護一份**線上賽事看板**（GitHub Pages）
- 產生**繁體中文的 Facebook 貼文草稿**，人工複製貼上即可發文

每 30 分鐘自動跑一次，**不需要你自己的電腦開機**。

---

## 它怎麼運作

```
GitHub Actions（每 30 分鐘）
      │
      ├─ 抓 osu! 論壇 Tournaments 版（依建立時間排序）       ← 1 次請求
      ├─ 比對已知的 topic id，找出新比賽                     ← 新帖靠 id 遞增偵測
      ├─ 只對「新帖」抓主題頁，讀首帖內文
      ├─ 關鍵字規則抽取：模式／賽事名／名次／隊伍／區域／報名狀態
      ├─ 寫入 data/tournaments.json
      ├─ 產生 docs/index.html       → GitHub Pages 線上看板
      └─ 產生 drafts/{id}.md        → 你複製到 Facebook
              │
              └─ 資料或看板內容有變更才 git commit + push（沒變更不會產生假提交）
```

### 為什麼是關鍵字規則而不是 AI

不需要 API key、零成本、可無人值守、每次結果一致（可寫成測試）。
代價是講得含蓄的比賽會判錯 —— 所以有「待確認」這一類，交給人複審。

---

## 一次性設定

### 1. 建立 repo

建立一個 **public** 的 GitHub repo（Pages 免費、Actions 分鐘數無限），把這個資料夾推上去。

> ⚠️ **必須先在這個資料夾裡 `git init`。**
> `git add -A` 從 git 2.0 起作用於**整個工作區**，不是當前目錄。如果這個資料夾自己不是
> repo，git 會往上層找 —— 萬一上層（例如 `C:\Users\<你>`）已經有另一個 repo，就會把
> 整個上層目錄 commit 進去，然後推上公開 repo。
> `osu_tourney/gitio.py` 已加防護：倉庫根目錄不是專案目錄就直接拒絕提交、讓排程變紅。

```bash
cd /d "路徑\到\這個資料夾"     # 先確認自己在專案資料夾裡
git init
git status                    # ← 先看這份清單，應該只有專案檔
git add -A
git commit -m "初始版本"
git branch -M main
git remote add origin https://github.com/<你的帳號>/<repo>.git
git push -u origin main
```

`git status` 那份清單裡如果出現 `AppData/`、`NTUSER.DAT`、`.claude/` 之類的東西，
代表 `git init` 沒有生效（你還在上一層的 repo 裡）—— **停下來，不要 commit**。

### 2. 開啟 GitHub Pages

repo → **Settings → Pages** → Source 選 **Deploy from a branch** → Branch 選 **main**、資料夾選 **`/docs`** → Save。

過一兩分鐘，看板會在：

```
https://<你的帳號>.github.io/<repo>/
```

### 3. 開啟 Actions 寫入權限

repo → **Settings → Actions → General → Workflow permissions** → 選 **Read and write permissions** → Save。

（`scrape.yml` 裡已宣告 `permissions: contents: write`，但 repo 層級的設定也要允許。）

### 4. 首次回填

到 **Actions → 更新賽事資料 → Run workflow** 手動觸發一次。

第一次請先用 `--seed-only`（見下方「首次回填」），否則會一次噴出十幾份草稿。

---

## 本機使用

```bash
pip install -r requirements.txt

python tests/test_rules.py              # 規則測試（不須 pytest）
python tests/test_store.py              # 資料合併與「有變更才提交」的測試
python tests/test_render.py             # 報名狀態校正（標題沒改、內文已截止）
python -m osu_tourney.scrape --offline  # 用 fixtures/ 跑，完全不碰網路
python -m osu_tourney.scrape --no-push  # 真的去抓，但只寫本機檔案、不提交
python -m osu_tourney.scrape --push     # 抓完並提交推送
```

> **Windows 注意**：新開的 cmd 可能找不到 `python`（PATH 問題），用 `py` 即可。
> 另外 `-m osu_tourney.scrape` 是從**當前目錄**找模組，所以要先 `cd` 到專案資料夾。
> 中文路徑與空白不影響，但指令要加引號：`cd /d "C:\...\新增資料夾 (6)"`。

| 參數 | 用途 |
|---|---|
| `--offline` | 用 `fixtures/` 的快照跑，離線驗證規則與看板 |
| `--no-push` | 預設。只寫檔案，不碰 git |
| `--push` | 提交並推送（Actions 用這個） |
| `--seed-only` | 首次回填：寫入資料與看板，但**不產生草稿** |
| `--limit N` | 本回合最多抓 N 個主題頁（想省請求時用） |
| `--since-topic-id N` | 只處理 id 大於 N 的主題 |
| `--refresh-days N` | 報名中的賽事幾天後重抓明細（預設 3） |

### 首次回填

```bash
python -m osu_tourney.scrape --seed-only --no-push
```

這會把目前列表上的 50 筆賽事寫進 `data/tournaments.json`、產生看板，並記下
`draft_baseline_topic_id`。之後**只有比這個 id 新的賽事**才會產生草稿 ——
否則第一次跑就會噴出十幾份你根本來不及發的草稿。

---

## 檔案配置

| 路徑 | 說明 |
|---|---|
| `osu_tourney/rules.py` | ★ **所有關鍵字規則**。要調判定邏輯就改這裡 |
| `osu_tourney/fetch.py` | 唯一碰網路的地方（UA、重試、退避） |
| `osu_tourney/parse.py` | HTML → 資料（不含政策） |
| `osu_tourney/store.py` | 讀寫 `data/tournaments.json` |
| `osu_tourney/render.py` | 產生看板與草稿 |
| `osu_tourney/gitio.py` | 唯一呼叫 git 的地方 |
| `osu_tourney/scrape.py` | 進入點（用 `python -m` 執行） |
| `osu_tourney/poster.py` | Facebook 發文接縫（本期未實作） |
| `data/tournaments.json` | 累積的賽事資料，key 是 topic id |
| `docs/index.html` | 看板產物（GitHub Pages 根目錄） |
| `drafts/` | 產生的繁中草稿 |
| `fixtures/` | 真實 osu! 快照，供測試用 |
| `tests/test_rules.py` | 黃金測試：規則與分類分布 |
| `tests/test_store.py` | 黃金測試：資料合併與變更偵測 |
| `tests/test_render.py` | 黃金測試：報名狀態校正與草稿標題 |

---

## 調校規則

規則全部集中在 `osu_tourney/rules.py`，改完務必跑：

```bash
python tests/test_rules.py
python tests/test_store.py
python tests/test_render.py
```

測試會斷言真實語料上的分類分布。若你**刻意**改了規則，測試會失敗並印出新的分布 ——
確認過再更新 `EXPECTED_DECISIONS`。若沒改規則卻失敗，那就是回歸。

### 判定結果的三種可能

| 結果 | 意思 |
|---|---|
| `include` | 收錄（無區域限制，或明確含台灣／亞洲／SEA／中文圈） |
| `exclude` | 排除（限定其他國家／區域／語言，或是徵工作人員的帖） |
| `review` | **待人工確認** —— 看板會標示⚠️，草稿也會加警告 |

被判成 `review` 的常見原因：線下賽（LAN，要自己看地點）、邀請賽、區域代碼無法判定、
缺少名次與隊伍資訊（可能是情報帖而非比賽）。

---

## 貼文草稿的格式

草稿在 `drafts/{topic_id}.md`，標題格式是 **模式＋賽事名＋報名時間**：

```
【osu!standard】SMST 84（50K–100K）｜報名開放中
```

```
🎮 模式｜osu!standard
🏆 賽事｜SMST 84
📊 名次｜50,000 – 100,000
👥 形式｜1v1 / 2v2
🌏 區域｜無區域限制（全球開放）
📝 報名｜報名開放中・截止 10/17 07:59（台北時間）
🔗 資訊｜https://osu.ppy.sh/community/forums/topics/2252361
💬 Discord｜https://discord.gg/6d4pF59
```

**手改過的草稿不會被覆蓋** —— 程式用雜湊比對，偵測到你改過就保留下來。

---

## 之後接自動發文

目前只產生草稿。要改成自動發到粉絲專頁：

1. 建立 Meta App，取得粉絲專頁的長期存取權杖（需要 `pages_manage_posts` 權限）
2. 在 repo 設定 `FB_PAGE_ID` 與 `FB_PAGE_TOKEN` 兩個 secret
3. 把 `osu_tourney/poster.py` 的 `post()` 補上對
   `https://graph.facebook.com/v21.0/{page_id}/feed` 的 POST

建議先只對 `include` 的賽事自動發，`review` 的仍留人工。

---

## 已知限制

- **GitHub Actions 是機房 IP**，可能被 osu! 的 Cloudflare 擋。程式遇到阻擋會**直接中止且不寫檔**，
  不會用空資料覆蓋歷史。若常發生，改成本機排程（Windows 工作排程器跑 `--push`）。
- **區域判定靠標題**。區域限制詳細寫在首帖內文時，規則看不到 —— 這類會被判成「無區域限制」而收錄。
- **截止時間只能低信心猜測**。句子抓得到，但裡面的日期常常夾帶賽程日期、又沒寫年份。
  猜不出來時草稿會顯示原句，不會編一個時間給你。
- **報名狀態以標題為準，但會用內文的截止時間校正**。主辦忘了把 `[Open]` 改掉時
  （真實案例：SMST 83，標題寫開放、內文 9/25 就截止，過了兩週還顯示「報名開放中」），
  看板與草稿會改標成 **「表定已截止」**。反過來，若內文的截止時間猜錯，也可能誤標 ——
  所以這種卡片不會被藏起來，只會變灰並附上提示，請點進原帖確認。
- **每週系列賽**（例如 `#51 week ... cup (weekly)`）會每週產生一篇草稿。
- 看板只顯示收錄與待確認的賽事；被排除的仍保存在 `data/tournaments.json` 裡，不會刪除。
