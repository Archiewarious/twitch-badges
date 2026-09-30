"""Холостой прогон publish_new на копии data/: без сети, без alert.sh."""
import asyncio, os, sys, json, shutil, io, contextlib
from pathlib import Path
os.environ.update(TELEGRAM_BOT_TOKEN="0:dummy", TELEGRAM_CHANNEL_ID="-100000", PUBLISH_ENABLED="true")
WT = Path("/home/alex/twitch-badges-next")
sys.path.insert(0, str(WT)); sys.path.insert(0, str(WT / "bot"))
SCR = Path(sys.argv[1])
with contextlib.redirect_stderr(io.StringIO()):
    import bot as b
state_copy = SCR / "published.json"
shutil.copy(WT / "data/published.json", state_copy)
b.PUBLISHED_FILE = state_copy

async def fake_alert(key, subject, body=""): print(f"  [ALERT {key}] {subject}")
async def fake_clear(key, note=""): pass
b.send_alert, b.clear_alert = fake_alert, fake_clear
_sleep = asyncio.sleep
async def nosleep(*a, **k): pass
b.asyncio.sleep = nosleep

class FakeBot:
    def __init__(self): self.sent = []
    async def send_photo(self, chat_id, photo, caption=None, **kw):
        self.sent.append(("photo", photo, caption)); return None
    async def send_media_group(self, chat_id, media, **kw):
        self.sent.append(("album", [m.media for m in media], media[0].caption)); return []
class Ctx: pass
ctx = Ctx(); ctx.bot = FakeBot()

async def main():
    for tick in range(int(sys.argv[2]) if len(sys.argv) > 2 else 10):
        n = len(ctx.bot.sent)
        with contextlib.redirect_stderr(io.StringIO()):
            await b.publish_new(ctx)
        new = ctx.bot.sent[n:]
        if not new:
            print(f"tick {tick}: нечего постить"); break
        for kind, media, cap in new:
            head = (cap or "").split("\n")[:2]
            print(f"tick {tick}: {kind} x{len(media) if isinstance(media, list) else 1} | {' / '.join(head)[:150]}")
            if cap and len(cap) > 1024: print(f"   !!! подпись {len(cap)} > 1024")
asyncio.run(main())
