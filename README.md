# Team Discord MCP (`team-discord`)

Kết nối AI của tất cả thành viên trong team vào chung 1 nhóm chat **Discord**, dùng **Google `gemini-decision-1`** làm bộ định tuyến chia việc tự động (Router ~200ms, 0 output token), kèm **Windows Toast Notification** và **Reactive Wakeup** (ngủ tốn 0 token, có việc tự dậy).

## Kiến trúc
- **`discord_daemon.py`**: Chạy ngầm 24/7 (~25MB RAM) mở WebSocket tới Discord Gateway v10.
  - Bắt `@AI-Tên` trong **< 1ms** -> lưu vào `inbox` + thả reaction `👀` + nổ **Windows Toast Noti**.
  - Nếu `IS_ROUTER=true`: dùng **`gemini-decision-1`** đọc tin nhắn chưa `@` (kể cả ảnh/video/PDF đính kèm) + `team_roster.json` -> tự chấm và bắn tin giao việc `@AI-X` lên Discord.
- **`mcp_server.py`**: MCP Server (`FastMCP`) gắn vào Antigravity / Cursor / Claude Code với 7 tool:
  - `discord_send`, `discord_inbox`, `discord_history`, `discord_set_status`, `discord_list_channels`, `discord_roster`, `discord_route_test`.
- **`discord_watch.py`**: CLI hỗ trợ chế độ ngủ chờ việc **0 token** (`python discord_watch.py wait --timeout 3600`) để tự đánh thức Antigravity ngay khi có noti.

## Hướng dẫn cài cho thành viên mới (2 phút)
1. Clone repo về máy:
```bash
git clone https://github.com/GiangDA881/Aicomunication.git
cd Aicomunication
```
2. Cài thư viện:
```bash
pip install -r requirements.txt
```
3. Copy `.env.example` thành `.env`, điền `DISCORD_BOT_TOKEN` chung của team, đổi `AI_AGENT_NAME` theo tên máy mình, giữ `IS_ROUTER=false`.
4. Thêm tên & vai trò của mình vào `team_roster.json` (trên máy Router).
5. Thêm block `team-discord` vào `mcp_config.json` của IDE:
```json
{
  "mcpServers": {
    "team-discord": {
      "command": "python",
      "args": ["<đường-dẫn>/team_discord_mcp/mcp_server.py"]
    }
  }
}
```

## Lệnh test nhanh
```powershell
# 1. Test bộ chia việc gemini-decision-1 (không gửi lên Discord)
python discord_watch.py test-router "Tập 4 bị lỗi dính logo BBC, kiểm tra và render lại giúp anh"

# 2. Bật daemon nghe Discord 24/7
python discord_watch.py ensure-daemon

# 3. Chế độ trực chờ noti (0 token - dành cho Antigravity Reactive Wakeup)
python discord_watch.py wait --timeout 3600

# 4. Gửi tin nhắn + đính kèm file
python discord_watch.py send "Đã xong cảnh 3 rồi nhé" --to AI-Editor
```
