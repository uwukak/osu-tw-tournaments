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
      ├─ 讀 data/overrides.json     ← 你在看板上直接改的說明（爬蟲只讀，永不覆寫）
      ├─ 讀 data/custom.json        ← 你在看板上手動新增的比賽（同上，爬蟲只讀）
      ├─ 產生 docs/index.html       → GitHub Pages 線上看板
      └─ 產生 drafts/{id}.md        → 你複製到 Facebook
              │
              └─ 資料或看板內容有變更才 git commit + push（沒變更不會產生假提交）
```

看板本身也能反向寫回來：打開隱藏的編輯模式（在頁面上打 `edit`，或網址加 `#edit`）後，
頁面會拿著你的 GitHub 權杖直接呼叫 API，把修改提交回 repo —— 改既有賽事寫
`data/overrides.json`，手動新增或刪除的比賽寫 `data/custom.json`
（見下方「在網站上直接改資料」與「手動新增比賽」）。訪客看不到任何編輯按鈕。

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
python tests/test_parse.py              # 首帖摘要的截斷
python tests/test_gitio.py              # 提交前的防護（不准推到上層目錄）
python tests/test_dashboard_js.py       # 看板的 inline script 能不能被 node 解析
python tests/test_custom_roundtrip.py   # 手動新增的比賽走一遍完整流程（在暫存目錄裡跑）
python -m osu_tourney.scrape --offline  # 用 fixtures/ 跑，完全不碰網路
python -m osu_tourney.scrape --no-push  # 真的去抓，但只寫本機檔案、不提交
python -m osu_tourney.scrape --push     # 抓完並提交推送
```

**改過 `parse.py`（或 `rules.py` 的 `extract_deadline`）之後**，資料檔裡既有的欄位還是舊解析器的
產物 —— 平常的重抓規則綁在「標題說報名中」上，標題寫 `unknown` 或已截止的賽事永遠不會被重算。
改完解析器要主動重抓一次：

```bash
python tests/test_parse.py                              # 先確認解析結果是對的
python -m osu_tourney.scrape --push --refetch-details   # 再重抓本次列表上每個主題的明細
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
| `--refetch-details` | 忽略新鮮度與報名狀態，重抓列表上**每個**主題的明細 |
| `--repo owner/repo` | 看板編輯功能的目標 repo（Actions 會自動帶 `GITHUB_REPOSITORY`） |
| `--branch main` | 看板編輯要提交到哪個分支（預設 `main`） |

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
| `data/tournaments.json` | 累積的賽事資料，key 是 topic id（爬蟲寫，**不要手改**） |
| `data/overrides.json` | 站長自己寫的說明，key 是 topic id（爬蟲只讀不寫） |
| `data/custom.json` | 站長手動新增的比賽，key 是負整數 id（爬蟲只讀不寫） |
| `docs/index.html` | 看板產物（GitHub Pages 根目錄） |
| `drafts/` | 產生的繁中草稿 |
| `fixtures/` | 真實 osu! 快照，供測試用 |
| `tests/test_rules.py` | 黃金測試：規則與分類分布 |
| `tests/test_store.py` | 黃金測試：資料合併、變更偵測、覆寫檔的讀取 |
| `tests/test_render.py` | 黃金測試：報名狀態校正、看板資料、草稿標題 |
| `tests/test_parse.py` | 黃金測試：首帖摘要的截斷 |
| `tests/test_gitio.py` | 黃金測試：提交前的防護（不准推到上層目錄） |
| `tests/test_dashboard_js.py` | 黃金測試：看板的 inline script 要能被 JS 引擎解析 |
| `tests/test_custom_roundtrip.py` | 黃金測試：手動新增的比賽整條路（`scrape.main()`，在暫存目錄裡跑） |

---

## 調校規則

規則全部集中在 `osu_tourney/rules.py`，改完務必跑：

```bash
python tests/test_rules.py
python tests/test_store.py
python tests/test_render.py
python tests/test_parse.py
python tests/test_gitio.py
python tests/test_dashboard_js.py
```

