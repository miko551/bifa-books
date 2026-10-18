"""BIFA BOOKS - FastAPI API + aiogram bot in one process."""
import os, io, re, hmac, hashlib, json, time, secrets, asyncio, logging
from html import escape as esc
from datetime import datetime, timezone, timedelta
from contextlib import asynccontextmanager
from urllib.parse import parse_qsl
import httpx
from fastapi import FastAPI, Request, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import CommandStart, Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (BufferedInputFile, InlineKeyboardButton as Btn, InlineKeyboardMarkup as Kb,
                           Message, CallbackQuery, Update, WebAppInfo, MenuButtonWebApp)

from dotenv import load_dotenv
load_dotenv()
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles

E = os.environ.get
BOT_TOKEN, SB, SB_KEY = E("BOT_TOKEN", ""), E("SUPABASE_URL", "").rstrip("/"), E("SUPABASE_SERVICE_KEY", "")
ADMINS = {int(x) for x in E("ADMIN_IDS", "").split(",") if x.strip()}
CHANNEL = int(E("CHANNEL_ID", "0"))
WEBAPP, BACKEND, WH = E("WEBAPP_URL", "").rstrip("/"), E("BACKEND_URL", "").rstrip("/"), E("WEBHOOK_SECRET", "change-me")
DEV, PROTECT = E("DEV_MODE") == "1", E("PROTECT_CONTENT") == "1"
CODE_RE = re.compile(r"BB-[A-Z0-9]{6}")
ALPHA = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

bot = Bot(BOT_TOKEN or "0:x", default=DefaultBotProperties(parse_mode="HTML"))
dp, r = Dispatcher(), Router()
dp.include_router(r)
http = httpx.AsyncClient(timeout=30)
H = {"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}"}

# ---------------------------------------------------------------- Supabase
async def db(method, table, params=None, body=None, prefer="return=representation"):
    res = await http.request(method, f"{SB}/rest/v1/{table}", params=params, json=body, headers={**H, "Prefer": prefer})
    if res.status_code >= 300:
        logging.error("db %s %s: %s", method, table, res.text)
        raise HTTPException(500, "database error")
    return res.json() if res.content else []

async def put_file(bucket, path, data, ctype):
    res = await http.post(f"{SB}/storage/v1/object/{bucket}/{path}", content=data,
                          headers={**H, "Content-Type": ctype, "x-upsert": "true"})
    if res.status_code >= 300:
        raise HTTPException(500, "storage error")

async def signed(path):
    res = await http.post(f"{SB}/storage/v1/object/sign/payments/{path}", json={"expiresIn": 3600}, headers=H)
    return f"{SB}/storage/v1{res.json()['signedURL']}" if res.status_code < 300 else None

def public(bucket, path): return f"{SB}/storage/v1/object/public/{bucket}/{path}"
def now(): return datetime.now(timezone.utc).isoformat()
def token(n=6): return "".join(secrets.choice(ALPHA) for _ in range(n))

async def upsert_user(u):
    row = {"id": u["id"], "first_name": u.get("first_name"), "username": u.get("username")}
    return (await db("POST", "users", {"on_conflict": "id"}, row, "resolution=merge-duplicates,return=representation"))[0]

async def payment():
    rows = await db("GET", "settings", {"key": "eq.payment"})
    return rows[0]["value"] if rows else {}

# ---------------------------------------------------------------- Telegram initData auth
def auth(request: Request):
    d = request.headers.get("X-Init-Data", "")
    if not d:
        if DEV and request.headers.get("X-Dev-User"):
            return {"id": int(request.headers["X-Dev-User"]), "first_name": "Dev"}
        raise HTTPException(401, "Open this app from Telegram")
    p = dict(parse_qsl(d, keep_blank_values=True))
    got = p.pop("hash", "")
    check = "\n".join(f"{k}={v}" for k, v in sorted(p.items()))
    key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    ok = hmac.compare_digest(hmac.new(key, check.encode(), hashlib.sha256).hexdigest(), got)
    if not ok or time.time() - int(p.get("auth_date", 0)) > 86400:
        raise HTTPException(401, "Session expired, reopen the app")
    return json.loads(p["user"])

