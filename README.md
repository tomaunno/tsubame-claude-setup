# TSUBAME を Claude Code から使えるようにする

東京科学大のスパコン **TSUBAME 4.0** を、あなたのパソコンの **Claude Code** から、あなたのアカウントで使えるようにするための準備一式です。準備が済めば、Claude に「この計算を TSUBAME で回して」と頼むだけで、計算を送る・終わるのを待つ・結果を取ってくる、までを Claude がやります。

プログラムの知識は要りません。コマンドは全部 Claude が打ちます。

## 用意するもの

- **TSUBAME のアカウント**(`ux12345` のようなログイン名)。まだ無ければ、大学のポータルから申し込みます(https://www.t4.cii.isct.ac.jp/getting-account )。
- **研究室の TSUBAME グループへの参加**。計算の料金(ポイント)はグループから払います。グループの管理者に招待してもらい、届いたメールのリンクで承諾してください。
- **Claude Code**。デスクトップアプリでもターミナル版でもかまいません(入れ方: https://code.claude.com/docs/en/quickstart )。ChatGPT を使っている人は、代わりに **Codex** でも進められます(下の「ChatGPT(Codex)を使っている人へ」)。
- Windows 10/11 か Mac。

## ChatGPT(Codex)を使っている人へ

ブラウザやスマホの ChatGPT(会話だけの画面)では、あなたのパソコンでコマンドを動かせないので、この準備はできません。ChatGPT のアカウントでそのまま使える **Codex**(パソコンで作業する版。アプリとターミナル版があります。入れ方: https://developers.openai.com/codex/quickstart )を入れて、下の「やり方」の Claude Code を Codex に置き換えて進めてください。送る文は同じです。

- Codex は手順書の中の「Codex で進めるとき」を読んで、Codex 用に読み替えて進めます。Claude のスキルの代わりに、Codex が読むスキル(`~/.agents/skills/tsubame/`)を書きます。
- Codex が「実行してよいか」「ネットワークを使ってよいか」「このフォルダの外を書き換えてよいか」と聞いてきたら、許可してください(TSUBAME への接続や、鍵・設定を書くために要ります)。
- Codex での通しの確認はまだしていません。うまくいかなかったら、どの手順で何が出たかを作った人に知らせてください。

## やり方

1. Claude Code を開きます。
2. 次の文をそのまま貼り付けて送ります。

   > https://github.com/tomaunno/tsubame-claude-setup をこのパソコンに持ってきて、中の SETUP.md の手順どおりに、私のアカウントで TSUBAME を使えるようにセットアップしてください。

3. あとは Claude の質問に答えるだけです。
   - 「実行してよいか」と聞かれたら「はい(Yes)」を押します。
   - 途中で 1 回、TSUBAME のサイト(ポータル)に大学の ID でログインしてもらいます。
   - 鍵にパスフレーズ(パスワード)を付けると決めた場合は、新しく開いた窓で、あなたが自分で打ちます。Claude には見えません。

かかる時間は 10〜20 分です。最後に小さな計算を 1 回だけ試しに送ります(0.01 ポイント未満)。

## 終わったら

Claude に頼むだけです。

- 「このフォルダの train.py を TSUBAME の GPU 1 枚で回して」
- 「TSUBAME のジョブはどうなった?」
- 「結果を取ってきて」
- 「研究室のポイントはあと何ポイント?」

途中で止まっても、もう一度同じ文を送れば続きから進みます。済んでいる所は自動で飛ばします。

## このパソコンに入るもの

| 入るもの | 場所 | 役割 |
|---|---|---|
| TSUBAME に入るための鍵と、接続の設定 | `~/.ssh/id_ed25519_tsubame`、`~/.ssh/config` の `Host tsubame` | あなたのアカウントで TSUBAME に入る |
| uv と hpcjob | `~/.local/bin/` | hpcjob が TSUBAME への送受信とジョブの操作をする([hpc-job-toolkit](https://github.com/tomaunno/hpc-job-toolkit))。uv はそれを Python ごと入れる |
| Claude のスキル | `~/.claude/skills/tsubame/`(Codex なら `~/.agents/skills/tsubame/`) | TSUBAME の使い方と、あなたの接続の値を Claude(か Codex)に教える |
| 確認なしで動かす許可(Claude Code で、選んだ場合だけ) | `~/.claude/settings.json` | hpcjob の送る・見る・取ってくる操作を、毎回聞かずに動かす |

準備のためだけに使った物は、最後に Claude が自動で片付けます。消すのは、持ってきたこのセットアップのフォルダ、TSUBAME に作った確認用のフォルダ、ダウンロードの残り、設定ファイルの控えです。

鍵の秘密の方(`.pub` でないファイル)は、このパソコンから外に出しません。人に渡したり、どこかに送ったりしないでください。

---

## 開発する人向け

`SETUP.md` は Claude が読む手順書で、`setup/t4setup.py` がその各手順の実体です(Python 3.11 以上、標準ライブラリだけで書いてあります)。手で動かすこともできます。

```
uv run --managed-python --python 3.12 setup/t4setup.py --help
uv run --managed-python --python 3.12 setup/t4setup.py check
```

自分の環境を壊さずに試すときは、最初の 1 回にだけ次のオプションを付けます。値は `setup/state.json` に残ります。

```
uv run --managed-python --python 3.12 setup/t4setup.py --alias tsubame-test \
    --key-name id_ed25519_tsubame_test --claude-dir <仮のフォルダ> \
    --toolkit <hpc-job-toolkit の手元のパス> check
```

手順 11 の片付けは、このフォルダごと消します。開発用の手元のコピーでは `cleanup` までにとどめ、フォルダを消すコマンドは打たないでください。

`skill/` の 3 つのファイルはスキルの雛形で、`{{GROUP}}` などは `install` が埋めます。MIT ライセンス。
