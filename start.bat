@echo off
rem VALORANT AIコーチ 起動スクリプト（Windows用）
rem 使い方: start.bat をダブルクリック（初回はインストールに数分かかります）
cd /d "%~dp0"

if not exist .venv (
  echo [1/3] 仮想環境を作成しています...
  python -m venv .venv
)
call .venv\Scripts\activate.bat

echo [2/3] 依存ライブラリを確認しています...
pip install -q -r requirements.txt

echo [3/3] サーバーを起動します: http://localhost:8000
echo   - AI詳細レポートを使う場合: 事前に set ANTHROPIC_API_KEY=sk-ant-...
echo   - 管理者トークンを固定する場合: set VALO_COACH_ADMIN_TOKEN=...
echo     （未設定なら下に自動生成トークンが表示されます）
echo.
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
pause
