"""The slash-command tree builds and validates without connecting to Discord."""

from agenthub.discord_bot.bot import AgentHubBot, _render
from agenthub.models import Event


async def test_command_tree_registers():
    bot = AgentHubBot()
    from agenthub.discord_bot.bot import register
    register(bot.tree)
    groups = {c.name: [s.name for s in c.commands] for c in bot.tree.get_commands()}
    assert set(groups) == {"project", "agent", "knowledge", "task", "debate"}
    assert "key" in groups["agent"] and "approve" in groups["task"]
    payload = [c.to_dict(bot.tree) for c in bot.tree.get_commands()]  # Discord-side schema validation
    assert all(len(p["name"]) <= 32 for p in payload)
    await bot.close()


def test_render_projection():
    e = Event(message_type="task.state", payload={"key": "AH-1", "title": "t", "from": "a", "to": "b"})
    assert "AH-1" in _render(e)
