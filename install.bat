@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Team Discord MCP — 1-Click Installer
python install.py
pause
