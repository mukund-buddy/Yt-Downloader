@echo off
setlocal EnableExtensions
title TuneGrab
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo Python 3 is required but was not found on PATH.
  echo Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
  echo.
  pause
  exit /b 1
)

python -c "import yt_dlp" >nul 2>nul
if errorlevel 1 (
  echo Installing yt-dlp, one moment...
  python -m pip install --user --upgrade yt-dlp
  if errorlevel 1 (
    echo Could not install yt-dlp. Run: python -m pip install --user yt-dlp
    pause
    exit /b 1
  )
)

where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo.
  echo WARNING: ffmpeg was not found on PATH.
  echo Metadata cleaning, SponsorBlock cuts and video merging need ffmpeg.
  echo Install it with: winget install Gyan.FFmpeg
  echo.
)

echo.
echo Starting TuneGrab...
echo Keep this window open while you download. Press Ctrl+C to stop.
echo.
python bridge\tunegrab_bridge.py serve --open %*
if errorlevel 1 pause
