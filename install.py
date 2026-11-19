"""One-click interactive installer for Team Discord MCP (Aicomunication).

Runs via `install.bat` (Windows) or `python install.py`:
  1. Installs Python dependencies (`requirements.txt`).
  2. Creates `.env` interactively (agent name, bot token, optional webhook).
  3. Creates `local_projects.json` pointing to this teammate's local project directory.
  4. Registers `team-discord` in Antigravity's `mcp_config.json` automatically
     (and prints copy-paste JSON for Cursor / Claude Code).
  5. Verifies live Discord Bot API connectivity.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
PROJECTS_PATH = BASE_DIR / "local_projects.json"
MCP_SERVER_PATH = BASE_DIR / "mcp_server.py"
ANTIGRAVITY_MCP_CONFIG = Path.home() / ".gemini" / "config" / "mcp_config.json"


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    val = input(f"{prompt}{suffix}: ").strip()
    return val if val else default


def step_install_deps() -> None:
    print("\n[1/5] Đang cài đặt thư viện Python (requirements.txt)...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", str(BASE_DIR / "requirements.txt")])


def step_setup_env() -> dict:
    print("\n[2/5] Cấu hình file .env cho máy của bạn:")
    existing: dict[str, str] = {}
    if ENV_PATH.exists():
        for ln in ENV_PATH.read_text(encoding="utf-8").splitlines():
            if "=" in ln and not ln.strip().startswith("#"):
                k, v = ln.split("=", 1)
                existing[k.strip()] = v.strip()

    cur_tok = existing.get("DISCORD_BOT_TOKEN", "")
    if cur_tok and "PASTE" not in cur_tok and len(cur_tok) > 30:
        print(f"  -> Đã có DISCORD_BOT_TOKEN (dài {len(cur_tok)} ký tự). Nhấn Enter để giữ nguyên.")
        tok = ask("  DISCORD_BOT_TOKEN", "") or cur_tok
    else:
        tok = ask("  Dán DISCORD_BOT_TOKEN (lấy từ trưởng nhóm)")

    agent_name = ask("  Tên AI máy bạn (VD: AI-Minh, AI-Nam)", existing.get("AI_AGENT_NAME", "AI-Member"))
    agent_role = ask(
        "  Vai trò/chuyên môn chính",
        existing.get("AI_AGENT_ROLE", "Phụ trách hỗ trợ code, kịch bản hoặc dựng video theo phân công"),
    )
    channel_id = ask("  DISCORD_CHANNEL_ID mặc định (#chung)", existing.get("DISCORD_CHANNEL_ID", "1572101960855724055"))
    webhook_url = ask("  DISCORD_WEBHOOK_URL (để trống nếu chưa có)", existing.get("DISCORD_WEBHOOK_URL", ""))
    is_router = ask("  Máy này có làm Router trung tâm không? (chỉ 1 máy chọn true)", existing.get("IS_ROUTER", "false"))

    env_text = f"""# Cấu hình Team Discord MCP — tạo tự động bởi install.py
DISCORD_BOT_TOKEN={tok}
DISCORD_CHANNEL_ID={channel_id}
DISCORD_WATCH_ALL_CHANNELS=true
DISCORD_WEBHOOK_URL={webhook_url}

AI_AGENT_NAME={agent_name}
AI_AGENT_ROLE={agent_role}

