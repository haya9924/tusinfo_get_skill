# tusinfo_get_skill

東京理科大学 (TUS) の **LETUS / CLASS** を、ログイン済みブラウザの CDP 経由で
読み取るためのスキルとスクリプト一式。**他の bot / 別マシンで同じ機能をすぐ立ち上げ直せる**ことを
目的にしている。

- **対象は LETUS と CLASS のみ**（それ以外のサービスは一切扱わない）
- **読み取り専用**。書き込み・外部サービス連携は行わない
- 認証情報は一切扱わない（ユーザーが専用ブラウザで手でサインインし、エージェントは読むだけ）
- 個人情報（学籍番号・氏名・科目コード・自分のコマ）は**コードに埋めない**（`config.json` から読む）

## 中身

```
scripts/
  tusinfo_common.py     共通部品（設定読込 / CDP クライアント / 科目名マッチ / 休講判定）
  tus_class_check.py    CLASS: 掲示板(時間割変更) / 学生出欠状況確認 / 学生時間割表 / その日の判定
  tus_letus_check.py    LETUS: コース一覧 / セクション(回・実施形態) / アナウンス / 課題ページ
  tus_assignments.py    LETUS 課題を SQLite に貯める（cabetus の LetusParsing 移植）
  tus_download.py       LETUS のファイルをログイン済み Cookie で保存
skills/
  authenticated-browser-container/SKILL.md   ログイン専用ブラウザ（CDP）の立て方・運用・落とし穴
  university-portal-extraction/SKILL.md      ポータル抽出の手順（DOM セレクタ・判定ルール）
config.example.json    自分の値を入れる設定ファイルの雛形（全部省略可）
```

## 使い方（最短）

### 1. 依存

```bash
python3 -m venv ~/.venvs/tusinfo
~/.venvs/tusinfo/bin/pip install websockets
```

必要なのは `websockets` だけ（他は標準ライブラリ）。

### 2. ログイン専用ブラウザを立てる

`skills/authenticated-browser-container/SKILL.md` の手順（Chromium コンテナ + socat ブリッジ）。
立ち上げたら、ユーザーが `https://<host>:3001` で学認 SSO を済ませる。**エージェントは認証情報を
持たないし入力しない。**

```bash
curl -s http://127.0.0.1:9223/json/version    # DevTools が生きていること
```

### 3. 設定（任意）

```bash
mkdir -p ~/.config/tus-info
cp config.example.json ~/.config/tus-info/config.json
$EDITOR ~/.config/tus-info/config.json        # paths / ignore_patterns だけ
```

省略しても既定値で動く。`TUS_INFO_CONFIG=/path/to/config.json` で別の場所も指定できる。
環境変数 `TUS_CDP` で CDP URL を上書きできる。

### 4. 読む

```bash
python scripts/tus_class_check.py timetable
python scripts/tus_class_check.py board
python scripts/tus_class_check.py attendance --day 11/05
python scripts/tus_class_check.py day <科目コード> 11/05

python scripts/tus_letus_check.py courses
python scripts/tus_letus_check.py course <course_id> --sections
python scripts/tus_assignments.py --sync        # 初回は数分（全履修コースを回る）
python scripts/tus_assignments.py --brief --days 7
```

## 設計メモ（ハマりどころ）

- **CLASS の休講は掲示板の「時間割変更」タブにしか出ない**。ただし同一ページに10個のタブパネルが
  同居していて、非表示パネルにも同じ `dl.keiji` が入っている。可視パネルに絞らないと、奨学金や
  就職の一般掲示100件超を「休講」として読む。
- **「その日は授業があるか」の最短の答えは CLASS 学生出欠状況確認の日付列**。履修科目ごとに学期の
  全実施日が1行に並び、休講の回だけが `休` に置き換わる。日付列にあって `休` が無ければ、掲示が
  0件でも「実施」と言い切れる。マークは**日付と同じセル**に入るので、行の innerText を繋ぐと
  1つ前の日付に付いて見える（誤読の温床）。**メニューから入ったあと「表示」ボタンを押さないと
  表が出ない。**
- **科目コードが3システムの結合キー**（CLASS 掲示件名 / 時間割表 / LETUS コース名末尾）。名前の
  類似で突き合わせると別科目に誤爆する。
- **LETUS の参加者一覧は学生権限で拒否**される。人数は英語クラス分け表の学籍番号範囲や
  CLASS の授業改善アンケート（対象者数）で取る。
- **録画 URL は解禁時刻前には取れない**（`mod/url` がコース頁へリダイレクトするだけ）。
- **課題DB に入るのは LETUS の mod_assign 等だけ**。`mod/url` 経由の提出物・宿題は1件も載らない。
  DB に0件＝宿題なし、と結論しない。

詳細は `skills/university-portal-extraction/SKILL.md`。

## 個人情報を入れないための約束

このリポジトリには次のものを**コミットしない**（`.gitignore` で弾き、コードにも書かない）:

- 学籍番号・氏名・連絡先アドレス・アカウント/チャットの ID
- OAuth トークン・クライアントシークレット・API キー
- ブラウザのプロファイル / state / DB（`~/.config/tus-info/config.json` は各自の環境に置くだけ）
- 特定個人のコマ（時間割の内容）や、所属班・担当教員名を含むログ

スクリプトは**すべて設定ファイル経由**で個人固有値を受け取る。新しい値を足すときは
`tusinfo_common.cfg_get()` を使い、既定値には個人情報を書かないこと。

## ライセンス

MIT（`LICENSE`）。