def admin(request: Request):
    u = auth(request)
    if u["id"] not in ADMINS:
        raise HTTPException(403, "Admins only")
    return u

# ---------------------------------------------------------------- Texts
T = {
 "en": {"welcome": "Welcome to <b>BIFA BOOKS</b> 📚\nTap below to browse books, e-books and courses.",
        "open": "📖 Open BIFA BOOKS", "pick": "Choose your language",
        "approved": "✅ Payment confirmed for <b>{t}</b>. Your files are on the way…",
        "rejected": "❌ Your payment for <b>{t}</b> was not accepted.\nReason: {r}\nYou can place a new order in the app."},
 "om": {"welcome": "Baga nagaan dhuftan <b>BIFA BOOKS</b> 📚\nKitaabota, e-book fi leenjiiwwan ilaaluuf button armaan gadii tuqaa.",
        "open": "📖 BIFA BOOKS Bani", "pick": "Afaan filadhu",
        "approved": "✅ Kaffaltiin <b>{t}</b> mirkanaa'eera. Faayiloonni kee amma sitti ergamaa jiru…",
        "rejected": "❌ Kaffaltiin <b>{t}</b> hin fudhatamne.\nSababni: {r}\nAppii keessaa ammas ajaja haaraa kennuu dandeessa."},
}
LANG_KB = lambda arg="": Kb(inline_keyboard=[[Btn(text="🇬🇧 English", callback_data=f"lang:en:{arg}"),
                                             Btn(text="🇪🇹 Afaan Oromoo", callback_data=f"lang:om:{arg}")]])
def open_kb(l, code=""):
    url = WEBAPP + (f"?p={code}" if code else "")
    return Kb(inline_keyboard=[[Btn(text=T[l]["open"], web_app=WebAppInfo(url=url))]])

# ---------------------------------------------------------------- Bot: start / language
@r.message(CommandStart())
async def start(m: Message, command: CommandObject):
    u = await upsert_user(m.from_user.model_dump())
    code = command.args if command.args and CODE_RE.fullmatch(command.args) else ""
    if not u.get("lang"):
        await m.answer("🇬🇧 Choose your language\n🇪🇹 Afaan filadhu", reply_markup=LANG_KB(code))
    else:
        await m.answer(T[u["lang"]]["welcome"], reply_markup=open_kb(u["lang"], code))

@r.message(Command("lang"))
async def lang_cmd(m: Message):
    await m.answer("🇬🇧 Choose your language\n🇪🇹 Afaan filadhu", reply_markup=LANG_KB())

@r.callback_query(F.data.startswith("lang:"))
async def set_lang(c: CallbackQuery):
    _, l, code = c.data.split(":")
    await upsert_user(c.from_user.model_dump())
    await db("PATCH", "users", {"id": f"eq.{c.from_user.id}"}, {"lang": l})
    await c.message.edit_text(T[l]["welcome"], reply_markup=open_kb(l, code))

# ---------------------------------------------------------------- Bot: /addproduct (admin)
class AP(StatesGroup):
    cover, title, price, desc = State(), State(), State(), State()

IS_ADMIN = F.from_user.id.in_(ADMINS)

@r.message(Command("cancel"), IS_ADMIN)
async def cancel(m: Message, state: FSMContext):
    await state.clear(); await m.answer("Cancelled.")

@r.message(Command("addproduct"), IS_ADMIN)
async def ap0(m: Message, state: FSMContext):
    await state.set_state(AP.cover); await m.answer("📷 Send the <b>cover image</b>. (/cancel to stop)")