IS_ROUTER={is_router.lower()}
USE_GEMINI_DECISION={'true' if is_router.lower() == 'true' else 'false'}
DECISION_THRESHOLD=0.70
ENABLE_WINDOWS_TOAST=true
MAX_AI_HOPS=3
DISCORD_APP_ID={existing.get('DISCORD_APP_ID', '1572813558050193418')}
"""
    ENV_PATH.write_text(env_text, encoding="utf-8")
    print(f"  -> Đã lưu {ENV_PATH}")
    return {"token": tok, "agent_name": agent_name, "channel_id": channel_id}


def step_setup_projects(default_channel_id: str) -> None:
    print("\n[3/5] Trỏ thư mục dự án cục bộ trên máy bạn (local_projects.json):")
    if PROJECTS_PATH.exists():
        print(f"  -> Đã có sẵn {PROJECTS_PATH}. Nhấn Enter để giữ nguyên hoặc nhập đường dẫn mới.")
        cur = json.loads(PROJECTS_PATH.read_text(encoding="utf-8"))
        default_ws = ask("  Thư mục dự án mặc định", cur.get("default_workspace", str(BASE_DIR.parent)))
        cur["default_workspace"] = default_ws
        PROJECTS_PATH.write_text(json.dumps(cur, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"  -> Đã cập nhật default_workspace = {default_ws}")
        return

    default_ws = ask("  Đường dẫn thư mục dự án chính trên máy bạn", str(BASE_DIR.parent))
    proj_id = ask("  Mã ngắn cho dự án này (VD: youtube, web, app)", "main")
    proj_name = ask("  Tên hiển thị dự án", Path(default_ws).name or proj_id)
    aliases_raw = ask("  Từ khóa nhận diện dự án (cách nhau dấu phẩy)", proj_id)
    aliases = [a.strip().lower() for a in aliases_raw.split(",") if a.strip()]
    if proj_id.lower() not in aliases:
        aliases.insert(0, proj_id.lower())

    cfg = {
        "default_workspace": default_ws,
        "projects": [
            {
                "id": proj_id.lower(),
                "name": proj_name,
                "path": default_ws,
                "channel_ids": [default_channel_id] if default_channel_id else [],
                "aliases": aliases,
            }
        ],
    }
    PROJECTS_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"  -> Đã tạo {PROJECTS_PATH}")


def step_register_mcp() -> None:
    print("\n[4/5] Đăng ký MCP server 'team-discord' vào Antigravity:")
    entry = {
        "command": sys.executable,
        "args": [str(MCP_SERVER_PATH)],
    }
    try:
        ANTIGRAVITY_MCP_CONFIG.parent.mkdir(parents=True, exist_ok=True)
        if ANTIGRAVITY_MCP_CONFIG.exists():
            data = json.loads(ANTIGRAVITY_MCP_CONFIG.read_text(encoding="utf-8"))
        else:
            data = {}
        data.setdefault("mcpServers", {})
        data["mcpServers"]["team-discord"] = entry
        ANTIGRAVITY_MCP_CONFIG.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"  -> Đã ghi vào {ANTIGRAVITY_MCP_CONFIG}")
    except Exception as exc:
        print(f"  -> Không thể tự ghi Antigravity config ({exc}).")

    print("\n  (Nếu bạn dùng Cursor / Claude Code, dán đoạn sau vào mcp_config.json):")
    print(json.dumps({"team-discord": entry}, ensure_ascii=False, indent=2))


def step_verify_discord(token: str) -> None:
    print("\n[5/5] Kiểm tra kết nối Discord Bot...")
    if not token or "PASTE" in token:
        print("  -> Chưa có DISCORD_BOT_TOKEN hợp lệ. Hãy mở file .env điền token rồi chạy start_daemon.bat.")
        return
    try:
        import httpx
        r = httpx.get("https://discord.com/api/v10/users/@me", headers={"Authorization": f"Bot {token}"}, timeout=10.0)
        if r.status_code == 200:
            me = r.json()
            print(f"  -> KẾT NỐI THÀNH CÔNG! Bot: {me.get('username')} (ID: {me.get('id')})")
        else:
            print(f"  -> Lỗi kết nối Discord (HTTP {r.status_code}): {r.text[:150]}")
    except Exception as exc:
        print(f"  -> Lỗi kiểm tra mạng: {exc}")


def main() -> None:
    print("=" * 60)
    print("  TEAM DISCORD MCP (Aicomunication) — CÀI ĐẶT 1 CLICK")
    print("=" * 60)
    step_install_deps()
    info = step_setup_env()
    step_setup_projects(info["channel_id"])
    step_register_mcp()
    step_verify_discord(info["token"])
    print("\nHOÀN TẤT! Hãy:")
    print("  1. Khởi động lại Antigravity / IDE để nạp 8 tool MCP 'team-discord'.")
    print("  2. Kích đúp 'start_daemon.bat' để bật nhận thông báo Discord 24/7.")


if __name__ == "__main__":
    main()
