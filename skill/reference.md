# TSUBAME 4.0 の参照資料

[SKILL.md](SKILL.md) から引く。値は 2026 年 10 月時点(公式: https://www.t4.cii.isct.ac.jp/docs/handbook.ja/ )。

## 資源の種類と課金

| 資源 | CPU コア | メモリ | GPU | 係数 | 向いている使い方 |
|---|---|---|---|---|---|
| node_f | 192 | 768GB | H100×4 | 1.00 | 4 GPU の並列学習(DDP) |
| node_h | 96 | 384GB | H100×2 | 0.50 | 2 GPU |
| node_q | 48 | 192GB | H100×1 | 0.25 | 1 GPU の本番・デバッグ |
| gpu_1 | 8 | 96GB | H100×1 | 0.20 | GPU は要るが CPU はほぼ要らない作業(環境づくりなど) |
| node_o | 24 | 96GB | H100 の 1/2(MIG) | 0.125 | 軽い GPU の検証 |
| gpu_h | 4 | 48GB | H100 の 1/2(MIG) | 0.10 | いちばん安い GPU |
| cpu_160 | 160 | 約 640GB | なし | 0.60 | CPU の大きな並列 |
| cpu_80 | 80 | 約 320GB | なし | 0.30 | |
| cpu_40 | 40 | 約 160GB | なし | 0.15 | |
| cpu_16 | 16 | 約 64GB | なし | 0.06 | |
| cpu_8 | 8 | 約 32GB | なし | 0.03 | |
| cpu_4 | 4 | 約 16GB | なし | 0.015 | 小さな CPU の計算・動作確認 |

- 消費ポイント = ノード数 × 係数 × (0.7 × 実際の実行秒 + 0.1 × 指定した h_rt の秒) / 3600。5 分未満でも 5 分ぶん(公式の課金表: https://www.t4.cii.isct.ac.jp/tsubame-kyodo/tariff2025 )。
- h_rt を長く指定するだけで 0.1 × 指定時間ぶんがかかる。見積もった実行時間の 1.3〜1.5 倍にする。短すぎるとジョブが途中で切られる。
- h_rt いっぱいまで走ったときの目安は「係数 × 0.8 pt / ノード時」: node_f 0.8、node_q 0.2、gpu_1 0.16、cpu_16 0.048 pt/時。
- MIG(node_o・gpu_h)は GPU 間通信(NCCL)ができないので DDP には使えない。
- node_f は空きが少なく、待ちが長くなりがち。急ぐなら node_h や node_q に落とす。
- 同時に実行できるのは 1 人あたり平日 30 ジョブ/6144 スロット、週末 100 ジョブ/12288 スロットまで。短いジョブを大量に投げるならアレイジョブにまとめる: `hpcjob submit x.sh -r "-l cpu_4=1 -t 1-100" -t 0:30:00`(`-r` が `-` で始まるとそのまま qsub に渡る。中の `-t 1-100` が qsub のアレイ指定で、外の `-t` は h_rt)。ジョブの中では `$SGE_TASK_ID` が 1〜100 になる。
- 予約実行(AR)は割高なので使わない。

## ジョブのスクリプト

資源と時間は `hpcjob submit -r -t` で渡すので、スクリプトには書かない。ログは置き場所の `auto/logs/<ジョブ名>.o<JOBID>` に出る。

```sh
#!/bin/sh
#$ -cwd
#$ -j y
set -eu
module purge
module load cuda/12.8.0          # GPU でコンパイラ(nvcc)が要るときだけ
PY={{WORK_REMOTE}}/envs/myenv/bin/python
echo "[job] host=$(hostname) start=$(date '+%F %T')"
"$PY" "$@"
echo "[job] done $(date '+%F %T')"
```

- `#$ -cwd` を書かないと、ジョブはホームで始まる。`-cwd` なら hpcjob の置き場所で始まる。
- module(2026-10 時点): `cuda/13.1.1`(既定)・`cuda/12.8.0` ほか、`miniconda/24.1.2`、`python/3.14.3`。今あるものは `hpcjob run -- "bash -lc 'module avail 2>&1'"`(ログインシェルでないと TSUBAME の module が見えない。一覧は標準エラーに出る)。
- GPU ドライバは CUDA 13 対応。PyPI の torch はそのまま動く。
- 計算ノードからインターネットに出られる(pip install はジョブの中でできる)。
- ジョブの中の一時ファイルは `$T4TMPDIR`(計算ノードのローカル SSD)に置く。`/tmp` 直下は使わない。
- 4 GPU の DDP: `torchrun --standalone --nproc_per_node=4 train.py`(node_f)。

### Python の環境を作るジョブ

環境はホームではなく work({{WORK_REMOTE}})に置く。conda と pip のキャッシュも work に逃がす。`-r gpu_1=1 -t 1:00:00` などで投げる。

```sh
#!/bin/sh
#$ -cwd
#$ -j y
set -eu
module purge
module load miniconda/24.1.2
eval "$(/apps/t4/rhel9/free/miniconda/24.1.2/bin/conda shell.bash hook)"
export CONDA_PKGS_DIRS={{WORK_REMOTE}}/conda/pkgs
export PIP_CACHE_DIR={{WORK_REMOTE}}/conda/pipcache
PREFIX={{WORK_REMOTE}}/envs/myenv
mkdir -p {{WORK_REMOTE}}/conda
conda create -y -p "$PREFIX" python=3.12
"$PREFIX/bin/pip" install --no-input numpy
"$PREFIX/bin/python" -c "import numpy; print('ENV_OK', numpy.__version__)"
```

普段のジョブでは activate せず、`$PREFIX/bin/python` のように環境の中の実行ファイルをフルパスで呼べばよい。

## 置き場所と容量

- ホームと work は合わせて 1 人 125 GiB(`hpcjob run -- t4-user-info disk home`)。
- グループのディスク: `/gs/fs/{{GROUP}}`(SSD、速い)と `/gs/bs/{{GROUP}}`(HDD、大容量)。容量はグループで買った分だけ(`hpcjob run -- t4-user-info disk group -g {{GROUP}}`)。大きなデータを作る前に空きを見る。
- 他のメンバーのフォルダは、初めは書き込めない。
- 大きな結果を `pull` する前に、大きさを確かめる(`hpcjob run -- du -sh <パス>`)。

## つまずく点

- ssh のたびに `please do: eval "$(... conda shell ...)"` と `WARNING: connection is not using a post-quantum key exchange algorithm` が出る。どちらも害はない。
- Windows で作ったスクリプトの改行(CRLF)が残ると、qsub が `JSV "ERROR"` で止まる。hpcjob の同期は直してくれる。手で scp したときは `sed -i 's/\r$//' <ファイル>`。
- `module load miniconda` だけでは conda コマンドが使えない。上の `eval "$(... conda shell.bash hook)"` が要る。環境の中の実行ファイルをフルパスで呼ぶなら不要。
- `hpcjob usage` の集計(qacct)に終わったジョブが載るまで数分かかる。
- `Permission denied (publickey)` で入れなくなったとき:
  - Windows: ssh-agent のサービスが止まっていないかを確かめる。PowerShell で `Get-Service ssh-agent` を見て、止まっていれば、このスキルのフォルダの `enable-ssh-agent.cmd` を本人にダブルクリックしてもらう(エクスプローラーで `explorer.exe /select,<そのパス>` と開いて見せる)。鍵が ssh-agent から消えていたら(`{{SSH_ADD}} -l` に出ない)、本人に PowerShell の窓で `{{SSH_ADD}} <鍵のパス>` を打ってパスフレーズを入れてもらう。
  - Mac: まず `ssh-add --apple-load-keychain`(キーチェーンに覚えたパスフレーズで読み込み直す。打つものはない)。それでも駄目なら、本人にターミナルで `ssh-add --apple-use-keychain ~/.ssh/<鍵>` を打ってパスフレーズを入れてもらう。
  - どちらでもなければ、ポータル(https://portal.t4.gsic.titech.ac.jp/ )の「SSH公開鍵管理」に鍵が残っているかを見る。
- アカウントは年度ごとに更新が要る。年度の初めにポータルで手続きしないと使えなくなる。
