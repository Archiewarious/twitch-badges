import asyncio, os, sys, json, shutil, io, contextlib, re
from pathlib import Path
os.environ.update(TELEGRAM_BOT_TOKEN="0:dummy", TELEGRAM_CHANNEL_ID="-100000", PUBLISH_ENABLED="true", QUIET_HOURS_START="0", QUIET_HOURS_END="0")
WT = Path("/home/alex/twitch-badges-next"); SCR = Path(sys.argv[1])
sys.path.insert(0, str(WT)); sys.path.insert(0, str(WT / "bot"))
with contextlib.redirect_stderr(io.StringIO()):
    import bot as b
st = json.load(open(WT / "data/published.json"))
KEY = sys.argv[2]
st[KEY].update(started=False, cond_vague=True, cond_known=False, ending=False)
json.dump(st, open(SCR / "p2.json", "w")); b.PUBLISHED_FILE = SCR / "p2.json"
async def fa(*a, **k): pass
b.send_alert = b.clear_alert = fa
async def ns(*a, **k): pass
b.asyncio.sleep = ns
class FakeBot:
    async def send_photo(self, chat_id, photo, caption=None, **kw): print("POST:", " / ".join(re.sub(r"<[^>]+>","",l) for l in caption.split("\n") if l.strip())[:230])
    async def send_media_group(self, chat_id, media, **kw): print("ALBUM:", media[0].caption[:200])
class C: pass
ctx = C(); ctx.bot = FakeBot()
async def main():
    for i in range(3):
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(sys.stdout):
            await b.publish_new(ctx)
asyncio.run(main())
