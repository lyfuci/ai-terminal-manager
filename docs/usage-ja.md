# atm — 使い方

[English](usage.md) | [中文](usage-cn.md) | **日本語**

[README](../README-ja.md) に戻る。全オプションと形式：[reference.md](reference.md)。

---

## 使い方

入れたら四つのキーだけ（`prefix` はデフォルト `Ctrl-b`）：

| キー | 動作 |
|---|---|
| `prefix + a` | **ポップアップ**：全履歴をあいまい検索 → 対象 pane を選ぶ → その pane で `--resume` |
| `prefix + A` | 同上、ただしカレントディレクトリ（サブディレクトリ含む）のセッションのみ |
| `prefix + b` | **サイドバー**：閉じていれば最左に全高の一列を開く；開いていればそこへ移動；すでに中なら閉じる |
| `prefix + B` | 現在の pane をバックグラウンドウィンドウ `bg` に退避——プロセスは動き続け、後でサイドバーから戻せる |

**ポップアップ内**：文字入力であいまい検索、`↑↓` / `^N` `^P` で移動、`Tab` で 全部 / Claude / Codex / Pi / Gemini / opencode を巡回、`⏎` で選択、`Esc` でキャンセル、`F1` / `?`（検索欄が空のとき）でキー一覧。
選択後は第二段階：全 pane（忙閑状態付き）+「新しく pane を分割」+「新しい window」+「表示のみ」。

**サイドバー内**：上半分は**実行中の pane**（選択 → `swap-pane` でメインへ、プロセスは継続）、下半分は**履歴**
（選択 → バックグラウンドの新 window で resume してから入れ替え）。`⏎` でメインへ、`^T` でどの pane に入れるか指定、`^X` で選択中を `bg` に退避、
`Tab` でソース切替、`^R` でインデックス再構築、`^C` で終了、`F1` / `?` でキー一覧。

**コマンドライン**からも使える（tmux 外では `pick` がコマンド表示に降格：`eval "$(atm pick --print)"`）：

```bash
atm list -n 20            # 最近 20 件；--source codex|claude|pi で一社のみ；--json で他のスクリプトへ
atm pick                  # 対話：セッション選択 → 対象 pane 選択 → 投入
atm resume <idの前方一致>  # TUI なしで id 指定で投入
atm panes                 # 全 tmux pane と忙閑状態
atm swap %7 --into %3     # %7 を %3 に入れ替え
atm park                  # 現在の pane を bg へ
atm prune -n              # bg の中の閉じられる idle shell を表示（-n を外すと実際に閉じる）
atm index --rebuild       # キャッシュを消して全再構築
atm restore               # 再起動のあと：前回のセッションを空の pane に戻す
atm update                # atm 自身を更新（uv tool / pipx / pip を判別）；--check は確認のみ。ミラーが遅れていれば PyPI 直結で再試行
```

> 投入時はデフォルトで cgroup のメモリゲートを被せ、値はマシンに応じて算出する（`memory.high` /
> `memory.max` は既定 `auto`：Max = 物理メモリの 35%・下限 4G、High = Max の 80%）。
> WSL のメモリ上限に当たったとき**tmux server がすべてのセッションごと死んだ**ことが一度あったため。
> 閾値の決め方と無効化は `docs/reference.md`「メモリゲート」。

**全オプション、実測性能、三種 JSONL のフォーマット詳細：[`docs/reference.md`](reference.md)。**
開発と貢献：[CONTRIBUTING.md](../CONTRIBUTING.md)。

---

## 再起動のあと：`atm restore`

tmux-resurrect が戻してくれるのは**骨組み**（window・pane・各 pane の作業ディレクトリ）だけで、
pane の中身は空の shell です。atm はあえて AI CLI を `@resurrect-processes` に**入れません**。
起動時に全部を一斉に立ち上げてしまうからで、実測ではセッション 4 本で RAM の 87% を食い、2 回フリーズしました。

`atm restore` はその pane を 1 本ずつ埋め戻します：

```bash
atm restore                  # いま入っている tmux セッション。計画を出して確認してから実行
atm restore -t work          # 別のセッション。`-t work:1` なら その window だけ
atm restore --all            # 保存ファイル内の全セッション
atm restore --print          # 計画を見るだけ
atm restore -y               # 確認なしで実行
atm restore --save-file PATH # last ではなく指定した保存から復元
```

