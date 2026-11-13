---
name: university-portal-extraction
description: "Use when reading TUS LETUS/CLASS (締切・休講・時間割・資料) with a signed-in browser."
version: 1.0.0
platforms: [linux]
metadata:
  hermes:
    tags: [letus, moodle, class, uprx, tus, deadlines, portal, browser, cdp]
---

# University portal extraction (TUS LETUS / CLASS)

東京理科大学 (TUS) の学生ポータルを読むための手順。対象:

| 系 | 実体 | 何が取れるか |
|---|---|---|
| **LETUS** | Moodle (`https://letus.ed.tus.ac.jp/`) | コース、セクション（回・実施形態）、課題の締切/提出状況、資料PDF、アナウンス |
| **CLASS** | uprx JSF/PrimeFaces (`https://class.admin.tus.ac.jp/`) | 掲示板（休講・補講・教室変更）、学生時間割表、学生出欠状況確認、シラバス |

**この2つ以外のサービスは対象外**。別途の手順が無い限り触らない。

**前提**: ログイン専用ブラウザ（`skills/authenticated-browser-container`）が起動していて、
ユーザーが学認 SSO を済ませていること。認証情報はエージェントが持たない・入力しない。

**このスキル同梱の道具**（`scripts/`、共通設定は `config.example.json`）:

```bash
python scripts/tus_class_check.py board                    # 掲示板（時間割変更タブ）全件＋本文
python scripts/tus_class_check.py attendance --day 11/05    # 履修科目の全日程と `休`（休講）
python scripts/tus_class_check.py timetable                # 曜時限・科目コード・教室・担当
python scripts/tus_class_check.py day <科目コード> 11/05    # その科目がその日にあるか
python scripts/tus_letus_check.py courses                   # 履修コース（コード付き）
python scripts/tus_letus_check.py course <cid> --sections   # セクション見出し＝回・実施形態
python scripts/tus_letus_check.py assign <activity_id>      # 締切・提出状況
python scripts/tus_assignments.py --sync / --brief / --list # 課題DB（SQLite）
python scripts/tus_download.py letus mod/resource/view.php?id=N
```

どれも標準ライブラリ＋`websockets` だけで動く（外部サービスとの連携はしない）。

## 0. 常時ルール

- **LETUS の課題ページ（mod_assign 等）由来**の提出物は課題DB（`tus_assignments.py --sync`）が持つ。
- **LETUS の課題ページに無いもの**（`mod/url` 経由の提出、持ち物、掲示で連絡された手続き）は
  課題DBに載らない。DB に0件＝宿題なし、と結論しない。コース頁とセクション説明まで読んで答える。
- 事実は**出所**（コース頁 / 課題頁 / どの PDF / 掲示板）を添える。推測で休講・締切・教室を作らない。
- 確認できないものは「未確認」と書く。**日時は絶対時刻＋曜日**で書く。

## 1. LETUS のコースを引く

```js
// https://letus.ed.tus.ac.jp/my/ — href で dedupe
Array.from(document.querySelectorAll('a[href*="course/view.php"]'))
  .map(a => ({n: a.innerText.trim().replace(/\s+/g,' '), h: a.getAttribute('href')}))
```

ダッシュボードは数十件のリンクを返す（履修コース＋ポータル/事務系）。**コース名末尾の括弧内が科目コード**
（`科目名 (99AB123)`）。`id=` の値が以降の全 URL に必要。コードを持たないものが事務系ポータル。

- **同名科目が前期・後期の両方に残る**（コード末尾で期を判別する）。名前の一致で選ぶと前期のページを
  読んで「もう終わった授業」を答える事故になる。**必ずコード一致**で絞る。
- 科目コードは3システムの結合キー: CLASS 掲示件名・学生時間割表・LETUS コース名に同じコードが出る。

## 2. コースを読む（「その日は何をするか／実施形態」）

```js
// /course/view.php?id=<cid>
document.querySelectorAll('li.section').forEach(sec => {
  const h = sec.querySelector('.sectionname, h3, h4');
  sec.querySelectorAll('li.activity').forEach(a => a.querySelector('a.aalink, a'));
});
```

