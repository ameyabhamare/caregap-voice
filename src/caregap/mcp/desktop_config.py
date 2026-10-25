"""``make mcp-config``: the Claude Desktop config entry for this checkout.

Claude Desktop does not inherit your shell's PATH, so the entry uses absolute paths.
Merge the printed ``mcpServers`` entry into claude_desktop_config.json (docs/mcp.md).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from caregap.config import get_settings


def entry(uv: str, repo: Path, core_url: str) -> dict[str, Any]:
    return {
        "mcpServers": {
            "caregap": {
                "command": uv,
                "args": ["--directory", str(repo), "run", "python", "-m", "caregap.mcp"],
                "env": {"CORE_URL": core_url},
            }
        }
    }


def main() -> None:
    repo = Path(__file__).resolve().parents[3]
    uv = shutil.which("uv") or "uv"
    print(json.dumps(entry(uv, repo, get_settings().core_url), indent=2))


if __name__ == "__main__":
    main()