@r.message(AP.cover, F.photo | F.document)
async def ap1(m: Message, state: FSMContext):
    if m.photo: f = m.photo[-1]
    elif m.document and (m.document.mime_type or "").startswith("image/"): f = m.document
    else: return await m.answer("Please send an image.")
    buf = io.BytesIO(); await bot.download(f, destination=buf)
    data = buf.getvalue()
    try:  # shrink cover so the Mini App loads fast (needs: pip install pillow)
        from PIL import Image
        img = Image.open(io.BytesIO(data)).convert("RGB"); img.thumbnail((720, 960))
        out = io.BytesIO(); img.save(out, "JPEG", quality=82, optimize=True); data = out.getvalue()
    except Exception as e:
        logging.warning("cover not resized: %s", e)
    path = f"{secrets.token_hex(8)}.jpg"; await put_file("covers", path, data, "image/jpeg")
    await state.update_data(cover=public("covers", path)); await state.set_state(AP.title)
    await m.answer("✏️ Product <b>title</b>?")

@r.message(AP.title, F.text)
async def ap2(m: Message, state: FSMContext):
    await state.update_data(title=m.text.strip()[:120]); await state.set_state(AP.price)
    await m.answer("💰 <b>Price</b> in ETB? (number only, e.g. 250)")

@r.message(AP.price, F.text)
async def ap3(m: Message, state: FSMContext):
    try:
        price = float(m.text.replace(",", "").strip()); assert price > 0
    except Exception:
        return await m.answer("Send a number, e.g. 250")
    await state.update_data(price=price); await state.set_state(AP.desc)
    await m.answer("📝 Product <b>description</b>?")

@r.message(AP.desc, F.text)
async def ap4(m: Message, state: FSMContext):
    d = await state.get_data(); await state.clear()
    code = "BB-" + token()
    await db("POST", "products", None, {"code": code, "title": d["title"], "price": d["price"],
                                         "description": m.text.strip(), "cover_url": d["cover"]})
    me = await bot.get_me()
    await m.answer(f"✅ <b>{esc(d['title'])}</b> created as draft.\n\n"
                   f"Upload the file(s) to your private channel with this exact text as the <b>caption</b>:\n\n"
                   f"<code>https://t.me/{me.username}?start={code}</code>\n\n"
                   f"Add several files with the same caption for a course. I will link them and publish the product automatically.")

# ---------------------------------------------------------------- Bot: channel posts link files to products
@r.channel_post(F.chat.id == CHANNEL)
async def channel_post(m: Message):
    mt = CODE_RE.search((m.caption or "").upper())
    if not mt: return
    ps = await db("GET", "products", {"code": f"eq.{mt.group()}"})
    if not ps: return
    p = ps[0]
    await db("POST", "product_files", None, {"product_id": p["id"], "channel_id": m.chat.id, "message_id": m.message_id})
    if p["status"] == "draft":
        await db("PATCH", "products", {"id": f"eq.{p['id']}"}, {"status": "published"})
    n = len(await db("GET", "product_files", {"product_id": f"eq.{p['id']}", "select": "id"}))
    for a in ADMINS:
        try: await bot.send_message(a, f"📦 File {n} linked to <b>{esc(p['title'])}</b>. Product is live.")
        except Exception: pass

# ---------------------------------------------------------------- Order decision + delivery
async def decide(oid, ok, admin_id, reason=None):
    rows = await db("PATCH", "orders", {"id": f"eq.{oid}", "status": "eq.pending"},
                    {"status": "approved" if ok else "rejected", "decided_by": admin_id,
                     "decided_at": now(), "reject_reason": reason})
    if not rows: return False          # already decided -> never deliver twice
    o = rows[0]
    u = (await db("GET", "users", {"id": f"eq.{o['user_id']}"}))[0]; l = u.get("lang") or "en"
    p = (await db("GET", "products", {"id": f"eq.{o['product_id']}"}))[0]
    try:
        if ok:
            await bot.send_message(u["id"], T[l]["approved"].format(t=esc(p["title"])))
            files = await db("GET", "product_files", {"product_id": f"eq.{p['id']}", "order": "created_at"})
            for f in files:  # copy_message = no "Forwarded from" label
                await bot.copy_message(u["id"], f["channel_id"], f["message_id"],
                                       caption=esc(p["title"]), protect_content=PROTECT)
            await db("PATCH", "products", {"id": f"eq.{p['id']}"}, {"sales": p["sales"] + 1})
            await db("PATCH", "orders", {"id": f"eq.{oid}"}, {"delivered": True})
        else:
            await bot.send_message(u["id"], T[l]["rejected"].format(t=esc(p["title"]), r=esc(reason or "-")))
    except Exception as e:
        logging.warning("delivery problem for %s: %s", oid, e)
    return True