- 実験/演習系は1セクション=1回で、**セクション名に日付が入る**（`…（9/30）`、
  `…（X班：11/11、Y班：11/18）`）。セクション一覧だけで「その日何をするか」が分かる。
  `li.activity` の innerText には各ファイルのアップロード/修正日が入るので、今年度の資料と
  過去版を切り分けられる。
- **「明日の授業は非同期／録画配信／休講か」と聞かれたら、まず全 `li.section` の innerText を読む。**
  実施方法はここにしか無いことが多い: セクション名自体が形式を持つ（`第3回（録画配信）`）うえ、
  **先頭の「一般」セクション**に担当者からの運用連絡が入る（「10/7 の講義はオンライン非同期です。
  教室での講義はありません。下記の課題を提出して下さい。」など）。
- アナウンスメント／フォーラムが「まだ新しいアナウンスメントは投稿されていません」でも
  **「変更なし」と結論してはいけない**（空のフォーラムは何の証拠でもない）。次の4つを見て、
  **出所ごとに「どこにあったか・どこには無かったか」を分けて**返す:
  LETUS セクション説明文 / LETUS アナウンスメント / CLASS 時間割変更 / CLASS 学生出欠状況確認の日付列。
- **録画の実体 URL は解禁時刻前には取れない**: 録画は `mod_url` で、利用可能時刻前は
  `mod/url/view.php` が `course/view.php` にリダイレクトし「…より利用可能」のトーストが出るだけ。
  URL を追いかけず、解禁時刻（多くは5限開始=16:20）を伝える。
- `mod/url` は**中継頁形式**（「リソースを開くには〜をクリック」）。リダイレクトを待たず、その頁の
  letus 域外 `a[href]` を拾う。
- **一般セクションの新旧判定は URL 内の activity id**（`view.php?id=N` の N が大きい順＝新着）。
  フォーラム/URL 活動には日付が無く担当が手動で並べるので、DOM 順は時系列ではない。

## 3. 締切と提出状態

`/mod/assign/view.php?id=<aid>`:

- `[data-region="activity-dates"]` → `開始予定 / 期限`
- `.submissionstatustable` → 提出ステータス・評定・残り時間
- 提出ボタン（`提出をアップロードする` 等）が残っていれば**まだ提出できる**

出席チェックは通常の課題で、開始時刻に開く**約20分の窓**。`開始予定` 前は提出コントロールが
出ないのが仕様（壊れているわけではない）。未提出＋締切時刻はレコード中もっとも行動につながる事実
なので、それを先頭に出す。

## 4. CLASS — 休講・補講・時間割変更の一次情報

休講/補講/教室変更の公式情報は LETUS ではなく **CLASS**。同じ学認セッションで入れる。

**入り方**: `https://class.admin.tus.ac.jp/` に goto → `ENTER`（PC）を click → ナビ
（`学籍情報` 等）が出るまで待つ。**URL 直打ちは `chrome-error` になることがある**ので必ずトップから。
ポータル頁のリンクを JS `.click()` しても遷移しない（JSF の onclick が `return false`）ので、
認証はトップからの ENTER で通す。

| 欲しいもの | 取り方 |
|---|---|
| 休講/補講/教室変更 | ナビの**「掲示板」**を click → `#funcForm:tabArea` の「時間割変更」タブ。パネル内 `dl.keiji` を列挙（`span.keijiCategory`=カテゴリ / `a.ui-commandlink`=件名 / 末尾テキスト=掲示日） |
| 掲示の本文 | 件名リンクを click → ダイアログ `#bsd00702:dialog` の innerText を `本文`〜`掲示期間` で切る（「補講日：オンデマンド形式にて実施」等の実施方法がここにしか無い） |
| 履修科目の正式名・科目コード・教室 | 左メニュー `a.ui-menuitem-link` で `学生時間割表` を click → `table.classTable` |
| 学期の全実施日・休講の有無 | 左メニュー `学生出欠状況確認` → **`#funcForm:btnHyoji`（表示）を click** → 履修科目ごとに1行 |
| 授業の実施形態・第N回の内容 | メニュー `シラバス照会` → `#funcForm:jugyoKamoku` に科目名の一部 → `#funcForm:search` |

### 掲示板の読み方（重要）