計画には、**触らない**ものも含めて 1 件ずつ理由が出ます：

```
2 件のセッションを復元します：
  main:1.1  claude    インデックスのキャッシュを追加
  main:1.4  codex     リリーススクリプトの整理

次の 2 件はそのまま：
  main:1.2  * github  -- スキップ：その pane では既に何かが動いています
  main:2.1  古い作業  -- スキップ：その pane はレイアウトに存在しません
```

**埋め戻せる pane。** 保存されたコマンドラインがセッションを指しているもの全部です：atm 自身が投入する
`claude --resume <id>` も、手で打った `claude -r github`（短いフラグ + `/rename` や `claude -n` で付けたセッション名）も対象です。
名前はインデックスで引きます（同じ CLI・同じディレクトリの中だけ —— `claude -r` 自身の検索範囲と同じ）。
名前はコマンドラインの最後の 1 語である必要があります —— 後ろに別の語が続く場合
（保存ファイルでは引用符が失われ、名前の区切りが分からない）は「特定できない」としてスキップします。
同名が複数あれば atm は選びません：計画に候補を並べるので、`atm resume <id>` で正しいものを指定してください。
素の `claude` で起動した pane には再開する手がかりがないので空のまま —— 最新の保存にそういう AI pane があれば `atm doctor` が知らせます。

**`last` がもう上書きされていたら。** tmux が戻ったあとの次の自動保存で `last` は置き換わり、そのとき pane がまだ空の shell なら、
その保存にはセッションがありません。`atm restore` はそれに気づき、セッションを含む一番新しい過去の保存を示します：
`atm restore --save-file <そのファイル>`。

**何かが動いている pane は決して上書きされません** —— このコマンドが守る唯一の不変条件です。
投入は**直列**（3 つの CLI が同時に 20MB 超のトランスクリプトを読むのは実際にスパイクになります）で、
atm 通常の投入経路を通るので、どれも cgroup メモリゲートの下に入ります。

### 起動時に自動で走らせる

既定は無効です。有効にすると `atm install` が resurrect の post-restore フックにこのコマンドを掛けます：

```bash
atm config restore.on-boot true
atm install                  # フックを書き込む。次回 tmux server 起動時に有効
```

フックはファイル先頭の**独立した marker ブロック**に書かれ、永続化ブロックとは無関係です：

```tmux
# >>> atm restore (atm config restore.on-boot) >>>
set -g @resurrect-hook-post-restore-all '/path/to/atm restore --boot'
# <<< atm restore <<<
```

この独立性が要点です。永続化ブロックは tpm を自分で管理している場合まるごとスキップされ（atm は
あなたが書いたものに触りません）、フックは以前その中にあったため、そうしたマシンでは
`restore.on-boot = true` が何もしませんでした。今は tpm を誰が管理していても設定が必ず有効になります。
切り替えは実行中の server にも適用されるため、次回起動を待つ必要はありません。`atm doctor` は
フックが実際に設定されているかを引き続き検証します。

起動時の実行は、何かする前に 3 点を確認し、1 つでも駄目なら見送ります：

| 確認すること | 理由 |
|---|---|
| cgroup メモリゲートが使えるか | ゲートがなければ一括復元に安全網がない |
| 前回の起動時復元が最後まで走ったか | 途中で止まっていたら kill されたということ。「復元 → フリーズ → 再起動 → また復元」の循環を断つのはこの条件 |
| `MemAvailable` が `restore.min-available`（既定 `4G`）より上か | 1 件ごとに再確認するので、起動時復元は「メモリが厳しくなるまで埋める」に自然に劣化する |

起動時には端末がないため、何をしたかは `~/.local/state/atm/restore.log` に書き出され、
`atm doctor` にも現在の状態が 1 行出ます。前回が中断していた場合は、再有効化の手順もそのメッセージに出ます ——
手動で `atm restore` を実行して問題ないことを確認し、`~/.local/state/atm/boot-restore.json` を削除するだけです。

> atm は journal の OOM 記録を見ません。`adm` / `systemd-journal` グループに入っていなければ `journalctl` は
> **自分のログしか見えず**、カーネルの OOM 行は見えません。「常に異常なし」としか答えられない検査は、
> 検査がないより悪いからです。

---

## 日常の CLI 基礎

