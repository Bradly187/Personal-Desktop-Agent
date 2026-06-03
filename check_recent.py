import asyncio, time, sys
sys.path.insert(0, ".")
async def main():
    import aiosqlite
    cutoff = time.time() - 600
    async with aiosqlite.connect("agent.db") as db:
        async with db.execute(
            "SELECT datetime(ts,'unixepoch','localtime'), source, text, action FROM commands WHERE ts > ? ORDER BY ts DESC LIMIT 15",
            (cutoff,)
        ) as cur:
            rows = await cur.fetchall()
    if not rows:
        print("No commands in last 10 minutes")
    for r in rows:
        print(r)
asyncio.run(main())