- 掲示板は**10個のタブパネルが同一ページに同居**し、非表示パネルにも `dl.keiji` が入っている。
  `document.querySelectorAll('dl.keiji')` を素で回すと「全表示」タブの一般掲示（奨学金・就職など
  100件超）が返り、休講を読んだつもりで無関係な掲示を報告してしまう。**タブの `aria-selected="true"`／
  可視パネル（`offsetParent !== null`）に絞る**。時間割変更タブ = `tabArea:3` 相当（id は自動生成で変わる）。
- パネル内はアコーディオン（`お知らせ(個人に対する)`/`授業・履修・試験`/`就職`/`奨学金`/…/`休講情報`）。
  カテゴリ単位で切るときは `.ui-accordion-header` の次要素から `dl.keiji` を拾う。
- 件名の形は `【休講】<区分> YYYY/MM/DD(曜) N限 <科目コード> <科目名> （<期曜限・担当>） <担当者>`。
- ダイアログ本文の照合は**空白を除去してから**比較（件名の空白有無が揃わない）。
- `#funcForm:tabArea` のようなコロン入り ID は `querySelector` で落ちる → `getElementById` か `\\:` エスケープ。

### 学生出欠状況確認（「その日は授業があるか」の最短の答え）

- 直 URL は `Bsa00101.xhtml` に落ちて**学期選択画面のまま**。メニューから入り、
  `開講年度学期`（`select#funcForm:kaikoNendoGakki_input`、値は `2026|02` 形式）を確認してから
  **`#funcForm:btnHyoji`（表示）を押す**。押さないと表が出ない（ここで空振りしやすい）。
- 履修科目ごとに1行: `火1 99AB123 科目名（…） 09/15 09/29 … 休 12/01 …`。
  **日付の並び順＝回数**（先頭＝第1回）、`休`＝休講の回。`〇` 付きは出席登録のあるコマ、
  `100％` は出席率（付かない科目は出席登録なし＝対面出席を取っていない）。
- **必ずセル単位で読む**: マークは日付と**同じセル**の先頭（`.syuketsuKbnMark`）に入る。
  行の `innerText` を丸ごと繋ぐと `11/24 休 12/01` のように**前の日付に付いて見える** —
  この誤読で「11/24 が休講」と逆に答える事故が起きる。`class="nolecture"` も併せて見る。
- **対象日が日付列にあって `休` が付いていなければ、掲示が1件も無くても「実施」と言い切れる。**
  逆に日付列に無ければ「その日はその科目の実施日ではない」。
- シラバス詳細には**日付が入っていない**。回数↔日付の対応はシラバスではなくこの日付列から取る。

### 時間割表

- 1セルに複数科目（複数回開講）が入るので `td.querySelectorAll('.jugyo-info')` で全部拾う。
- `td` の位置は**曜日ヘッダの中心 x 座標**と突き合わせる（先頭 td 以外を順番に曜日へ割り当てると1列ずれる）。
- セル内は `.fontB`=科目名、`\d{2}[A-Z0-9]{5}`=科目コード、`教室`=教室、残りが担当教員。
- この表を**教員→科目コードの対応表**として保存しておくと、担当者名からコマを引ける。

### シラバス

- 検索は `#funcForm:jugyoKamoku`（科目名の一部）で引く。**時間割コードを「科目ナンバリング」欄に入れると
  0件**になる（別体系。例 `L3FLCHL102`）— 「科目が無い」のではない。
- 結果一覧は**前期・後期の同名科目が混在**する。行内の「20XX年度 後期」と曜日時限で絞り、
  同じコードの行が1つだけになることを確認してから click。
- 検索欄は値を代入するだけでは送信に乗らないことがある → `input`/`change` を dispatch してから
  `#funcForm:search` を click。検索後・詳細 click 後は描画待ちに 6〜8 秒置く。

## 5. ファイルを落とす

```python
goto_url(f"{LETUS_BASE}/mod/resource/view.php?id={rid}"); wait_for_load()
href = js("location.href")            # .../pluginfile.php/.../<name>.pdf
assert "pluginfile.php" in href
b64 = js("""(async () => { const r = await fetch(location.href, {credentials:'same-origin'});
  const b = await r.arrayBuffer(); const u = new Uint8Array(b); let s = '';
  for (let i = 0; i < u.length; i += 0x8000) s += String.fromCharCode.apply(null, u.subarray(i, i + 0x8000));
  return btoa(s); })()""")
```