@r.callback_query(F.data.regexp(r"^(ap|rj):"), IS_ADMIN)
async def cb_decide(c: CallbackQuery):
    act, oid = c.data.split(":")
    ok = await decide(oid, act == "ap", c.from_user.id, None if act == "ap" else "Payment could not be verified")
    if not ok: return await c.answer("Already decided", show_alert=True)
    await c.message.edit_caption(caption=(c.message.caption or "") + ("\n\n✅ APPROVED" if act == "ap" else "\n\n❌ REJECTED"))
    await c.answer("Done")

# ---------------------------------------------------------------- API
@asynccontextmanager
async def life(app):
    task = None
    try:
        if BACKEND:
            await bot.set_webhook(f"{BACKEND}/webhook/{WH}", allowed_updates=dp.resolve_used_update_types())
        else:
            await bot.delete_webhook(); task = asyncio.create_task(dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types()))
        if WEBAPP.startswith("https"):
            await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="BIFA BOOKS", web_app=WebAppInfo(url=WEBAPP)))
    except Exception as e:
        logging.error("startup: %s", e)
    yield
    if task: task.cancel()

app = FastAPI(lifespan=life)
app.add_middleware(GZipMiddleware, minimum_size=800)
app.add_middleware(CORSMiddleware, allow_origins=E("CORS_ORIGINS", "*").split(","), allow_methods=["*"], allow_headers=["*"])

@app.get("/health")
async def health(): return {"ok": True}

@app.post("/webhook/{secret}")
async def webhook(secret: str, request: Request):
    if secret != WH: raise HTTPException(403)
    await dp.feed_update(bot, Update.model_validate(await request.json(), context={"bot": bot}))
    return {"ok": True}

@app.get("/api/me")
async def me(request: Request):
    u = auth(request); row = await upsert_user(u)
    return {"id": u["id"], "name": u.get("first_name"), "lang": row.get("lang"),
            "banned": row["is_banned"], "is_admin": u["id"] in ADMINS}

@app.post("/api/lang")
async def set_lang_api(request: Request):
    u = auth(request); l = (await request.json()).get("lang")
    if l not in T: raise HTTPException(400)
    await upsert_user(u); await db("PATCH", "users", {"id": f"eq.{u['id']}"}, {"lang": l}); return {"ok": True}

@app.get("/api/catalog")
async def catalog(request: Request):
    auth(request)
    ps, cs, pay, rv = await asyncio.gather(
        db("GET", "products", {"status": "eq.published", "order": "created_at.desc",
                               "select": "id,code,title,description,price,old_price,badge,cover_url,sales,category_id"}),
        db("GET", "categories", {"order": "sort"}), payment(),
        db("GET", "reviews", {"select": "product_id,rating"}))
    agg = {}
    for x in rv:
        a = agg.setdefault(x["product_id"], [0, 0]); a[0] += x["rating"]; a[1] += 1
    for p in ps:
        t, n = agg.get(p["id"], [0, 0]); p["rating"] = round(t / n, 1) if n else 0; p["reviews"] = n
    return {"products": ps, "categories": cs, "payment": pay}

@app.get("/api/my/orders")
async def my_orders(request: Request):
    u = auth(request)
    return await db("GET", "orders", {"user_id": f"eq.{u['id']}", "order": "created_at.desc",
                                      "select": "code,status,price,method,reject_reason,created_at,product_id,products(title,cover_url)"})

