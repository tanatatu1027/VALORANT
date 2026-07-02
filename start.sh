#!/usr/bin/env bash
# VALORANT AIコーチ 起動スクリプト（Mac / Linux用）
# 使い方:  ./start.sh
# 初回は依存ライブラリのインストールに数分かかります。
set -e
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  echo "[1/3] 仮想環境を作成しています..."
  python3 -m venv .venv
fi
source .venv/bin/activate

echo "[2/3] 依存ライブラリを確認しています..."
pip install -q -r requirements.txt

echo "[3/3] サーバーを起動します: http://localhost:8000"
echo "  - AI詳細レポートを使う場合: 事前に export ANTHROPIC_API_KEY=sk-ant-..."
echo "  - 管理者トークンを固定する場合: export VALO_COACH_ADMIN_TOKEN=..."
echo "    （未設定なら下に自動生成トークンが表示されます）"
echo
exec python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