- **`fetch()` で資源頁を取って `pluginfile.php` を探す方法は効かない**（ビューアは JS が組む）。
  必ずナビゲートして `location.href` を見る。
- 保存名は ASCII の key、日本語の元名はログ行にだけ出す（`tus_download.py` がこの形）。

### PDF が読めない環境なら

まず `read_file(<path>)` — PDF のテキスト層をそのまま抽出する。0 チャンクや崩れが酷いときだけ venv:

```bash
uv venv /tmp/pdfenv --python 3.11
uv pip install --python /tmp/pdfenv/bin/python pypdf pdfplumber
```

**罫線テーブル（実験予定表など）は座標が要る**: `page.rects`=罫線／セル塗り、`page.chars`=文字の
x0/x1/top。`extract_text(extraction_mode="layout")` は長い行を折り返すので、位置が要るときは `chars` を見る。
0 文字のページはスキャン画像 — 「読めない」と結論せず、テキストを持つページを報告する。

## 6. 人数・履修者数を調べる

学生権限では LETUS の「参加者」一覧は**全コースで拒否**される（パーミッションなし）。

| 欲しい数 | 一次情報 | 取り方 |
|---|---|---|
| 学科の学年人数（正） | 新入生**英語クラス分け表**（工学部ポータル） | 学科ごとに**学籍番号の範囲**（例 `…001〜034 / 035〜069 / 070〜104`）→ 範囲の最大値 |
| 科目の対象者数 | CLASS `授業改善アンケート結果照会` | 年度学期を選び検索 → **対象者数 / 回答者数 / 回答率** |
| 実験クラスの人数 | LETUS の「班分け表」PDF | 学籍番号の列挙（学科を半数ずつ分割している場合は×2） |
| 開講学科・学年 | CLASS シラバス照会 | 詳細の「開講学科・専攻」 |

- 学年には**原級生**（前年度の学籍番号）が混ざるので「入学生N名」と「学年在籍 N+M 名」は別物。
- 複数ソースの突き合わせで確度を上げる（範囲最大値・対象者数・班分け表の人数×分割数）。

## 7. 返し方（期待される形）

- **提出物と締切が先**: 何をいつまでに出すか、もう出したか。次にセッション内容、準備物、その後の締切。
  前置きも「何を調べたか」のナレーションも書かない。
- ユーザー自身の specifics（班・科目コード・自分のコマ）を書く。「学生一般」の説明にしない。
- 事実は**出所つき**で（コース頁 / 課題頁 / どの PDF / 掲示板）、ラベルの付いた箇条書きで簡潔に。
- 質問に答える PDF は `~/documents/<コース>/` に ASCII 名でコピーして添付する（本文を貼らない）。
- 作業後は一時タブを閉じる（CDP の `Target.closeTarget` は `CDP` クラスが自動でやる）。手動で開いた
  タブは `tabs.sh trim` で整理する。
- **日時は絶対時刻＋曜日**で書く。

## 落とし穴（実測で踏んだもの）

- 掲示板の全パネル走査（上記）。`全N件` は**掲示板の全件数**なので「休講掲示が無い」根拠に使える。
- 出欠画面で `表示` を押し忘れると表が出ない。
- 出欠の `休` を行 innerText で読むと1つ前の日付に付く（セル単位で読む）。
- `学生出欠状況確認` / `学生時間割表` は**自分の分だけ**で人数は出ない。時間割セルの詳細ボタンは
  サーバエラーになることがある。
- **日付（`10/13` 等）で body 全文を走査すると、ヘッダの「前回ログイン：2026/10/13 08:33」が必ず
  最初にヒットする** — ログインバナーは掲示本文の証拠ではない。数える前に除外する。
- 監視系スクリプトは**正常時もエラー時も exit 0** で終わる設計にすると cron が静かに回る。だが
  `last_status: ok` は正常の証拠にならない（沈黙したまま壊れる）。ログと state を実際に読むこと。