@app.post("/api/orders")
async def create_order(request: Request, product_id: str = Form(...), method: str = Form(...),
                       payer_info: str = Form(...), screenshot: UploadFile = File(...)):
    u = auth(request); row = await upsert_user(u)
    if row["is_banned"]: raise HTTPException(403, "Account blocked")
    if method not in await payment(): raise HTTPException(400, "Unknown payment method")
    payer_info = payer_info.strip()[:120]
    if len(payer_info) < 3: raise HTTPException(400, "Enter the bank name or phone number you paid with")
    ps = await db("GET", "products", {"id": f"eq.{product_id}", "status": "eq.published"})
    if not ps: raise HTTPException(404, "Product not found")
    if await db("GET", "orders", {"user_id": f"eq.{u['id']}", "product_id": f"eq.{product_id}", "status": "in.(pending,approved)"}):
        raise HTTPException(409, "You already ordered this product")
    data = await screenshot.read()
    if len(data) > 8_000_000 or not (screenshot.content_type or "").startswith("image/"):
        raise HTTPException(400, "Upload an image under 8 MB")
    code = "ORD-" + token(); path = f"{u['id']}/{code}"
    await put_file("payments", path, data, screenshot.content_type)
    p = ps[0]
    o = (await db("POST", "orders", None, {"code": code, "user_id": u["id"], "product_id": p["id"], "price": p["price"],
                                           "method": method, "payer_info": payer_info, "screenshot_path": path}))[0]
    cap = (f"🧾 <b>{code}</b>\n📦 {esc(p['title'])}\n💰 {p['price']:g} ETB\n🏦 {method.upper()} · {esc(payer_info)}\n"
           f"👤 {esc(u.get('first_name') or '')} @{u.get('username') or '-'} (<code>{u['id']}</code>)")
    kb = Kb(inline_keyboard=[[Btn(text="✅ Approve", callback_data=f"ap:{o['id']}"), Btn(text="❌ Reject", callback_data=f"rj:{o['id']}")]])
    for a in ADMINS:
        try: await bot.send_photo(a, BufferedInputFile(data, "payment.jpg"), caption=cap, reply_markup=kb)
        except Exception as e: logging.warning("notify admin %s: %s", a, e)
    return {"code": code, "status": "pending"}

# ---- admin API
@app.get("/api/admin/orders")
async def a_orders(request: Request, status: str = "pending"):
    admin(request)
    rows = await db("GET", "orders", {"status": f"eq.{status}", "order": "created_at.desc", "limit": "60",
                                      "select": "*,products(title,cover_url),users(first_name,username)"})
    urls = await asyncio.gather(*[signed(o["screenshot_path"]) for o in rows])
    for o, url in zip(rows, urls): o["screenshot_url"] = url
    return rows

@app.post("/api/admin/orders/{oid}/decide")
async def a_decide(oid: str, request: Request):
    a = admin(request); b = await request.json()
    if not await decide(oid, bool(b.get("approve")), a["id"], (b.get("reason") or "").strip()[:200] or None):
        raise HTTPException(409, "Order already decided")
    return {"ok": True}

@app.get("/api/admin/products")
async def a_products(request: Request):
    admin(request)
    return await db("GET", "products", {"order": "created_at.desc", "select": "*,product_files(id)"})

@app.patch("/api/admin/products/{pid}")
async def a_product_edit(pid: str, request: Request):
    admin(request); b = await request.json()
    patch = {k: b[k] for k in ("title", "description", "price", "status", "category_id", "old_price", "badge") if k in b}
    for k in ("old_price", "badge"):
        if patch.get(k) in ("", 0): patch[k] = None
    if patch.get("status") not in (None, "draft", "published", "archived"): raise HTTPException(400)
    if patch.get("badge") not in (None, "new", "launch"): raise HTTPException(400)
    return await db("PATCH", "products", {"id": f"eq.{pid}"}, patch)