測試會斷言真實語料上的分類分布。若你**刻意**改了規則，測試會失敗並印出新的分布 ——
確認過再更新 `EXPECTED_DECISIONS`（還有分頁用的 `EXPECTED_KINDS`）。若沒改規則卻失敗，
那就是回歸。

### 判定結果的三種可能

| 結果 | 意思 |
|---|---|
| `include` | 收錄（無區域限制，或明確含台灣／亞洲／SEA／中文圈） |
| `exclude` | 排除（限定其他國家／區域／語言） |
| `review` | **待人工確認** —— 看板會標示⚠️，草稿也會加警告 |

被判成 `review` 的常見原因：線下賽（LAN，要自己看地點）、邀請賽、區域代碼無法判定、
缺少名次與隊伍資訊（可能是情報帖而非比賽）。

### 分頁：選手報名／工作人員報名

看板上方有一組分頁，把「報名比賽」和「徵求工作人員」分開 —— 兩者是不同的資訊，
但對社群一樣有用。

分頁的歸屬是 `kind` 這個欄位，有**三個**值而不是兩個：

| `kind` | 意思 |
|---|---|
| `player` | 選手賽事（預設） |
| `staff` | 標題本身就在徵人（`[STAFF REGS]`、`[STAFF RECRUITMENT]`） |
| `both` | 標題**同時明講**徵選手與徵工作人員，兩頁都列 |

`both` 是刻意的設計：分頁是**檢視**，不是把資料切成兩半。一頁看不到的資訊對那一頁的
讀者就是不存在，所以「兩者都徵」的比賽兩頁都要出現。

判定寫在 `rules.detect_kind()`，刻意要求「工作人員」與「選手／隊伍」字樣**同時**出現才算
`both` —— 只看 `staff` 一個字的話，`[osu!catch] UK Catch Tournament 2026 (Staff Wanted)`
這種「比賽順便徵人」會被整批歸到工作人員分頁。

兩種情況仍以區域判定優先：`[STAFF REGS]` 掛在日本限定的比賽上，一樣不會出現在台灣看板上
（`kind` 仍然是 `staff`，只是整筆被排除）。

分頁判錯的時候，在編輯模式的「分頁」那一格改就好 —— 它跟「收錄判定」一樣是手改得動的
下拉選單。既有的資料沒有 `kind` 這個欄位，一律當成 `player`，所以不會有賽事在某次改版後
突然跳進工作人員分頁。

工作人員帖也會產生 Facebook 草稿，只是措辭換成「工作人員招募中」／「🙋 招募」，
不會寫成「報名開放中」—— 那會讓讀者以為是去報名比賽，而那場比賽根本不收選手。

---

## 看板

`docs/index.html` 是單一自足頁面（CSS／JS／資料全部內嵌），推到 GitHub Pages。

- 最上方**分頁**切換「選手報名／工作人員報名」（見上方「分頁」一節）；按鈕上的數字是
  那一頁有幾筆，所以「工作人員頁空著」跟「今天沒有比賽」分得出來
- 卡片列出賽事，可用**模式**與**報名狀態**篩選；「顯示待確認」控制 review 那批要不要出現
- 清單依**報名狀態**排序：報名開放 → 未標明 → 已截止（見下）
- **點卡片**會彈出詳細說明：原帖首段的節錄、主辦自己寫的截止那句話、原帖完整標題、完整名次與主辦
- 時間一律顯示成 `MM/DD HH:MM（UTC+8）`，後面接**剩餘時間**（`剩餘 3 天`／`剩餘 5 小時 12 分`）

清單排序刻意看的是**能不能報**，而不是發現時間。讀者是來找「現在還能報名」的比賽的；
三種狀態混在一起時，每一列都得先讀狀態才知道要不要停下來，集中起來只要掃上半頁。

**標題說已截止的**與**內文截止時間已過的**刻意同分（`STATUS_ORDER` 裡都是 2）—— 對讀者
來說兩者都是「不能報了」，再分誰先誰後沒有意義。同分時沿用原本的次要排序：收錄優先，
再依發現時間新到舊，最後才是 topic id。`tests/test_render.py` 有一組測試釘著這件事。

