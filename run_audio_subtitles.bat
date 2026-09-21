@echo off
chcp 65001 >nul
title 智能 AI 字幕翻译与视频字幕合成大师
cd /d "%~dp0"

echo 正在启动 智能 AI 字幕翻译与视频字幕合成大师...
start "" python audio_subtitle_tool.py
exit
