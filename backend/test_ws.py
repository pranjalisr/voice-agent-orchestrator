"""End-to-end test through the real WebSocket server."""
import asyncio
import json

import websockets


async def main():
    uri = "ws://127.0.0.1:8000/ws"
    async with websockets.connect(uri) as ws:
        got = []

        async def reader():
            try:
                async for msg in ws:
                    ev = json.loads(msg)
                    got.append(ev)
                    d = {k: v for k, v in ev.get("data", {}).items() if k != "steps"}
                    print(f"  <- {ev['type']:12} {d}")
            except websockets.ConnectionClosed:
                pass

        rt = asyncio.create_task(reader())

        print("-> user_utterance: schedule a sync for Monday at 10am")
        await ws.send(json.dumps({"type": "user_utterance",
                                  "text": "schedule a sync for Monday at 10am"}))
        await asyncio.sleep(0.7)

        print("-> barge_in: wait no, change that to Tuesday")
        await ws.send(json.dumps({"type": "barge_in",
                                  "text": "wait no, change that to Tuesday"}))
        await asyncio.sleep(3.0)

        rt.cancel()

        types = [e["type"] for e in got]
        assert "interrupted" in types
        assert "replanned" in types
        assert "done" in types
        tuesday = any(e["type"] == "tool_end"
                      and e["data"].get("result", {})
                      and e["data"]["result"].get("day") == "Tuesday" for e in got)
        assert tuesday, "no Tuesday event created"
        print("\n\u2705 WebSocket end-to-end PASS")


if __name__ == "__main__":
    asyncio.run(main())
