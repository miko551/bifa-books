# BIFA BOOKS - Telegram Mini App digital store

backend/   FastAPI + aiogram bot (one process)  -> Render
frontend/  index.html (customers) + admin.html (admin) -> Vercel (static, no build)
Supabase   Postgres + Storage (covers public, payments private)

## 1. Supabase
1. New project -> SQL Editor -> run backend/schema.sql
2. Settings > API: copy Project URL and the service_role key
3. Admin dashboard > Settings: set your real CBE / Awash / Telebirr accounts

## 2. Telegram
1. @BotFather: /newbot -> BOT_TOKEN
2. Create a PRIVATE channel, add the bot as admin (can post + read messages). Get the channel id (-100...)
3. Get your own Telegram id (e.g. @userinfobot) -> ADMIN_IDS

## 3. Local test (VS Code)
    cd backend && python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
    pip install -r requirements.txt
    cp .env.example .env   # fill it; keep BACKEND_URL empty (polling mode)
    set -a; source .env; set +a
    uvicorn main:app --reload --port 8000
Mini Apps need HTTPS. Serve the frontend and expose it:
    cd frontend && python -m http.server 5500
    cloudflared tunnel --url http://localhost:5500      # or: ngrok http 5500
Put the https URL in WEBAPP_URL. The API also needs HTTPS for Telegram, so tunnel port 8000 too and put that URL in frontend/config.js.
Browser-only test: DEV_MODE=1 and open  http://localhost:5500/?dev=<your telegram id>

## 4. Deploy
- Render (Web Service, root dir backend): build `pip install -r requirements.txt`, start `uvicorn main:app --host 0.0.0.0 --port $PORT`.
  Add all .env values; BACKEND_URL = your Render URL; DEV_MODE=0; CORS_ORIGINS = your Vercel URL.
  Free plan sleeps: ping /health every 10 min (UptimeRobot) so orders and webhooks are not delayed.
- Vercel: import the frontend folder (no build). Edit config.js -> window.API = your Render URL.
- BotFather: /setdomain not needed; the bot sets the menu button itself from WEBAPP_URL.

## 5. Daily use
/addproduct -> cover, title, price, description -> bot returns a link -> upload file(s) to the private channel with that link as caption -> product goes live.
Payments arrive in the bot (screenshot + Approve/Reject buttons) and in admin.html. Approve = files copied to the customer (no "forwarded from" tag).
