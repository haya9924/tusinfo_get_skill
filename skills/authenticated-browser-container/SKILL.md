---
name: authenticated-browser-container
description: "Use when an agent must read a site the user is logged into (SSO/LMS), without holding credentials."
version: 1.0.0
platforms: [linux]
metadata:
  hermes:
    tags: [browser, cdp, docker, login, credentials, sso, agent-browser, chromedp]
---

# Authenticated browser container（ユーザーがログインし、エージェントが読む）

会員限定/学認 SSO 配下のサイトを、**ユーザーがログイン済みのブラウザ**から読むための構成。
エージェントは認証情報を持たない。専用コンテナで Chromium を動かし、ユーザーがその Web UI で
自分でサインインし、エージェントは CDP で読むだけにする。

分離: ログイン済みプロファイルはコンテナのボリュームにだけ存在し、通常のブラウザ利用
（使い捨てプロファイル）とは別。**認証情報がエージェントに渡ることはない。**

## セットアップ（linuxserver/chromium + socat CDP ブリッジ）

```bash
mkdir -p ~/chrome-auth/config
PW=$(python3 -c "import secrets;print(secrets.token_urlsafe(12))")

docker run -d --name chrome-auth --restart unless-stopped \
  --memory=2g \
  -p 3001:3001 \
  -p 127.0.0.1:9223:9223 \
  -e PUID=$(id -u) -e PGID=$(id -g) -e TZ=Asia/Tokyo \
  -e CUSTOM_USER=hermes -e PASSWORD="$PW" \
  -e CHROME_CLI="--remote-debugging-port=9222 --no-first-run --no-default-browser-check" \
  -v ~/chrome-auth/config:/config \
  --shm-size=1g \
  lscr.io/linuxserver/chromium:latest

# Chromium は DevTools を loopback にしか出さないので、netns の外へブリッジする
docker run -d --name chrome-auth-cdp --restart unless-stopped --memory=128m \
  --network container:chrome-auth \
  alpine/socat tcp-listen:9223,fork,reuseaddr tcp:127.0.0.1:9222
```

**ユーザーに何か言う前に検証する**:

```bash
curl -s http://127.0.0.1:9223/json/version   # webSocketDebuggerUrl が返ること
curl -sk -o /dev/null -w '%{http_code}\n' https://<host-lan-ip>:3001/   # 401 = basic auth 有効
```

ユーザーは `https://<host-lan-ip>:3001` を開き（自己署名証明書を許可、CUSTOM_USER/PASSWORD で
basic 認証）、その Chromium の中で**手でサインインする**。完了の連絡を待ってから読み取りを始める。

## アタッチ

- `browser.cdp_url: http://127.0.0.1:9223` を config.yaml に置く（**loopback のみ**。CDP を LAN に
  晒さない）。Hermes は 1 呼び出しごとに生 config を読むので再起動不要（次のブラウザ起動で効く）。
- 直接 CLI: `agent-browser --cdp 9223 open <url>` / `agent-browser --cdp 9223 snapshot -i -c -d 3`。
- 自前スクリプトからは CDP を直接叩くほうが軽い（このリポジトリの `scripts/tusinfo_common.py` の
  `CDP` クラスは一時タブを1枚作って閉じるだけの最小実装。依存は `websockets` のみ）。
- 使い捨てプロファイルに戻すときは `browser.cdp_url` を空にする。

**運用ルール**: `browser.cdp_url` は**既定で空**にしておき、必要なタスクの間だけ設定して、
終わったら戻す。設定したままだとエージェントの全ブラウジングがユーザーの認証済みプロファイル内で
走り、「分離されたログイン環境」でなくなる。切り替えは無料（次の起動で効く）。

## ログインを生かし続ける（監視）— 必須

linuxserver の chromium イメージはアプリを**監視しない**: `/config/.config/labwc/autostart` が
`wrapped-chromium ${CHROME_CLI}` を**一度だけ**実行する。Chromium が落ちる（クライアント切断・
クラッシュ・OOM）と DevTools エンドポイントが消え、エージェントはアタッチできなくなる。
`/config/.config/labwc/autostart`（ボリューム内＝再起動でも残る）を**監視ループ**に書き換える:
chromium をバックグラウンド起動 → 30秒ごとに `http://127.0.0.1:9222/json/version` をポーリング →
3回連続失敗で kill（ハング）→ 少し待って再起動。`curl` はイメージに入っていて、
`wrapped-chromium` は PATH 上にあり非特権ユーザー `abc` で動く。

SSO の cookie は**セッション cookie**のことが多く、ブラウザと一緒に消える。再ログインの確率を
下げるには `session.restore_on_startup = 1`（"Continue where you left off"）と対象サイトの URL を
`.../chromium/Default/Preferences` に設定する（**chromium を止めている間**にホストから編集し、
監視ループに起動させる）。それでも再起動後は再ログインが必要になり得るので、**勝手に
「セッションは生きている」と決めつけない**。

ホスト側にコンテナ/ブリッジ層の watchdog を置く（例 `~/chrome-auth/health.sh` + cron、`no_agent`）。
空 stdout なら何も送らないので健全時は静か。復旧は段階的に:
① socat サイドカーだけ再起動（ログイン維持）→ ② chromium を kill（監視ループが上げ直す）→
③ コンテナごと再作成。

## ログインを失わずに再起動する

