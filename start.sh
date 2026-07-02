#!/usr/bin/env bash
# VALORANT AIコーチ 起動スクリプト（Mac / Linux用）
# 使い方:  ./start.sh
#   実行権限がないと言われたら:  bash start.sh
set -e
cd "$(dirname "$0")"

echo "================================================"
echo "  VALORANT AIコーチ 起動スクリプト"
echo "================================================"
echo

# ---- Pythonを探す ----
PY=""
for cand in python3 python; do
  if command -v "$cand" >/dev/null 2>&1; then
    if "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
      PY="$cand"
      break
    fi
  fi
done

if [ -z "$PY" ]; then
  echo "[エラー] Python 3.10以上が見つかりません。"
  echo "  Mac:   https://www.python.org/downloads/ からインストール"
  echo "         （Homebrewなら: brew install python）"
  echo "  Linux: sudo apt install python3 python3-venv python3-pip など"
  echo "  インストール後、もう一度このスクリプトを実行してください。"
  exit 1
fi
echo "使用するPython: $($PY --version)"
echo

# ---- 仮想環境 ----
if [ ! -d ".venv" ]; then
  echo "[1/3] 仮想環境を作成しています...（初回のみ）"
  "$PY" -m venv .venv || {
    echo "[エラー] 仮想環境の作成に失敗しました。"
    echo "  Ubuntu/Debianの場合: sudo apt install python3-venv を実行してから再試行してください。"
    exit 1
  }
fi
# shellcheck disable=SC1091
source .venv/bin/activate

echo "[2/3] 依存ライブラリを確認しています...（初回は数分かかります）"
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements.txt

echo "[3/3] サーバーを起動します"
echo
echo "  ブラウザで http://localhost:8000 を開いてください"
echo "  終了するには Ctrl+C"
echo
echo "  ※ AI詳細レポートを使う場合: export ANTHROPIC_API_KEY=sk-ant-... を実行してから起動"
echo "  ※ 管理者タブのトークンはこの下に表示されます"
echo
exec python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
