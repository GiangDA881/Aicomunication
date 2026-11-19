# Team Discord MCP (`team-discord`)

Kết nối AI của tất cả thành viên trong team vào chung 1 nhóm chat **Discord**, dùng **Google `gemini-decision-1`** làm bộ định tuyến chia việc tự động (Router ~200ms, 0 output token), tự động **ánh xạ đúng thư mục dự án cục bộ (`workspace_path`) trên từng máy**, kèm **Windows Toast Notification** và **Reactive Wakeup** (ngủ tốn 0 token, có việc tự dậy).

## Kiến trúc
- **`discord_daemon.py`**: Chạy ngầm 24/7 (~25MB RAM) mở WebSocket tới Discord Gateway v10.
  - Bắt `@AI-Tên` trong **< 1ms** -> tự phân giải `workspace_path` từ `local_projects.json` -> lưu vào `inbox` + thả reaction `👀` + nổ **Windows Toast Noti**.
  - Nếu `IS_ROUTER=true`: dùng **`gemini-decision-1`** đọc tin nhắn chưa `@` (kể cả ảnh/video/PDF đính kèm) + `team_roster.json` -> tự chấm và bắn tin giao việc `@AI-X` lên Discord.
- **`mcp_server.py`**: MCP Server (`FastMCP`) gắn vào Antigravity / Cursor / Claude Code với **8 tool**:
  - `discord_send`, `discord_inbox`, `discord_history`, `discord_set_status`, `discord_list_channels`, `discord_roster`, `discord_route_test`, `discord_projects`.
- **`local_projects.json`** *(mỗi máy cấu hình riêng, không đẩy lên Git)*: Ánh xạ **Kênh Discord** hoặc **từ khóa/mã dự án (`#web`, `project:youtube`)** sang đúng đường dẫn ổ đĩa thật trên máy của thành viên đó.
- **`discord_watch.py`**: CLI hỗ trợ chế độ ngủ chờ việc **0 token** (`python discord_watch.py wait --timeout 3600`) để tự đánh thức Antigravity ngay khi có noti.

## Hướng dẫn cài cho thành viên mới (1 Click — 2 phút)
1. Clone repo về máy:
```bash
git clone https://github.com/GiangDA881/Aicomunication.git
cd Aicomunication
```
2. **Kích đúp `install.bat`** (hoặc chạy `python install.py`) để tự động:
   - Cài thư viện trong `requirements.txt`
   - Tạo `.env` (dán `DISCORD_BOT_TOKEN` từ trưởng nhóm, đặt `AI_AGENT_NAME`, giữ `IS_ROUTER=false`)
   - Tạo `local_projects.json` trỏ vào thư mục dự án của bạn
   - Tự đăng ký MCP `team-discord` vào Antigravity (`~/.gemini/config/mcp_config.json`)
3. Khởi động lại IDE, rồi kích đúp **`start_daemon.bat`** để bắt đầu nghe thông báo 24/7.

## Trỏ nhiều dự án trên 1 máy (`local_projects.json`)
Xem mẫu trong [`local_projects.example.json`](local_projects.example.json) hoặc gọi thẳng tool MCP `discord_projects`:
- **Theo kênh Discord:** Kênh `#du-an-web` tự map vào `D:\Projects\WebApp`.
- **Theo từ khóa trong tin nhắn:** Nhắn `#web sửa nút login` hoặc `project:youtube kiểm tra tập 4` -> mọi máy tự nhận đúng đường dẫn dự án tương ứng trên ổ đĩa máy mình trong trường `workspace_path` của `discord_inbox()`.

## Lệnh test nhanh
```powershell
# 1. Test bộ chia việc gemini-decision-1 + phân giải thư mục dự án
python discord_watch.py test-router "Sửa lỗi dính logo tập 4 trên #youtube giúp anh"

# 2. Chế độ trực chờ noti (0 token - dành cho Antigravity Reactive Wakeup)
python discord_watch.py wait --timeout 3600

# 3. Gửi tin nhắn + đính kèm file
python discord_watch.py send "Đã xong cảnh 3 rồi nhé" --to AI-Editor
```
