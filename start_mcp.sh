#!/bin/bash
# HxMV MCP server wrapper —— 只做转发：真正的状态（run / 记忆 / 项目档案）都在面板进程里。
# 用法：  HXMV_PYTHON=/path/to/python ./start_mcp.sh
# 需要一个装了 mcp SDK 的解释器（pip install 'mcp>=1.9.0'）；
# 若你把 HxMV 接进 Hermes，直接复用 Hermes 自带 venv 的解释器即可，不必再养一个 venv。
set -e
cd "$(dirname "$0")"
exec "${HXMV_PYTHON:-python3}" mcp_server.py