```bash
atm -v list                    # stderr に進行情報；-vv でファイル単位 / tmux コマンド単位（ATM_DEBUG=1 は -vv と同じ）
atm doctor --json              # 機械可読な健診レポート；設定ファイルが壊れているときだけ exit 1
atm config --json              # 各項目の値と由来：default / file / env
atm update --check --json
eval "$(atm completion bash)"  # 実際のパーサ定義から生成した補完（zsh、fish も）
NO_COLOR=1 atm pick            # https://no-color.org に従う
ATM_LANG=ja atm --help         # 表示言語：LC_ALL / LC_MESSAGES / LANG に従う（zh / ja、その他は英語）；ATM_LANG で強制
```

設定の優先順位：**コマンドライン引数 > 環境変数 > ファイル > デフォルト**。全キーに環境変数がある：
`memory.high` → `ATM_MEMORY_HIGH`、`memory.swap-max` → `ATM_MEMORY_SWAP_MAX` など。
未知のキーや壊れたファイルはエラーであり、黙って無視しない——制限が効いていると思い込むのを防ぐため。

`atm install` は最後の確認しか聞かない：調整できる値はすべて `atm config` にあり、install はそれを適用するだけ
（キーバインド、合計 slice、tmux オプション）。`--key s` などは先に config に保存してから入れるショートカット。
`atm install --conf PATH` / `atm uninstall --conf PATH` で `~/.tmux.conf` 以外の設定を対象にできる。
`eval "$(atm pick --print)"` は stdout が捕捉されていても動く：ピッカーは `/dev/tty` に描く。

## 設定の変更

設定編集ではファイルの値と明示的な変更だけを保存する。環境変数による上書きは一時的なままで、編集後も `← env` マーカーを維持する。`atm config --reset` は TOML を削除し、tmux ブロックと合計制限をデフォルトに合わせる。このリセットでは以前のインストール先を使い、実行時の環境変数の優先順位は変わらない。

`atm install --conf PATH` は絶対パスを `keys.conf-path` に記録する（TOML では `[keys]` の `conf_path`、空文字列は `~/.tmux.conf`）。以後の設定編集・インストール・アンインストールはこのパスを使い、明示的な `--conf` で上書きできる。リセットはパスの記録も消すため、その後のインストールやアンインストールでは再度 `--conf PATH` を渡す。パス設定の変更は以後の編集先を選ぶだけで、既存ブロックを移動しない。

`atm install --key s` と `atm config keys.pick s` はどちらも、新キーの書き込みと割り当てに成功してから、既存ブロックの不要なキーを解除する。書き込みや再割り当てに失敗した場合は旧バインドを残す。マーカーの欠落や入れ子があるファイルは変更を拒否する。

tmux オプションの有効化は引き続き即時適用する。無効化はファイルから atm の設定を除き、実行中の値は維持する。変更は新しい tmux server に適用され、ユーザー自身の設定に従って読み込まれる。合計 slice のインストールは `memory.user=true` のみ対応し、システムモード（`memory.user=false`）では理由を表示して拒否する。システムユニットは手動で設定すること。`daemon-reload` の失敗は「ファイルは書き込み済みだが、再読み込みに失敗」と原因を報告し、成功とは表示しない。

## メモリゲート：`atm claude` と `claude` の違い

```bash
atm config                     # 対話エディタ：↑↓ でキー選択、Enter で編集/切替、s で保存、? でヘルプ；右パネルが選択中の項目を表示言語で説明（形式 / デフォルト / 環境変数 / 由来）（atm config --show は表示のみ）
atm config memory.high 4G      # ソフト上限：スロットリング + 回収、殺さない。既定 auto = Max の 80%
atm config keys.pick s         # ピッカーキー（大文字 = 現在のディレクトリのみ）；keys.sidebar、keys.popup-width/-height も。保存で実行中の server に再割り当て
atm config tmux.mouse true     # tmux 共通オプション：mouse / focus-events / history-limit / base-index / renumber-windows → ~/.tmux.conf に独立ブロック、即時適用。あなた自身の行が同じオプションを設定していれば、黙って負けるのではなく行番号付きで報告する
atm config memory.slice-high 20G  # 合計 slice の数値（デフォルト auto = 物理メモリの 50% / 65%）；atm が書いたユニットを書き直し + daemon-reload
atm config memory.max 8G       # ハード上限：セッションの scope 全体（子プロセス含む）を kill。既定 auto = 物理メモリの 35%・下限 4G
atm claude --resume <id>       # その cgroup 内で claude を起動；引数はそのまま透過
claude                         # プレフィックスなし = ネイティブ、制限なし
```

