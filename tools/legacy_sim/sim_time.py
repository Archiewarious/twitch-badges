import asyncio, os, sys, json, shutil, io, contextlib, re
from pathlib import Path
from datetime import datetime as _dt, timedelta, timezone
os.environ.update(TELEGRAM_BOT_TOKEN="0:dummy", TELEGRAM_CHANNEL_ID="-100000", PUBLISH_ENABLED="true",
                  QUIET_HOURS_START="0", QUIET_HOURS_END="0")
WT = Path("/home/alex/twitch-badges-next"); SCR = Path(sys.argv[1])
sys.path.insert(0, str(WT)); sys.path.insert(0, str(WT / "bot"))
with contextlib.redirect_stderr(io.StringIO()):
    import bot as b
site = b.site
OFF = [timedelta(0)]
class FDT(_dt):
    @classmethod
    def now(cls, tz=None):
        return _dt.now(tz) + OFF[0]
b.datetime = FDT; site.datetime = FDT
shutil.copy(WT / "data/published.json", SCR / "published.json"); b.PUBLISHED_FILE = SCR / "published.json"
shutil.copy(WT / "data/streamdb_latest.json", SCR / "snap.json"); b.DATA_FILE = SCR / "snap.json"
async def fa(key, subject, body=""): print(f"  +{OFF[0]} [ALERT {key}] {subject}")
async def fc(key, note=""): pass
b.send_alert, b.clear_alert = fa, fc
async def ns(*a, **k): pass
b.asyncio.sleep = ns
posts = []
class FakeBot:
    async def send_photo(self, chat_id, photo, caption=None, **kw): posts.append((OFF[0], caption or ""))
    async def send_media_group(self, chat_id, media, **kw): posts.append((OFF[0], media[0].caption or ""))
class Ctx: pass
ctx = Ctx(); ctx.bot = FakeBot()
step = timedelta(hours=float(sys.argv[2])); days = int(sys.argv[3])
async def main():
    t = timedelta(0)
    while t <= timedelta(days=days):
        OFF[0] = t
        ts = (_dt.now(timezone.utc) + t).timestamp(); os.utime(b.DATA_FILE, (ts, ts))
        b._cache["mtime"] = None
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            await b.publish_new(ctx)
        t += step
asyncio.run(main())
for off, cap in posts:
    lines = [re.sub(r"<[^>]+>", "", l) for l in cap.split("\n") if l.strip()]
    print(f"+{off.days:>2}d{off.seconds//3600:02d}h | {' / '.join(lines[:2])[:120]}")
st = json.load(open(SCR / "published.json"))
print("state after:", len(st))
