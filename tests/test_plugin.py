"""Claude Code 플러그인(스킬 + MCP)이 실제 MCP 서버와 어긋나지 않는지."""

import json
import re
from pathlib import Path

import anyio

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "plugin"


def test_manifests():
    plugin = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert plugin["name"] == "sasu"
    mcp = json.loads((PLUGIN / ".mcp.json").read_text(encoding="utf-8"))
    assert mcp["mcpServers"]["sasu"] == {"command": "sasu", "args": ["mcp"]}
    market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    assert market["plugins"][0]["source"] == "./plugin"


def test_skill_mentions_only_real_tools():
    """스킬 문서의 도구 표에 적힌 이름이 모두 MCP 서버에 있고, 서버의 도구도 모두 표에 있다."""
    from sasu.mcp_server import server

    text = (PLUGIN / "skills" / "sasu" / "SKILL.md").read_text(encoding="utf-8")
    front = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    assert front and "name: sasu" in front.group(1) and "description:" in front.group(1)
    table = text.split("## 도구", 1)[1].split("## 흐름 1", 1)[0]
    in_skill = set(re.findall(r"\| `(\w+)` \|", table))
    in_server = {t.name for t in anyio.run(server.list_tools)}
    assert in_skill == in_server