@app.delete("/api/admin/products/{pid}")
async def a_product_del(pid: str, request: Request):
    admin(request); await db("DELETE", "products", {"id": f"eq.{pid}"}); return {"ok": True}

@app.get("/api/admin/stats")
async def a_stats(request: Request):
    admin(request)
    orders, users = await asyncio.gather(
        db("GET", "orders", {"select": "status,price,created_at,products(title)"}),
        db("GET", "users", {"select": "id"}))
    ok = [o for o in orders if o["status"] == "approved"]
    day = lambda o: o["created_at"][:10]
    today = datetime.now(timezone.utc).date()
    last7 = [(today - timedelta(days=i)).isoformat() for i in range(6, -1, -1)]
    top = {}
    for o in ok:
        t = (o.get("products") or {}).get("title", "?"); top[t] = top.get(t, 0) + 1
    return {"revenue": sum(float(o["price"]) for o in ok),
            "today": sum(float(o["price"]) for o in ok if day(o) == today.isoformat()),
            "customers": len(users),
            "counts": {s: sum(1 for o in orders if o["status"] == s) for s in ("pending", "approved", "rejected")},
            "series": [{"d": d, "v": sum(float(o["price"]) for o in ok if day(o) == d)} for d in last7],
            "top": sorted(top.items(), key=lambda x: -x[1])[:5]}

@app.put("/api/admin/payment")
async def a_payment(request: Request):
    admin(request); v = await request.json()
    await db("POST", "settings", {"on_conflict": "key"}, {"key": "payment", "value": v}, "resolution=merge-duplicates"); return {"ok": True}

@app.post("/api/admin/broadcast")
async def a_broadcast(request: Request):
    admin(request); text = ((await request.json()).get("text") or "").strip()
    if not text: raise HTTPException(400, "Write a message")
    users = await db("GET", "users", {"is_banned": "eq.false", "select": "id"})
    async def run():
        for u in users:
            try: await bot.send_message(u["id"], text)
            except Exception: pass
            await asyncio.sleep(0.05)
    asyncio.create_task(run()); return {"queued": len(users)}

# ---- reviews (only customers with an approved order can review)
@app.get("/api/reviews/{pid}")
async def get_reviews(pid: str, request: Request):
    auth(request)
    return await db("GET", "reviews", {"product_id": f"eq.{pid}", "order": "created_at.desc", "limit": "20",
                                       "select": "id,name,rating,text,created_at"})

@app.post("/api/reviews")
async def post_review(request: Request):
    u = auth(request); b = await request.json()
    pid, rating = b.get("product_id"), int(b.get("rating", 0))
    if rating < 1 or rating > 5: raise HTTPException(400, "Choose 1-5 stars")
    if not await db("GET", "orders", {"user_id": f"eq.{u['id']}", "product_id": f"eq.{pid}", "status": "eq.approved"}):
        raise HTTPException(403, "Only customers who bought this product can review it")
    row = {"product_id": pid, "user_id": u["id"], "name": (u.get("first_name") or "Customer")[:40],
           "rating": rating, "text": (b.get("text") or "").strip()[:500]}
    await db("POST", "reviews", {"on_conflict": "product_id,user_id"}, row, "resolution=merge-duplicates,return=representation")
    return {"ok": True}

@app.get("/api/admin/reviews")
async def a_reviews(request: Request):
    admin(request)
    return await db("GET", "reviews", {"order": "created_at.desc", "limit": "100",
                                       "select": "id,name,rating,text,created_at,products(title)"})

@app.delete("/api/admin/reviews/{rid}")
async def a_review_del(rid: str, request: Request):
    admin(request); await db("DELETE", "reviews", {"id": f"eq.{rid}"}); return {"ok": True}

# ---- serve the frontend from the same server (must stay LAST)
app.mount("/", StaticFiles(directory=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "frontend"), html=True), name="frontend")