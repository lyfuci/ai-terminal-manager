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

- **軽い。** Electron なし、常駐デーモンなし。`atm` はキーを押した瞬間にだけ走る（ウォームスタート 5ms、実測は[研究記録](research/README-ja.md)）。
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

> このリポジトリは同時に**研究記録**でもある。「使い方」は [docs/usage-ja.md](docs/usage-ja.md)、「なぜこう設計したか / 実測で踏んだ落とし穴」は
> [研究記録](research/README-ja.md) と `research/notes/`。覆された古い結論はすべて取り消し線で残し、消さない。

---

## インストール

### 要件

- Linux または WSL2 + **tmux ≥ 3.2**（`display-popup` は 3.2 から；開発基準 3.6、3.4 は実測で互換）
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

uv がなければ `pipx install ai-terminal-manager` でも同じ。更新は **`atm update`**——atm がどう入ったか（uv tool / pipx / pip、PyPI か git か）を判別して対応する更新コマンドを実行する；`atm update --check` は確認のみ。

### 健診・キーバインド・永続化

```bash
atm doctor      # データソースはあるか、tmux は通るか、何件見つかるか、自動保存フックが本当に入っているか
atm install     # ~/.tmux.conf にキーバインドを書き + resurrect/continuum を入れる。書く内容を先に表示して確認を取る；-y で確認省略。調整できる値はすべて `atm config` に
```

`atm install` は二つのことをし、それぞれをマーカーで囲んだブロックとして書く。変更前に自動バックアップ：

- **キーバインドブロック**：四つ——`prefix + a/A` ポップアップ、`prefix + b/B` サイドバー（詳細は [docs/usage-ja.md](docs/usage-ja.md)）。実行中の tmux server に即時反映。キーは変更可：`atm install --key s --sidebar-key g`。
- **永続化ブロック**：tpm 経由で **tmux-resurrect + tmux-continuum** を入れ（`~/.tmux/plugins/` に clone）、
  `@continuum-restore` を有効化、10 分ごとに自動保存。再起動後に session / window / pane / cwd が自動で戻る。
  claude / codex を再起動させることは意図的に**しない**——起動時に一斉に立ち上げるとメモリを一瞬で食い尽くす
  （`research/notes/2026-08-12-incident.md` 付録三）。セッションは対応する pane で必要なときに resume する。不要なら `--no-persist`。
  自分で tpm を管理している場合は自動でスキップし、二重に書かない。

表示言語はシステムの locale に従う（日 / 英 / 中）、`ATM_LANG=ja|en|zh` で強制可。

任意：`atm config memory.high 4G` を一度設定すれば、以後 `atm claude` / `atm codex` / `atm pi` は cgroup のメモリゲート内で起動する；
素の `claude` は制限なしのまま——プレフィックスが選択。詳細は [docs/usage-ja.md](docs/usage-ja.md)。

tmux が入っていなければ `atm install` がパッケージマネージャに応じたインストールコマンドを表示する。sudo は代わりに実行しない。
アンインストール：`atm uninstall && uv tool uninstall ai-terminal-manager`——この二つのブロックだけを消し、あなた自身の設定は一文字も触らず、clone したプラグインも残す。

---

## ドキュメント

- [使い方](docs/usage-ja.md) —— 四つのキー、ポップアップとサイドバー、コマンドライン、仕組み
- [リファレンス](docs/reference.md) —— 全オプション、実測性能、セッションファイルの形式、メモリゲート
- [研究記録](research/README-ja.md) —— なぜこう設計したか、何を計測したか、踏んだ落とし穴すべて（覆した結論は取り消し線で残す）
- [貢献](CONTRIBUTING.md)

## 貢献

issue / PR 歓迎。開発環境と規約は [CONTRIBUTING.md](CONTRIBUTING.md)；
セキュリティ問題は公開 issue にせず [SECURITY.md](SECURITY.md) を参照。ライセンス [MIT](LICENSE)。
