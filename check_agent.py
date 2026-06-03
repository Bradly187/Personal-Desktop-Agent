import sys, asyncio, time
sys.path.insert(0, ".")
async def main():
    import aiosqlite
    cutoff = time.time() - 7200
    async with aiosqlite.connect("agent.db") as db:
        async with db.execute(
            "SELECT datetime(ts,'unixepoch','localtime'), level, msg FROM ipad_logs WHERE ts > ? ORDER BY ts DESC LIMIT 5",
            (cutoff,)
        ) as c:
            rows = await c.fetchall()
    print("ipad_logs (last 2h):")
    for r in rows: print(" ", r)

    async with aiosqlite.connect("agent.db") as db:
        async with db.execute(
            "SELECT id, datetime(start_time,'unixepoch','localtime') FROM sessions ORDER BY id DESC LIMIT 3"
        ) as c:
            rows = await c.fetchall()
    print("sessions:")
    for r in rows: print(" ", r)

    async with aiosqlite.connect("agent.db") as db:
        async with db.execute(
            "SELECT datetime(ts,'unixepoch','localtime'), sensor, value FROM sensor_telemetry ORDER BY ts DESC LIMIT 3"
        ) as c:
            rows = await c.fetchall()
    print("sensor_telemetry (latest):")
    for r in rows: print(" ", r)

asyncio.run(main())