關於時間有兩件事是刻意這樣設計的：

- **絕對時間由 Python 算好，倒數由瀏覽器現算。** 看板是靜態頁，可能好幾小時前就產生；
  用產生時間去算「剩餘幾小時」，一打開就已經是錯的。所以 `deadline_iso`（原始 UTC）也放進
  資料裡，讓 JS 每分鐘自己重算一次 —— 只改那幾個字，不重繪整頁。
- **草稿裡沒有倒數。** 貼文是被讀者稍後才看到的，寫「剩餘 3 天」只會誤導。

「簡要說明」用的是**原帖首段的原文節錄（180 字內，切在句尾）**，不摘要、不改寫、不翻譯，
並標明出處。判斷留給讀者，點一下就能回到原帖。

### 在網站上直接改資料

看板上**沒有任何編輯按鈕** —— 訪客看到的就只是一條賽事看板。編輯模式是藏起來的，
要打開有兩條路，都不會在頁面上留下痕跡：

| 怎麼開 | 適合 |
|---|---|
| 在頁面上直接打 `edit`（四個字母，不用按 Ctrl） | 桌機最快，不用離開看板 |
| 網址後面加 `#edit` | 手機，或直接存成書籤 |

```
https://uwukak.github.io/osu-tw-tournaments/#edit
```

兩個都是**切換**：再打一次 `edit`（或把 `#edit` 拿掉）就退出。第一次進來還沒有權杖時，
權杖面板會自己打開。

進編輯模式後，工具列下方會多一行提示，右邊附**更換權杖**和**結束編輯**兩個連結。
點任一張卡片就能改那筆的**任何欄位**；按儲存會寫進 `data/overrides.json`。
訪客不會看到那一行，也不會看到卡片上的「編輯 ▸」（他們看到的是「詳細說明 ▸」）。

**可以改的欄位：**

| 欄位 | 什麼時候用 |
|---|---|
| 賽事名稱 | 標題是英文／縮寫，想給台灣讀者一個看得懂的名字 |
| 模式 | 規則把 taiko 判成 standard 之類 |
| 鍵數 | mania 的 4K／7K |
| 名次（卡片顯示）／名次（完整） | 規則抓錯區間，或主辦改用另一種寫法 |
| 隊伍形式 | 例如 `1v1 / 2v2`，多個用 `/` 分隔 |
| 區域 | 規則判不了的那批（`MN` 是 Minnesota 還是 Mongolia？） |
| 報名狀態 | **規則判錯時最常改的一格**，見下面 |
| 收錄判定 | 把「待確認」升成「收錄」，或反過來排除掉 |
| 分頁 | 這筆該出現在「選手報名」還是「工作人員報名」頁（見上方「分頁」一節） |
| 截止時間（UTC）／截止時間（主辦原句） | 解析器猜錯時間，或那句話抓得不完整 |
| Discord／報名表單／直播 | 抓錯連結，或連結是後來才補上的 |
| 資訊連結 | **只有手動新增的比賽才有這一格**（爬蟲那批的網址是從 topic id 算出來的） |
| 說明／站長補充 | **只有看板看得到**，不會進到 Facebook 草稿 |

**「報名狀態未標明」和「表定已截止」怎麼改。** 這兩種都是規則的推論，不是事實：

- **未標明**＝標題和內文都沒寫報名開不開放。卡片上照實寫「未標明」，不猜。
- **表定已截止**＝標題寫著開放／未標明，但**內文那句截止時間已經過去了**（主辦忘了改標題）。
  真實案例 SMST 83：標題還掛著 `[Open]`，內文寫的 9/25 早就過了兩週。

要人工改，就在編輯模式的「報名狀態」那格選。**選了就以你為準**，程式不會再用截止時間
去校正它 —— 否則你改成「開放中」，下一回合又被同一條規則算回「表定已截止」，
改了等於沒改。那一格旁邊會直接寫出「目前看板顯示『表定已截止』—— 內文寫的截止時間
已經過去，程式自己校正的」，讓你清楚自己在覆蓋什麼。

