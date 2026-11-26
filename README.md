# Download Sorter

Windows の「ダウンロード」フォルダを、ルールに従って自動で振り分けるスクリプトです。
論文の PDF は DOI から著者・年・タイトルを調べて名前を付け、**論文ごとのフォルダ**に本文・翻訳版・補足資料をまとめます。

## できること

- **ルールで振り分け**：`rules.json` の正規表現（ファイル名）に最初に当てはまったルールの移動先へ移します。どのルールにも当てはまらないファイルは動かしません。
- **論文の名前変更とフォルダ作成**：PDF の中の DOI（見つからなければファイル名やタイトル）から [Crossref](https://www.crossref.org/) で書誌情報を取得し、次の形にします。
  ```
  Papers\Abramson et al. 2024 - Accurate structure prediction of biomolecular\
      Abramson et al. 2024 - Accurate structure prediction of biomolecular.pdf
      Abramson et al. 2024 - Accurate structure prediction of biomolecular_翻訳.pdf
      41586_2024_7487_MOESM1_ESM.pdf
  ```
  - 翻訳版（`translated_` などで始まるもの）と補足資料（`MOESM`、`_suppl_`、`-sup-0001` など）は、対応する論文のフォルダに入ります。
  - 対応する論文がまだない翻訳版・補足資料は「未特定」フォルダに置かれ（サンプル設定では `Papers\_Unmatched`）、あとで論文が来たときに自動で照合し直されます。
  - ファイル名が論文らしくない PDF は、同じ DOI が 2 回以上出てくる場合だけ論文と判定します（参考文献の DOI による誤判定を防ぐため）。
- **解凍済み zip の整理**：zip の中のファイルが、名前もサイズも同じまま 9 割以上どこかのフォルダ（ダウンロードまたはドキュメント以下）に見つかれば、解凍済みとみなして 削除候補フォルダ（サンプル設定では `_ToDelete\ExtractedZips`）に移します。
- **安全策**
  - ダウンロードから 10 分たっていないファイルと、開いている途中のファイルは処理しません。
  - 移動先に同じ名前のファイルがあり、中身も同じなら 重複フォルダ（サンプル設定では `_ToDelete\Duplicates`）へ、中身が違えば ` (2)` を付けて移動します。
  - Crossref に接続できないときは何もせず、次の実行でやり直します。
  - すべての移動を `logs\sort_log_YYYYMM.csv` に記録し、`undo-sort.ps1` で元に戻せます。

## 必要なもの

- Windows 10 / 11（Windows PowerShell 5.1）
- Python 3.10 以上と [PyMuPDF](https://pymupdf.readthedocs.io/)（論文の機能を使う場合のみ）

## セットアップ

```powershell
# 1. 設定ファイルを作り、移動先などを自分の環境に合わせて書き換える
Copy-Item rules.example.json rules.json

# 2. 論文の機能用の Python 環境（パスは rules.json の papers.python と合わせる）
py -3 -m venv "$env:LOCALAPPDATA\DownloadSorter\venv"
& "$env:LOCALAPPDATA\DownloadSorter\venv\Scripts\python.exe" -m pip install -r requirements.txt

# 3. まず試運転（実際には動かさず、移動先だけ表示）
powershell -ExecutionPolicy Bypass -File sort-downloads.ps1 -DryRun

# 4. タスクスケジューラに登録（30 分ごと + サインイン時）
powershell -ExecutionPolicy Bypass -File register-task.ps1
```

## 使い方

| やりたいこと | コマンド |
|---|---|
| 試運転 | `sort-downloads.ps1 -DryRun` |
| 未特定フォルダの翻訳版・補足資料を今すぐ照合し直す | `sort-downloads.ps1 -Recheck` |
| 指定した日時以降の移動を元に戻す | `undo-sort.ps1 -Since "2026-11-17 09:00"` |
| 直近 5 件の移動を元に戻す | `undo-sort.ps1 -Last 5` |
| 自動実行を止める | `Unregister-ScheduledTask DownloadSorter` |

## 既存の論文フォルダを論文ごとのフォルダに整理し直す

本文・翻訳版・補足資料が別々のフォルダ（既定では `main`、`translations`、`supplements`）に分かれている既存の論文を、まとめて論文ごとのフォルダに組み直せます。フォルダ名が違う場合は `--main-dir`、`--trans-dir`、`--supp-dir` で指定します。

```powershell
$py = "$env:LOCALAPPDATA\DownloadSorter\venv\Scripts\python.exe"
# 計画だけを作る（ファイルは動かさない）。plan.csv を開いて中身を確認する
& $py -I paper_tool.py plan-reorg "<論文フォルダ>" plan.csv
# 計画どおりに移動する（記録は logs\ に保存され、undo-sort.ps1 で戻せる）
& $py -I paper_tool.py apply-reorg plan.csv "<論文フォルダ>" "<重複の移動先フォルダ>" logs\sort_log_reorg.csv
```

## ファイル構成

| ファイル | 役割 |
|---|---|
| `sort-downloads.ps1` | 振り分けの本体 |
| `paper_tool.py` | DOI の検出、Crossref への問い合わせ、論文フォルダの決定、既存フォルダの再整理 |
| `undo-sort.ps1` | 記録をもとに移動を元に戻す |
| `register-task.ps1` | タスクスケジューラへの登録 |
| `rules.example.json` | 設定のサンプル（`rules.json` にコピーして使う） |

`rules.json`、`paper_index.json`（手元の論文の一覧）、`logs\` は個人のデータなので、`.gitignore` で除外しています。

## 注意

- Crossref に送るのは DOI とタイトルの文字列だけです。応答は `%LOCALAPPDATA%\DownloadSorter\crossref_cache.json` に保存されます。
- スキャンした（画像だけの）PDF からは DOI を読み取れないため、名前は変わりません。
- `.ps1` に日本語を直接書くと Windows PowerShell 5.1 で文字化けするため、日本語のフォルダ名などはすべて `rules.json` に書く設計です。