`atm codex …` / `atm pi …` も同じ。`prefix + a` の投入とサイドバーの resume も同じ設定を使う。
`atm install` は合計用の `atm-ai.slice`（物理メモリの 50% / 65%）も書き、N 本合計でもマシンを落とさない；
`atm doctor` は両層を報告する。デフォルト値の根拠は [reference.md](reference.md#内存闸门默认开)。

## ペインが固まったら：`atm health`

コマンドによってはペインが固まったように見える——プロセスは生きているのに何も出ず、Ctrl-C も効かない。
atm はどのペインが何で詰まっているかを示す。使うのはカーネルがペインごとに持っている数値だけ
（tmux のペインはそれぞれ独立した systemd scope）：

- **サイドバー**：詰まったペインに赤い `⚠` を付ける——`⚠回収`（メモリのソフト上限に当たり続け、CPU が回収に消える）、
  `⚠D待ち`（プロセスが 2 回連続で割り込み不可スリープ）、`⚠上限超`（ソフト上限超過）、`⚠メモリ` / `⚠IO` / `⚠CPU`
  （PSI の待ち時間）。選ぶとフッターに理由が出る。詰まり**始めた**ときに tmux のステータス行で一度だけ知らせる。
- **統計**：サイドバーが詰まりごと（開始・終了・時間・原因）を `~/.local/state/atm/health.jsonl` に記録する。
  サイドバーを複数開いていても記録するのは 1 つだけ。

```bash
atm health            # いま詰まっているペイン + 直近 7 日のペインごとの合計
atm health --all      # 正常なペインも数値つきで表示
atm health --days 1   # 集計期間を変える；スクリプトには --json
```

`atm doctor` にも同じ項目がある。PSI だけでなく回収レートを見る理由：`MemoryHigh` で抑えられたプロセスは実測で
約 640 倍遅くなったのに、memory PSI は 1–2% しかなかった——待っているのではなく回収で忙しいから。
詳細は [reference.md](reference.md#格子健康哪格在卡2026-09-18)。

## 仕組み（三分版）

**「状態を覚える」は実は三層**。atm はそのうち二層に触れ、残りは tmux に任せる：

| 層 | 意味 | 担当 |
|---|---|---|
| **L1 見た目** | 分割レイアウト、各 pane の cwd、スクロールバック | tmux-resurrect（atm install が入れる） |
| **L2 プロセス** | UI を閉じても `claude` プロセスが動き続ける | tmux server 自体；atm のサイドバーはこの層で `swap-pane` する |
| **L3 セッション** | AI の会話コンテキスト | CLI 自身の `--resume`；atm のインデックス + ポップアップがそれを探し出して正しい pane に投げる |

> **L3 は L2 の代わりにならない**：`--resume` が戻すのは会話履歴で、途中まで走ったプロセスではない。サイドバーが存在する理由はここ。

**データの出所**：三つの CLI が自分で書くセッションファイルだけを、しかもファイル先頭だけを読む（タイトル / cwd / branch は先頭にあると実測）。
`(mtime_ns, size)` でキャッシュ——213 セッション 1.73 GB のコーパスでコールドスタート 198ms、ウォームスタート 5ms。
フォーマットは逆解析で得たもので公開契約ではないため、パースは終始防御的：汚い行が一つあってもリスト全体は落ちない。

**核心ジェスチャは一行**：

```
tmux send-keys -t %<pane-id> -l -- "cd <cwd> && claude --resume <sessionId>"
```

（`-l --` は必須：ないとコマンド内の `Enter` / `C-c` といった語が tmux に**キー名**として解釈される。）

---

## プロジェクトの現状

🟢 **ルート C を決定し実装済み**（2026-08-12）：範囲を「エージェント横断の統合履歴 → 指定 tmux pane に投入」に絞り、
その後 常駐サイドバー（09-02）、Pi 対応と永続化インストール（09-05）を追加。
Python 3.11+ ランタイム依存ゼロ、240+ テスト、MIT。

> アーキテクチャの分岐 A（tmux バックエンド + GUI）/ B（自作デーモン）は**否定されたのではなく、作っていないだけ**——
> 決定変数（別デバイスからの SSH 引き継ぎが必要か）はまだ未回答。作るときは `src/atm/index.py` の層をそのまま再利用できる。詳細は[研究記録](../research/README-ja.md)。