**它怎麼運作的。** 看板是 GitHub Pages 的靜態頁，沒有後端 —— 所以「在網站上改並存下來」
唯一可行的做法，是讓那個頁面拿著一把你的 GitHub 權杖，直接呼叫 GitHub API 產生 commit。
權杖只存在**你自己的瀏覽器**（localStorage），不會進 repo、也不會出現在 HTML 裡；
其他訪客的頁面沒有它，他們看到的永遠是已提交的內容。

**第一次要設定權杖**（打 `edit` 進編輯模式時會自己跳出來，也可以按那行右邊的「更換權杖」）：

1. GitHub → Settings → Developer settings → **Fine-grained tokens** → Generate new token
2. Repository access 只選 `uwukak/osu-tw-tournaments`
3. Permissions → **Contents: Read and write**
4. （選用）**Actions: Read and write** —— 有這個的話，存檔後會順手觸發一次排程，
   看板大約 1 分鐘後就更新；沒有就等下一回合，最多 30 分鐘。
5. 貼進面板、按「儲存並開始編輯」

**幾個要知道的事：**

- **入口藏起來只是門面，不是安全機制。** 任何人打 `edit` 或加上 `#edit` 都進得了編輯模式、
  也都打得開權杖面板 —— 但沒有權杖就什麼都存不了（GitHub API 一律回 401），
  也讀不到你存在自己瀏覽器裡的那把。真正擋住寫入的是權杖，不是這個入口。
- **權杖是「能改這個 repo」的憑證。** 頁面本身只載 Google Fonts 跟自己的 inline script，
  沒有第三方 JS，所以被偷的機率低 —— 但風險不是零，所以那把權杖的最小權限就好，
  不要給 classic token 的 `repo`（那等於整個帳號）。
- **`uwukak.github.io` 是你所有 Pages 專案共用的網域**，而 localStorage 以網域為界、
  不分路徑。現在只有這一個站所以沒事；日後若在別的 repo 開 Pages 又載了別人的 JS，
  那個 JS 讀得到同一把權杖。
- **換瀏覽器／換電腦要重設一次。** 權杖不會跟著 repo 跑。
- **手改的值不會被爬蟲蓋掉。** 覆寫寫在 `data/overrides.json`，爬蟲只讀不寫它。
  手改是**疊在顯示層**上，不是寫回 `data/tournaments.json` —— 那裡永遠留著「機器當下
  看到了什麼」，所以哪天解析器修好、重算出正確結果，你看得出它變了，也隨時能把
  覆寫刪掉退回自動判定。
- **跟自動判定一字不差的欄位不會寫進檔案**：你只是打開來看一下、順手按了儲存，
  不會把當下的判定結果整批凍結住。按「還原成自動判定」可以一鍵把表單填回原始值。
- **把一個欄位清空 ＝ 這一欄就是要空的**（例如解析錯的 Discord）。這跟「沒改」是不同的
  兩件事，檔案裡用 `null` 表示；沒有它，清空只會被當成沒覆寫，下一回合那個值又自己長回來。
- **手改的欄位也會進到 Facebook 草稿**（除了「說明」與「站長補充」那兩欄 —— 那是看板
  專用的註解，貼出去會很突兀）。名次判錯正是你動手改的原因，草稿卻寫著舊的，貼出去就是
  發錯文。
- **`overrides.json` 壞掉的話排程會變紅、整個停住**（不寫檔、不提交）。這是刻意的：
  當成空檔繼續跑，你的說明會整批從看板上消失，而且因為 `dashboard_sha` 跟著變了，
  這個「消失」還會被當成一次正常更新提交出去。修好它，或直接刪掉那個檔案。
- **把 `decision` 改成「排除」之後，舊的草稿檔不會自己消失。** 看板與 `drafts/README.md`
  會正確地不再列出它，但 `drafts/{topic_id}.md` 還躺在磁碟上，要自己刪。
