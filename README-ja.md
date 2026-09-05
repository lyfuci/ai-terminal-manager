# ai-terminal-manager (atm)

[English](README.md) | [中文](README-cn.md) | **日本語**

---

## なぜ作ったか

AI CLI はもう日常開発の大半を担えるほど強くなった。それを管理するツールも急増しているが、ほぼ全部が**デスクトップ GUI** だ。
問題は、かなりの人の開発がそもそもデスクトップ上で行われていないこと：

- コードはサーバーにあり、SSH で入って作業する。GUI は入れられないし、入れるべきでもない；
- 一人で Claude Code / Codex のセッションを 3〜4 本同時に開き、それぞれが別の tty。**どの会話がどのウィンドウにあるかは記憶頼み**；
- 回線が切れる、再起動する、別のマシンに移る——セッションが全部一斉に消え、ディスクに jsonl の山だけが残る。

この人たちに足りないのは、もう一つの GUI ではない。**tmux の中でのマルチセッション管理**だ。tmux は彼らがもともと開いているもので、
プロセスの常駐、再接続、別マシンからの引き継ぎ、レイアウトのシリアライズはすでに解決済み。欠けているのは「AI セッションを一級市民として扱う」層だけ。
atm はその層を補い、それ以外には触れない。

ついでに得られる二つの利点：

- **軽い。** Electron なし、常駐デーモンなし。`atm` はキーを押した瞬間にだけ走る（ウォームスタート 5ms、実測は後述）。
  サイドバーは普通の tmux pane の中の Python TUI。デスクトップ GUI と比べてどれだけ軽いかは**未計測**——使用感であって測った数字ではない。
- **tmux のセッション復元をそのまま使える。** tmux-resurrect / continuum が再起動後にウィンドウ・分割・ディレクトリを組み直す。
  対応する pane でキーを一つ押せば昨日の会話が resume される。状態永続化の仕組みを自前で再発明しなくていい。

## これは何で、何ではないか

**これは** AI CLI（Claude Code / Codex / Pi）向けの tmux セッションマネージャ。やることは三つ：

1. 三つの CLI それぞれの履歴セッションを**一つのリストに統合**し、あいまい検索して、選んだ一件を**指定した tmux pane に投げ込んで** `--resume` する；
2. 折りたためる**常駐の左サイドバー**。実行中の pane を一覧し、選ぶと `swap-pane` でメイン pane に入れ替わる。プロセスは止まらない；
3. ついでに tmux-resurrect + continuum を入れて設定し、再起動後に骨格が自動で戻るようにする。

**これは** GUI ではなく、レイアウト同期でもなく、コントロールモードのパーサでもない。それらは tmux エコシステムと公式 Desktop がすでに担っている
（調査は `research/notes/survey-existing-tools.md`）。セッションデータをネットワークに送ることも一切ない——ローカルファイルを読むだけ。

**向いている人**：Linux / WSL2 / サーバー上で tmux を使って開発し、AI セッションを複数同時に開いている人。
**向かない人**：tmux を使わない人；AI セッションが一本だけの人；フローティングウィンドウのような自由レイアウトが必要な人（tmux は二分割ツリー）。

