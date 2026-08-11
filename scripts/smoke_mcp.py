"""End-to-end smoke: spawn the MCP server over stdio (like Claude Code does),
list tools, post an intent, complete it as uncommitted, then land it twice to
prove landing is idempotent. Needs a reachable registry:

    FLIGHTPLAN_URL=<registry url> FLIGHTPLAN_API_KEY=<key> \
        uv run python scripts/smoke_mcp.py
"""

import asyncio
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def text(result) -> dict:
    return json.loads(result.content[0].text)


async def main() -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "flightplan.mcp_server"],
        env=dict(os.environ),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = sorted(t.name for t in tools.tools)
            print("tools:", names)
            assert names == [
                "complete_intent", "list_intents", "mark_intent_landed",
                "post_intent", "update_intent",
            ], names

            posted = text(
                await session.call_tool(
                    "post_intent",
                    {
                        "repo": "smoke-test",
                        "summary": "Smoke-testing the intent registry end to end.",
                        "touches": ["scripts/smoke*"],
                        "kind": "spike",
                    },
                )
            )
            print("posted:", posted["id"], "overlaps:", len(posted["overlaps"]))

            done = text(
                await session.call_tool(
                    "complete_intent",
                    {
                        "id": posted["id"],
                        "status": "done",
                        "outcome": "Smoke test passed; registry and MCP wiring work end to end.",
                        # Declared uncommitted so the landing below has
                        # something real to correct.
                        "uncommitted": True,
                    },
                )
            )
            assert done["intent"]["status"] == "done", done
            print("completed:", done["intent"]["id"])

            landed = text(
                await session.call_tool(
                    "mark_intent_landed",
                    {"id": posted["id"], "commits": ["0" * 40]},
                )
            )
            stamp = landed["intent"]["landed_at"]
            assert stamp, landed
            # `uncommitted` is the completion-time fact and is never rewritten.
            assert landed["intent"]["uncommitted"] is True, landed
            print("landed:", stamp)

            again = text(
                await session.call_tool(
                    "mark_intent_landed", {"id": posted["id"]},
                )
            )
            assert again["intent"]["landed_at"] == stamp, again
            print("landing is idempotent")
            print("SMOKE OK")


if __name__ == "__main__":
    asyncio.run(main())
