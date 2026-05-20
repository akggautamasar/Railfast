# RailFast — IRCTC Tatkal Auto-Booker

Deploy on Render, open on Android. Fill once, books automatically at 10:00:00 AM IST.

---

## Deploy in 5 minutes (Render)

### Step 1 — Push to GitHub
```bash
git init
git add .
git commit -m "RailFast initial"
# Create a repo on github.com, then:
git remote add origin https://github.com/YOUR_USERNAME/railfast.git
git push -u origin main
```

### Step 2 — Deploy on Render
1. Go to https://render.com → New → Web Service
2. Connect your GitHub repo
3. Settings:
   - **Environment**: Python
   - **Build Command**: `bash build.sh`
   - **Start Command**: `gunicorn --worker-class eventlet -w 1 --timeout 300 --bind 0.0.0.0:$PORT app:app`
   - **Region**: Singapore (closest to India)
   - **Plan**: Starter ($7/mo) — **NOT free tier** (free tier sleeps after 15 min inactivity, scheduler won't fire!)
4. Add env var: `SECRET_KEY` = any random string
5. Click **Create Web Service**

Build takes ~5 minutes (installing Chrome + Tesseract).

### Step 3 — Open on Android
- Open your Render URL in Chrome on Android
- Tap ⋮ → "Add to Home Screen"
- Works like a native app, runs on the server 24/7

---

## How it works

```
Your Android (fills form once)
        ↓
Render Server (runs 24/7)
        ↓ at 9:59:50 AM IST D-1
Chrome (headless) → IRCTC Website
        ↓ login → search → select class
        ↓ fill passengers → captcha (auto OCR, 5 retries)
        ↓ review page → UPI payment
        ↓
Live logs stream to your phone via WebSocket
```

---

## Safety notes

- **auto_pay = OFF by default** → bot fills everything but stops at review page. You pay manually in IRCTC app.
- **auto_pay = ON** → fully automated including payment. Use only if you trust the bot.
- Payment is NEVER initiated if seats are unavailable (bot checks before proceeding).
- Captcha retries 5 times before aborting (never charges if captcha fails).

---

## Folder structure

```
railfast/
├── app.py              ← Flask server + booking engine
├── templates/
│   └── index.html      ← Mobile PWA frontend
├── requirements.txt
├── build.sh            ← Installs Chrome + Tesseract on Render
├── render.yaml
├── Procfile
└── README.md
```

---

## Common issues

| Issue | Fix |
|-------|-----|
| Build fails on Chrome install | Render's Ubuntu version changed — update `build.sh` apt packages |
| Captcha always fails | IRCTC updated captcha class name — update `By.CLASS_NAME, "captcha-img"` in `app.py` |
| Train not found | Check train number and coach class string exactly as shown on IRCTC |
| Scheduler doesn't fire | Must use Starter plan — free tier sleeps |
| UPI field not found | IRCTC payment page XPath changed — update `handle_payment()` |

---

## Updating IRCTC selectors

IRCTC sometimes changes their frontend. If booking breaks, inspect element on IRCTC.co.in and update the XPaths in `app.py` in the `IRCTCBot` class.