- **本機測這個功能**要自己帶 repo：`py -m osu_tourney.scrape --no-push --repo uwukak/osu-tw-tournaments`。
  沒帶的話編輯面板會打開但存不回去（會直接告訴你）。

### 手動新增比賽

論壇上抓不到的比賽（社群自辦、只在 Discord 公告的）可以自己加。

進編輯模式後，那行提示的最左邊會多一顆 **`＋ 新增賽事`**（訪客看不到它）。點下去開的是
同一份表單，只有「賽事名稱」是必填，其餘留空就用預設值。存檔後那筆會當場出現在看板上，
不必等下一回合。

幾個要知道的事：

- **它存在 `data/custom.json`**，跟 `overrides.json` 一樣是「只有人會寫、爬蟲只讀」的檔案。
  這是刻意的：新增的比賽不會被下一回合的抓取蓋掉，因為爬蟲根本不寫那個檔案。
- **它的 id 是負整數**（`-1`、`-2`…），由看板配發。爬蟲的 topic id 永遠是正整數，所以兩邊
  不可能相撞。號碼不會重用，刪掉再新增不會踩到舊的草稿檔。
- **它一直留著，只能手動刪除。** 爬蟲抓來的賽事會在掉出論壇列表 45 天後淡出看板，
  手動新增的不受這條限制 —— 它沒有「最後出現在列表上」這種時間，而且你要的是它留著。
- **刪除要按兩次。** 第一次只是「上膛」（按鈕會變成「確定刪除（無法復原）」並在旁邊寫清楚
  會發生什麼事），第二次才真的把 key 從 `custom.json` 移除。這裡刻意不用 `confirm()` ——
  那是跟整頁無關的系統彈窗，手機上很容易誤觸。
- **刪掉之後，`drafts/{id}.md` 會留著。** 那份草稿可能已經貼出去了，刪掉就查不到自己發過
  什麼。`drafts/README.md` 下次重繪時會自動不再列出它，孤兒 `.md` 要自己刪。
- **沒有論壇原帖，所以「資訊連結」是你自己填的。** 沒填的話卡片上不會出現那一行 ——
  寧可沒有連結，也不要一個連過去是 404 的（負 id 拼出來的論壇網址看起來完全正常）。
- **`custom.json` 壞掉的話排程會變紅、整個停住**（不寫檔、不提交），跟 `overrides.json`
  完全同一個理由：當成空檔繼續跑，手動新增的比賽會整批從看板上消失，而且因為
  `dashboard_sha` 跟著變了，這個「消失」還會被當成一次正常更新提交出去。
- **單筆形狀不對只會濾掉那一筆**（例如整筆不小心寫成字串），其餘照常顯示。只有 JSON 本身
  壞掉、或最外層不是物件，才會中止整個排程。

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

工作人員帖的措辭不一樣：標題的狀態詞換成「工作人員招募中」，內文那一行是
`🙋 招募｜…`。同時徵選手與工作人員的（`kind == both`）本體是比賽，措辭照選手走，
另外加一行 `🙋 招募｜同時徵求工作人員`。

手動新增的比賽也有草稿（`drafts/{負id}.md`），網址用你填的「資訊連結」，沒填就整行不寫。

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
- **分頁歸屬是啟發式。** `detect_kind()` 只看標題，而且要求「工作人員」與「選手／隊伍」
  字樣**同時**出現才算 `both`。所以「比賽順便徵人」依預設算選手賽事 —— 不同意就在編輯模式
  改「分頁」那一格。
- **首帖節錄是後加的欄位**，所以在這版上線前就已抓過明細的賽事，點開時只會少那一段，
  其餘欄位照常。想立刻補齊就跑一次 `python -m osu_tourney.scrape --no-push --refetch-details`
  （見下方說明），否則要等該賽事的標題變成「報名中」且過了明細更新週期才會自己補上。
- 看板只顯示收錄與待確認的賽事；被排除的仍保存在 `data/tournaments.json` 裡，不會刪除。
  手動新增的比賽保存在 `data/custom.json`，只有把 key 刪掉才會消失。
