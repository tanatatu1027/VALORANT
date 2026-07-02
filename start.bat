@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title VALORANT AIコーチ

echo ================================================
echo   VALORANT AIコーチ 起動スクリプト
echo ================================================
echo.

rem ---- Pythonを探す（py ランチャー優先） ----
set "PY="
where py >nul 2>nul
if not errorlevel 1 set "PY=py -3"
if not defined PY (
  where python >nul 2>nul
  if not errorlevel 1 set "PY=python"
)
if not defined PY goto :nopython

rem ---- バージョン確認（3.10以上） ----
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>nul
if errorlevel 1 goto :nopython

for /f "delims=" %%v in ('%PY% --version') do echo 使用するPython: %%v
echo.

rem ---- 仮想環境 ----
if not exist .venv (
  echo [1/3] 仮想環境を作成しています...（初回のみ）
  %PY% -m venv .venv
  if errorlevel 1 goto :fail
)
call .venv\Scripts\activate.bat
if errorlevel 1 goto :fail

echo [2/3] 依存ライブラリを確認しています...（初回は数分かかります）
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements.txt
if errorlevel 1 goto :fail

echo [3/3] サーバーを起動します
echo.
echo   ブラウザで http://localhost:8000 を開いてください
echo   終了するにはこのウィンドウで Ctrl+C を押すか、ウィンドウを閉じてください
echo.
echo   ※ AI詳細レポートを使う場合は、起動前にコマンドプロンプトで
echo      set ANTHROPIC_API_KEY=sk-ant-... を実行してから start.bat を実行
echo   ※ 管理者タブのトークンはこの下に表示されます
echo.
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
echo.
echo サーバーが終了しました。
pause
exit /b 0

:nopython
echo [エラー] Python 3.10以上が見つかりません。
echo.
echo   1. https://www.python.org/downloads/ を開き「Download Python 3.x」を
echo      クリックしてインストーラーをダウンロードしてください
echo   2. インストール画面の最初で「Add python.exe to PATH」に
echo      必ずチェックを入れてから「Install Now」を押してください
echo   3. インストール完了後、この start.bat をもう一度ダブルクリックしてください
echo.
echo   ※ すでにインストール済みなのにこのエラーが出る場合は、
echo      チェックを入れ忘れている可能性があります。Pythonを一度アンインストールし、
echo      チェックを入れて再インストールするのが確実です。
echo.
pause
exit /b 1

:fail
echo.
echo [エラー] セットアップに失敗しました。
echo   上に表示されているエラーメッセージを確認してください。
echo   解決しない場合は .venv フォルダを削除してからもう一度実行してみてください。
echo.
pause
exit /b 1