> このリポジトリは同時に**研究記録**でもある。「使い方」が前半、「なぜこう設計したか / 実測で踏んだ落とし穴」が
> [後半](#以下は研究記録) と `research/notes/`。覆された古い結論はすべて取り消し線で残し、消さない。

---

## インストール

### 要件

- Linux または WSL2 + **tmux ≥ 3.0**（開発基準 3.6、3.4 は実測で互換）
- **Python ≥ 3.11**、ランタイム依存ゼロ
- Claude Code / Codex / Pi のうち少なくとも一つ（atm はそれらが `~/.claude/projects/`、`~/.codex/sessions/`、`~/.pi/agent/sessions/` に書くセッションファイルを読むだけ）

### 導入

[uv](https://docs.astral.sh/uv/) 推奨：

```bash
# PyPI から（コマンド名は変わらず atm）
uv tool install ai-terminal-manager

# または clone せずリポジトリから直接——常に最新の main
uv tool install git+https://github.com/lyfuci/ai-terminal-manager

# または clone してから（--editable を付けるとソース変更が即反映）
git clone https://github.com/lyfuci/ai-terminal-manager
uv tool install ./ai-terminal-manager
```

uv がなければ `pipx install ai-terminal-manager` でも同じ。更新は `uv tool upgrade ai-terminal-manager`（git 形式なら `--reinstall` を付けて再実行）。

### 健診・キーバインド・永続化

```bash
atm doctor      # データソースはあるか、tmux は通るか、何件見つかるか、自動保存フックが本当に入っているか
atm install     # ~/.tmux.conf にキーバインドを書き + resurrect/continuum を入れる。書く内容を先に表示して確認を取る；-y で確認省略
```

`atm install` は二つのことをし、それぞれをマーカーで囲んだブロックとして書く。変更前に自動バックアップ：

- **キーバインドブロック**：下の四つ。実行中の tmux server に即時反映。キーは変更可：`atm install --key s --sidebar-key g`。
- **永続化ブロック**：tpm 経由で **tmux-resurrect + tmux-continuum** を入れ（`~/.tmux/plugins/` に clone）、
  `@continuum-restore` を有効化、10 分ごとに自動保存。再起動後に session / window / pane / cwd が自動で戻る。
  claude / codex を再起動させることは意図的に**しない**——起動時に一斉に立ち上げるとメモリを一瞬で食い尽くす
  （`research/notes/2026-08-12-incident.md` 付録三）。セッションは対応する pane で必要なときに resume する。不要なら `--no-persist`。
  自分で tpm を管理している場合は自動でスキップし、二重に書かない。

tmux が入っていなければ `atm install` がパッケージマネージャに応じたインストールコマンドを表示する。sudo は代わりに実行しない。
アンインストール：`atm uninstall && uv tool uninstall ai-terminal-manager`——この二つのブロックだけを消し、あなた自身の設定は一文字も触らず、clone したプラグインも残す。

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
```

> 投入時はデフォルトで cgroup のメモリゲートを被せる（`MemoryHigh=2G` / `MemoryMax=4G`）。
> WSL のメモリ上限に当たったとき**tmux server がすべてのセッションごと死んだ**ことが一度あったため。
> 閾値の決め方と無効化は `docs/reference.md`「メモリゲート」。

**全オプション、実測性能、三種 JSONL のフォーマット詳細：[`docs/reference.md`](docs/reference.md)。**
開発と貢献：[CONTRIBUTING.md](CONTRIBUTING.md)。

---

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
> 決定変数（別デバイスからの SSH 引き継ぎが必要か）はまだ未回答。作るときは `src/atm/index.py` の層をそのまま再利用できる。詳細は下の研究記録。

---

# 以下は研究記録

## 起点（ユーザー本人の言葉）

> terminal 管理プログラムを作りたい。いま開発はほぼ claude code / codex でやっていて、どのエディタや IDE も重すぎる。
> 問題はウィンドウをたくさん開くと管理できないこと。だから比較的自由なレイアウトで、各コマンドラインの最終状態を覚えてくれるツールが欲しい；
> 左側に折りたためるウィンドウがあって、そこから最近の会話を指定した分割にすぐ開けるように。

## 鍵となる概念：「状態を覚える」の三層

| 層 | 意味 | 誰が提供できるか |
|---|---|---|
| **L1 見た目** | 分割レイアウト、各 pane の cwd、スクロールバックの文字 | 自分で JSON に保存、簡単；tmux-resurrect も提供 |
| **L2 プロセス** | UI を閉じても `claude` プロセスが動き続ける | **常駐プロセスのホストだけ**（tmux server か自作デーモン） |
| **L3 セッション** | AI の会話コンテキスト自体 | CLI 内蔵：`claude --resume` / `codex resume` |

> **L3 は L2 の代わりにならない**：`--resume` が戻すのは会話履歴で、途中まで走ったプロセスではない。
> 十分かかるリファクタが半分で UI が落ちたとき、L3 は要件を言い直す手間を省くだけで、すでに走った仕事は戻らない。

**L2 の正確な境界**（本機の実測構成で）：「UI を閉じた / 落ちた」「全ログインセッション終了」は生き残る（`KillUserProcesses=no` のため）。
**`wsl --shutdown` / Windows 再起動は生き残れない**——VM 全体が消え、L3 による縮退復旧しかない。

## 確認済み

| 論点 | 結論 |
|---|---|
| **L2 は必須か** | **必須**——長いタスクを常に掛けている。UI の寿命にプロセスを縛れない |
| **実行形態** | Windows ネイティブ GUI から WSL に接続（当時の選択） |
| **サイドバーのデータソース** | 既存のものがあり、自分で記録する必要なし。次節 |
| ~~**サイドバーを tmux pane にしない**~~ | ~~ネイティブ部品で作る——折りたたみがレイアウトツリーを乱すべきでない~~ **2026-09-02 に方針変更**：サイドバーは常駐の全高左 tmux pane（`prefix + b` で 開/移動/閉）で、**実行中**の pane を一覧し、選ぶと `swap-pane` でメインへ。閉じる = その pane を kill、メインが自動で埋まる。レイアウトツリーへの影響は幅だけ |

## サイドバーのデータソース（2026-08-12 の実装時に再実測、以下は修正版）

> ⚠️ 本節の以前の三つの記述は**実装時に実測で覆された**。原文は git 履歴に残し、ここには今成り立つものだけを書く。
> 詳細は `docs/reference.md`「データソース」節。

**Codex** —— 本当のデータソースは `~/.codex/sessions/<YYYY>/<MM>/<DD>/rollout-<ts>-<uuid>.jsonl`（86 件、125MB）。

- ❌ ~~`~/.codex/session_index.jsonl` は一行一件でタイトル付き~~ —— このファイルは **2026-08-03 で更新停止、5 件しか残っていない**。
  セッション一覧には使えない。ただしその数件の `thread_name` は最も質が高いので、タイトルの第一優先として残す。
- ❌ `~/.codex/thread_history_1.sqlite` はインデックスに見えるが、実測では**単一 thread の投影キャッシュ**（1 thread / 3 turn）で、グローバルなインデックスではない。
- ✅ 1 行目 `session_meta` に `session_id` / `cwd` / `git`（`git` は `null` のことがあると実測）。
- 復元：`codex resume <SESSION_ID>`（`codex resume --help` で確認）

**Claude Code** —— `~/.claude/projects/<cwd の '/' を '-' に置換>/<sessionId>.jsonl`、本機に 63 プロジェクトディレクトリ。

- ❌ ~~`type:"summary"` 行からタイトルを取る~~ —— 120 ファイルの末尾をサンプリング、**summary は一件もなし**。
- ⚠️ `type:"ai-title"` は確かに存在する（2.1.228 の新機能）が、カバー率はわずか **2%**（150 サンプル中 3 件）。
- ✅ **タイトルの本命は「最初の非 `isMeta` な user メッセージ」、カバー率 94%**；`cwd` / `gitBranch` も同じく 94%。
- ⚠️ **1459 個の jsonl のうち resume できるセッションは 127 個だけ**。残り 1332 個は
  `<sessionId>/{subagents,workflows,tool-results}/` 配下で、セッションの産物であり独立した sessionId を持たない。
  スキャンは一階層のみ。再帰にすると押しても何も起きない偽エントリが 1332 件増える。
- 復元：`claude --resume <sessionId>`

**Pi** —— `~/.pi/agent/sessions/--<cwd の '/' を '-' に置換>--/<ts>_<uuid>.jsonl`（schema v3）。
アダプタは上流の `session-format.md` に従って書いたもので、**本機に pi は未インストール、実データでの検証はしていない**。
`cwd` は 1 行目の SessionHeader にしかなく、表示名は独立した `session_info` レコードで何度も変更されうる（末尾をもう一度走査して最後のものを取る）。復元：`pi --session <id>`。

**三者ともインジェクションのラッパーを除外する必要がある**：Codex は実測で本当の質問の前に 10865 文字の
`<recommended_plugins>…</environment_context>` を差し込む。Claude は `<local-command-caveat>` / `<command-name>` の一群。
Pi は role の列挙が広く（`toolResult` / `bashExecution` / `compactionSummary`）、フィルタしないと bash の出力がタイトルに化ける。
「最初の user メッセージ」をそのままタイトルにすると画面一杯のゴミになる。

> ~~この部分はアイデア全体で最も作りやすく、最も差別化できる——「AI セッション履歴」を一級市民として扱う人は誰もいない。~~
> ❌ **2026-08-12 の調査で覆された**：clauhist、claude-sessions はすでに履歴閲覧 + resume をやっている。
> tmux-agent-sidebar / tmux-agent-status / opensessions はエージェントサイドバーを、公式 Desktop のサイドバーはネイティブ。
> `research/notes/survey-existing-tools.md` 参照。残った差別化点は一つだけ：**エージェント横断（Claude + Codex + Pi）の統合履歴 → 指定 pane へ投入**、
> そして GUI が使えないサーバー / SSH の人々に向けていること。

## ⚠️ 未決の分岐（次の議論はここから）

**決定変数は一つだけ：別の場所（純粋な SSH、スマホ、別マシン）から同じセッション群を引き継ぐ必要があるか？**

| | ルート A：tmux をバックエンドに | ルート B：自作の常駐デーモン |
|---|---|---|
| L2 | 無料 | 自分で実装（PTY はデーモンが持ち、GUI は attach するレンダラに過ぎない） |
| レイアウトの自由度 | **二分割ツリー**に制限、フローティング / 重なりは不可 | **完全に自由**、フローティングも重なりも可 |
| プロトコル | コントロールモード `tmux -CC`、プレーンテキストの行プロトコル、man page に定義 | 自分で決める、stdio / WebSocket 一本で十分 |
| 最も汚い仕事 | **レイアウトの真実が二つになり同期が必要**（tmux がレイアウトを持ち、そのツリーを鏡写しにする）——この種のプロジェクトでバグが最も密な場所 | この問題がない |
| パーサ | コントロールモードの状態機械 300〜500 行、煩雑 | 不要 |
| 再接続 / 別マシンから SSH で引き継ぎ | 無料 | 不可能 |
| 十数年の境界条件の磨き込み（resize 競合 / SIGWINCH / terminfo） | 無料 | 捨てる |

> **よくある誤解の訂正**：tmux を推すのは**作業量が少ないからではない**。むしろ逆で、前半の作業量は多い。
> 推す理由は一つだけ：L2 + 別デバイス引き継ぎは自作では買えない。別デバイス引き継ぎが不要なら、自作デーモンのほうが総複雑度は低くレイアウトも自由。

**さらにルート C（最終的に採用）**：まず app を書かない。tmux エコシステムがすでにレイアウトを食い尽くしているかもしれない——

| 能力 | 提供者 | どこまで |
|---|---|---|
| レイアウトのシリアライズ | ネイティブ `#{window_layout}` / `select-layout <文字列>` | **既存 pane** の再配置のみ、pane の再生成は不可 |
| 再起動後の session/window/pane/cwd 再構築 | tmux-resurrect | 構造とディレクトリを再構築。**プロセスを再起動するのであって、プロセス状態を復元するのではない**（つまり L1 + cwd、L2 ではない） |
| 定期自動保存 + 起動時自動復元 | tmux-continuum | 「手で保存しなくていい」を補う |
| プロジェクトテンプレート（プロジェクト X の標準レイアウトを開く） | tmuxinator / tmuxp（YAML 宣言） | 既製品 |

~~本機には**どれも入っていない**（`.tmux.conf` なし、tpm なし、tmuxinator なし）。~~
**古い（2026-08-12 当日に導入済み）**：`~/.tmux.conf` あり、resurrect + continuum 導入済みで動作を実測
（tmuxinator はまだ）。結論は**レイアウトの部分は tmux エコシステムで確かに十分**、ルート C 成立。
こうして構想全体は数百行の tmux サイドバーに縮んだ（`display-popup -E` のポップアップ、`prefix + a` で呼び出し、選んだら消える、**レイアウトを一切占有しない**）。
Windows GUI / コントロールモードパーサ / レイアウト同期はすべて蒸発。2026-09-05 以降は `atm install` が resurrect + continuum を直接入れる。

## 既知の落とし穴（検証済み——もう踏まない）

1. **コントロールモード `%output` のエスケープはトラフィックを膨らませる。** man page 原文：`value escapes non-printable characters and backslash as octal \xxx`。
   Claude Code は ANSI 再描画の重い TUI で、ESC（`\033`）自体が非印字文字——ほぼすべてのエスケープシーケンスが 4 バイトに膨れる。
   **実際にどれだけ膨れるかは未計測**。tmux ルートを行くなら最初の仕事はスループットのベンチ。耐えられなければ「pane ごとに独立した `tmux attach` パイプ一本」に退避。
2. **inotify イベントは 9p を越えて Windows 側の `\\wsl.localhost\` に届かない**——ファイル監視は WSL 内でやること。
3. **tmux にはまったく異なる二つのプロトコル層がある**。混同しないこと：
   - クライアント↔サーバーの `/tmp/tmux-<UID>/default` unix socket——**バイナリ、内部、未文書化、バージョン間で変わる、絶対に触らない**。
   - コントロールモード `-CC`——クライアントプロセスの stdin/stdout 経由、プレーンテキストの行プロトコル、公開インターフェース、iTerm2 が長年依存。
   - 正しい姿勢は**tmux クライアントをサブプロセスとして起動**（`spawn tmux -CC attach -t <session>`）し、バイナリプロトコルは代わりに話してもらうこと。
4. **tmux-continuum は自動保存を黙って無効化する——ロード時にマシン上に別の tmux server があるだけで。**
   ソース `continuum.tmux:main()`：

   ```bash
   if ! another_tmux_server_running; then
       add_resurrect_save_interpolation   # #(continuum_save.sh) を status-right に差し込む
   fi
   ```

   自動保存は**完全にステータスライン更新で駆動される**（`status-interval` 秒ごとにその `#()` が走る）。フックが入らなければ永久に保存されず、
   しかも**一切の表示がない**：`@continuum-restore` は `on` のまま、プラグインディレクトリもあり、すべて正常に見える。

   2026-08-12 に実際に踏んだ：実験で残った socket があり、メイン server がちょうどその後に再起動、
   **9 時間 40 分一度も保存されず**、手で `status-right` を確認するまで気づかなかった。

   自己診断（唯一信頼できる判定は**status-right にその `#()` があるか**で、`@continuum-*` オプションではない；`atm doctor` はまさにこれを見る）：

   ```bash
   tmux show-options -gv status-right | grep -q continuum_save.sh \
     && echo "自動保存 OK" || echo "❌ フック未導入、永久に保存されない"
   ls -lt ~/.local/share/tmux/resurrect/ | head -3      # 最新の保存は save-interval 以内であるべき
   ```

   修正：server が一つだけであることを確認し（`ls /tmp/tmux-$UID/`、迷子の socket を消す）、
   `tmux source-file ~/.tmux.conf`。再読み込みは「最後の保存時刻」を今にリセットするので、
   すぐに `~/.tmux/plugins/tmux-resurrect/scripts/save.sh` を一度実行しないと一 interval 分の空白ができる。

   **帰結：本プロジェクトで tmux の実験をするときは必ず `-L` の独立 socket を使い、その場で片付ける**——
   残った socket は汚いだけでなく、ユーザーの自動保存を黙って壊す。
5. **tmux-resurrect の空の保存ファイルは起動直後の server を殺す**（2026-09-05 実測）。restore は `last` の存在だけを見て非空かは見ない
   （`restore.sh:check_saved_session_exists`）。0 バイトの保存 → 「ゼロから復元」と判定 → `handle_session_0` が唯一の session 0 を kill
   → server はセッションがなくなり終了。空ファイルは server が死んだ後に `save.sh` が呼ばれることで生まれる（systemd ユニットの `ExecStop` など）。
   起動時に tmux を自動起動するユニットは `ExecStartPre` で空の `last` を最新の非空保存に向け直す必要がある。
6. **tmux 3.4 は `-F` フォーマット出力の制御文字を文字列 `\037` にエスケープして出力し、3.6 は生バイトを出す**（2026-09-05 実測）。
   `\x1f` をフィールド区切りにするパーサは 3.4 で「一行一フィールド」になり、「フォーマットが合わない行はスキップ」という防御が**全行**を黙って飲み込む。
   `tmux.py:_split_fields` は両方を認識するが、`\037` というこの一つの列だけを見て汎用の八進アンエスケープはしない（pane タイトルの `C:\123` を壊してはいけない）。
7. **tmux server の環境は起動した瞬間のスナップショット。** `run-shell` / `display-popup` 内の PATH は今のシェルの PATH ではない；
   `atm` は `~/.local/bin` にあり、server がその PATH 追加より前に起動していれば `run-shell 'atm …'` は 127 で、エラーは一行だけ。
   だから `atm install` が tmux.conf に書くのは常に絶対パス。
8. **シナリオにちょうど当たる二つの tmux 機能**：
   - `refresh-client -A %<pane>:off`——指定 pane の**出力読み取りを止める**。Claude Code を 6 本掛けて視界に 2 本しかないとき、残りは配信を切る。CPU を焼かない鍵となるスイッチ；`pause-after` と組み合わせれば自動で一時停止、再開は `%continue`。
   - `refresh-client -B <name>:<what>:<format>`——フォーマット文字列を購読し、変化時に `%subscription-changed` が push される。pane タイトル、活動の有無、いま走っているコマンドをすべて push で取れてポーリング不要——サイドバーの「この pane は忙しい」表示はこれに依存。

## 未確認

1. **別デバイス引き継ぎの要否** → ルート A / B を決める。**まだ未回答**だが、もうブロッカーではない：ルート C が使えるものを出荷済み。
2. ~~ルート C の検証を先にするか A/B に直行するか~~ → 決定：**ルート C**、実装済み（`src/atm/`）。
3. ~~サイドバーの形態~~ → **両方併存**（2026-09-02）：`display-popup -E` のポップアップが「履歴を探して一回投げる」（`prefix + a`）；
   **常駐の全高左 pane** が「実行中のプロセスを切り替える」（`prefix + b`、`atm sidebar`）。
   後者は新しい次元：atm はもともと L3（ディスク上の会話）にしか触れていなかったが、サイドバーは L2（tmux 内ですでに走っている pane）に触れ、
   核心ジェスチャが `send-keys` から `swap-pane` に変わる。tmux 3.6 で実測、`swap-pane` は window 越え / session 越えともに可。
   視界外のプロセスは `bg` window で動き続ける。エンドツーエンドは `research/experiments/2026-09-02-sidebar-swap/`。
4. ~~GUI を作る場合の技術スタック~~ → GUI は作らない。`src/atm/` は **Python 3.11+、ランタイム依存ゼロ**。
5. ~~「指定した分割に開く」の操作~~ → 決定：**セッション選択後に第二段階のピッカー**。
   全 pane（忙閑状態付き）+「新しく pane を分割」+「新しい window」+「表示のみ」を列挙。
   `display-panes` は使わない：tmux にフォーカスが必要で、ポップアップから呼ぶとジェスチャが途切れる。

**次にやるべきこと**（価値順）：

1. pi が入ったマシンで Pi アダプタを検証する（現状はドキュメントから書いたもの）。
2. セッションファイルの監視（inotify）→ インデックスの差分更新。落とし穴 #2 に注意：WSL 内でやること。
3. タイトルの質：94% が先頭メッセージの切り詰めで、長い質問は読みにくいタイトルになる。
   ローカルの小さなモデルでタイトルを補うか、Claude の `ai-title` カバー率が自然に上がるのを待つか。
4. CLI 横断のセッション引き継ぎ（Claude Code の会話を Codex で続ける、逆も）——コンテキストの持ち回り方がまだ見えていない。

## 本機の環境事実（2026-08-12 実測）

- `tmux 3.6`、`node v24.19.0`、`python 3.13.13`；zellij / wezterm は**なし**；`.tmux.conf` / tpm / tmuxinator / tmuxp なし。
- WSL2：`systemd=true`、`Linger=no`、ただし `KillUserProcesses=no`（デフォルト）→ ログアウトで tmux server が巻き添えにならない。
- `.wslconfig`：`memory=6GB`、`autoMemoryReclaim=gradual`；**`vmIdleTimeout` 未設定**、VM は 23h 連続稼働 → 日常では勝手に回収されない。
- Mirrored + hostAddressLoopback のため、**Windows↔WSL の TCP localhost は通る**
  （以前の「TCP ポートを使わず `wsl.exe --exec` + stdio JSON-RPC で」という助言はもう硬い制約ではないが、stdio のほうが依然楽：ポートなし、ファイアウォールのポップアップなし、認証なし）。

## 実測性能（`research/experiments/2026-08-12-index-bench/`）

| 指標 | 実測値 |
|---|---|
| コーパス | 213 セッション、1.73 GB（最大単一ファイル **680 MB**） |
| コールドスタート（全量パース） | **198ms** 中央値 |
| ウォームスタート（キャッシュヒット） | **5ms** 中央値 |
| コールドスタートの実読み取り量 | 47 MB / 1.73 GB = **2.7%** |

鍵は**ファイル先頭だけを読む**（タイトル/cwd/branch は先頭にあると実測）+ `(mtime_ns, size)` でのキャッシュ。

## ディレクトリ

`CLAUDE.md` 参照。実際のプロジェクトコードは `src/atm/`。

## ログ

- 2026-08-12 ディレクトリ作成；セッション `00000000-0000-4000-8000-000000000004` から要件とアーキテクチャ議論を抽出、`research/notes/2026-08-12-design-session.md`。
- 2026-08-12 既存ツールを調査、「誰もやっていない」という判断を覆す、`research/notes/survey-existing-tools.md`。
- 2026-08-12 **ルート C を決定し実装**：`src/atm/` の `atm`（当時 68 テスト通過）。
  実装中に本ファイルの二つの jsonl フォーマットに関する三つの記述を実測で覆した（上の「サイドバーのデータソース」節）。
  エンドツーエンド検証 `research/experiments/2026-08-12-tmux-e2e/`、ベンチ `research/experiments/2026-08-12-index-bench/`。
- 2026-09-02 **常駐サイドバー + swap 入れ替え**（ブランチ `sidebar-swap`）：`atm sidebar` が最左の全高 pane に常駐、
  上半分に実行中の pane（Claude Code が自分で pane タイトルを `✳ <タスク名>` にするのでセッション id の逆引き不要）、
  下半分に履歴；実行中を選ぶ → `swap-pane` でメインへ、履歴を選ぶ → 新 window で resume してから入れ替え。
  `prefix + b` で 開/移動/閉、`prefix + B` で現在の pane を `bg` へ。「サイドバーを tmux pane にしない」という古い結論を覆した（上記）。
- 2026-09-05 **実機で見つけた tmux 3.4 のバグ二つ**（PR #3 / #4）：バインドに裸の `atm` を書いて server 内で 127；`\x1f` 区切りが 3.4 でリテラルにエスケープされパースが全滅。
  **Pi セッションソース**（PR #5）：三つ目の CLI、アダプタは上流ドキュメントから、実機未検証；
  あわせて `cli.py` の「Claude でなければ Codex」というタグのバグを修正、ソースタグを `model.SOURCE_TAG` に集約してガードテストを追加。
  **`atm install` が resurrect + continuum を入れる**（PR #6）、そしてこの README にポジショニングを明記：サーバー / SSH 上のマルチセッション管理。

## 貢献

issue / PR 歓迎。開発環境と規約は [CONTRIBUTING.md](CONTRIBUTING.md)；
セキュリティ問題は公開 issue にせず [SECURITY.md](SECURITY.md) を参照。ライセンス [MIT](LICENSE)。
