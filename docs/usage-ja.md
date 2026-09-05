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

**ポップアップ内**：文字入力であいまい検索、`↑↓` / `^N` `^P` で移動、`Tab` で 全部 / Claude / Codex / Pi を巡回、`⏎` で選択、`Esc` でキャンセル。
選択後は第二段階：全 pane（忙閑状態付き）+「新しく pane を分割」+「新しい window」+「表示のみ」。

**サイドバー内**：上半分は**実行中の pane**（選択 → `swap-pane` でメインへ、プロセスは継続）、下半分は**履歴**
（選択 → バックグラウンドの新 window で resume してから入れ替え）。`⏎` でメインへ、`^T` でどの pane に入れるか指定、`^X` で選択中を `bg` に退避、
`Tab` でソース切替、`^R` でインデックス再構築、`^C` で終了。

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
atm update                # atm 自身を更新（uv tool / pipx / pip を判別）；--check は確認のみ
```

> 投入時はデフォルトで cgroup のメモリゲートを被せる（`MemoryHigh=2G` / `MemoryMax=4G`）。
> WSL のメモリ上限に当たったとき**tmux server がすべてのセッションごと死んだ**ことが一度あったため。
> 閾値の決め方と無効化は `docs/reference.md`「メモリゲート」。

**全オプション、実測性能、三種 JSONL のフォーマット詳細：[`docs/reference.md`](reference.md)。**
開発と貢献：[CONTRIBUTING.md](../CONTRIBUTING.md)。

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

`atm install --conf PATH` / `atm uninstall --conf PATH` で `~/.tmux.conf` 以外の設定を対象にできる。
`eval "$(atm pick --print)"` は stdout が捕捉されていても動く：ピッカーは `/dev/tty` に描く。

## メモリゲート：`atm claude` と `claude` の違い

```bash
atm config                     # 対話エディタ：↑↓ でキー選択、Enter で編集/切替、s で保存（atm config --show は表示のみ）
atm config memory.high 4G      # ソフト上限：スロットリング + 回収、殺さない
atm config memory.max 8G       # ハード上限：セッションの scope 全体（子プロセス含む）を kill
atm claude --resume <id>       # その cgroup 内で claude を起動；引数はそのまま透過
claude                         # プレフィックスなし = ネイティブ、制限なし
```

`atm codex …` / `atm pi …` も同じ。`prefix + a` の投入とサイドバーの resume も同じ設定を使う。
`atm install` は合計用の `atm-ai.slice`（物理メモリの 50% / 65%）も書き、N 本合計でもマシンを落とさない；
`atm doctor` は両層を報告する。デフォルト値の根拠は [reference.md](reference.md#内存闸门默认开)。

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