- **このコンテナを `docker rm -f` しない。** 即時 SIGKILL では Chromium がセッション cookie を
  書き出す暇がなく、SSO ログインが消える。`docker stop -t 20`（graceful SIGTERM）→ `docker rm`。
- `session.restore_on_startup = 1` を設定していれば、graceful stop + restart で SSO セッション
  cookie は**実際に保持される**（検証済み）。設定が無いと同じ再起動でログアウトした。
- リソース: `--memory=2g` で動かす。アイドルは 0.4〜0.7GiB（タブ1枚あたり 100〜300MB、
  Selkies ~227MB、labwc ~130MB）。上限に当たると Docker がコンテナを再起動し、health ジョブが拾う。

## タブ規律（ハードルール）

タブ1枚 = 生きたレンダラプロセス ~100-300MB（2GB 上限のコンテナ）。よって:

- **サイトごとに1タブを再利用**。既に開いているサイトのために2枚目を開かない —
  まず `curl -s http://127.0.0.1:9223/json/list`（または `agent-browser --cdp 9223 tab list`）で確認する。
- **ユーザーのタブをナビゲートせずに検証する**: 既存ページを `js(...)` で読む（`open` し直さない）。
- **作業後に片付ける**: `about:blank` と重複 URL のタブを閉じる（フォーカス中のものは閉じない）。
  枚数を `list` で確認する。
- 作業セットは使用中のサイト（通常1枚）だけ。公開サイトが必要なら、このコンテナにタブを足すより
  `browser.cdp_url` を空にして使い捨てプロファイルを使うほうが安い。

## ログインを「できた」と報告する前に

- `curl -s http://127.0.0.1:9223/json/list` で開いているタブ（title + URL）を見る — 安価で
  読み取り専用、認証済み表示かどうかが分かる。
- ユーザーのタブを触らずに調べる: 自分のタブを開く（`agent-browser --cdp <port> tab new <url>`）。
- **ログイン済みの印**はページ本文より確実: Moodle 系 LMS ならユーザーメニュー/アカウント
  アバター/通知ドロワー、ファイル一覧画面なら一覧の描画。「ログイン」リンクが無いことは
  証拠にならない（セレクタがログアウトリンクに当たることがある）。
- `browser.cdp_url` を設定した状態の `browser_exec` は**ユーザーのブラウザを操作する**。
  狙った `js(...)` 抽出（`document.querySelectorAll(...).slice(0,8).map(...)`）でトークンを節約し、
  スナップショットを丸ごと出さない。

## 落とし穴

- **背面タブを読むと古いデータを静かに返すことがある。** Chromium は隠れたタブを throttle し、
  SPA の同期ループ（チャット・ダッシュボード等）は更新を止める。DOM は最後に描画した一覧の
  ままなので、スクレイパーは延々と「新着なし」を報告する。抽出前に `Target.activateTarget`
  （または自分の一時タブ）を使い、**変わっていないページを「新鮮な読み取り」とみなさない**。
- **ページ内セッション失効はリダイレクトしない。** 一部の SPA はバナー
  （「セッションの有効期限が切れています」＋サインインボタン）を出し、古い一覧を DOM に残す。
  URL だけ見る失効検出は外れる。バナー文言**と**ページ内サインインボタンの両方で判定し、
  まず `Page.reload` を試す（サイレント更新で直ることが多い）。駄目なときだけユーザーに依頼。
- **Chromium 136 以降は `--remote-debugging-address` を無視する。** DevTools はコンテナ内の
  127.0.0.1 にバインドするので、`-p host:9222:9222` では繋がらない（docker-proxy はコンテナ IP に
  向かう）。非デフォルトの `--user-data-dir` でも loopback バインドは解除されない。上の socat
  サイドカーを使い、公開するのはブリッジポートだけ、しかも 127.0.0.1 に。
- **linuxserver イメージの `CHROME_CLI` に自前の `--user-data-dir` を足さない。** ラッパーが既に
  渡している。重複すると Chromium が `DevToolsActivePort` も DevTools リスナーも無い状態で起動する
  （実測: 起動はするがアタッチ不能）。イメージ既定（`/config/.config/chromium`）を保つ — それは
  すでにボリューム内に永続化される。
- linuxserver の Web UI は CUSTOM_USER/PASSWORD を設定しないと**認証なし**で、しかも
  **パスワードなし sudo のターミナル**を含む。LAN / Tailscale のみに置き、ポートフォワードしない。
- 実プロファイル閲覧（`browser.use_real_profile`）はここでは筋が悪い: **ホストの**既定 Chrome
  プロファイルを丸ごとスナップショットする（しかもホストに Chromium 系が必要）。コンテナ方式のほうが
  狭く、Chrome の無いヘッドレスホストでも動く。

## 運用ルール（自律ログインしない）

- `browser.cdp_url` は**既定で空**。必要なタスクの間だけ設定し、終わったら戻す。
- エージェントによるセッション後始末が `agent-browser --cdp <url> close` を発行し得る
  （実運用では外部ブラウザは何度も往復しても生き残った）。ただし決めつけない — autostart の
  監視ループを残し、**データを最新と報告する前にログインを再確認**する。
- **ユーザーの認証情報を保存しない・入力しない。** 途中でログイン画面や 2FA が出たら止めて、
  Web UI でサインインし直すようユーザーに依頼する。
- セッションは**読み取り主体**として扱う。サイトが表示していることを報告する。アカウント操作
  （投稿・購入・設定変更）は、その都度の明示同意なしに行わない。
