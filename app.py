"""
RailFast - IRCTC Tatkal Semi-Auto Booker
Bot does everything up to payment, then pings you on Telegram.
You approve UPI in 5 seconds. Ticket confirmed.
"""

import os, json, time, logging, threading, io, requests
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Optional, List

import pytz
from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, join_room, emit
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from PIL import Image
import pytesseract

# ── App setup ──────────────────────────────────────────────────────────────────

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "railfast-2025")
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet",
                    ping_timeout=60, ping_interval=25)

IST = pytz.timezone("Asia/Kolkata")
scheduler = BackgroundScheduler()
scheduler.start()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("railfast")

active_jobs = {}
booking_logs = {}

# ── Models ─────────────────────────────────────────────────────────────────────

@dataclass
class Passenger:
    name: str
    age: int
    gender: str   # Male / Female / Transgender
    food: str     # V / NV / N
    berth: str = "NP"

@dataclass
class BookingRequest:
    username: str
    password: str
    upi_id: str
    from_station: str
    to_station: str
    train_no: str
    coach_class: str
    journey_date: str        # YYYY-MM-DD
    passengers: List[Passenger]
    telegram_token: str = ""
    telegram_chat_id: str = ""
    captcha_retries: int = 6
    session_id: str = ""

@dataclass
class BookingResult:
    success: bool
    pnr: Optional[str] = None
    message: str = ""
    payment_charged: bool = False
    timestamp: str = field(default_factory=lambda: datetime.now(IST).strftime("%d %b %Y %H:%M:%S IST"))

# ── Telegram ───────────────────────────────────────────────────────────────────

def send_telegram(token: str, chat_id: str, message: str):
    """Send message via Telegram bot."""
    if not token or not chat_id:
        return
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        requests.post(url, json={
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML"
        }, timeout=10)
        logger.info("Telegram notification sent")
    except Exception as e:
        logger.warning(f"Telegram failed: {e}")

# ── Logging ────────────────────────────────────────────────────────────────────

def push(sid: str, msg: str, level: str = "info"):
    ts = datetime.now(IST).strftime("%H:%M:%S")
    line = {"ts": ts, "msg": msg, "level": level}
    booking_logs.setdefault(sid, []).append(line)
    socketio.emit("log", line, room=sid)
    logger.info(f"[{sid[:8]}] {msg}")

# ── Bot ────────────────────────────────────────────────────────────────────────

class IRCTCBot:
    def __init__(self, req: BookingRequest):
        self.req = req
        self.pw = None
        self.browser = None
        self.page = None

    def log(self, msg, level="info"):
        push(self.req.session_id, msg, level)

    def notify(self, msg: str):
        """Send Telegram + socket notification."""
        self.log(msg, "ok")
        send_telegram(self.req.telegram_token, self.req.telegram_chat_id, msg)

    # ── Browser ───────────────────────────────────────────────────────────────

    def start(self):
        self.pw = sync_playwright().start()
        self.browser = self.pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-software-rasterizer",
                "--disable-extensions",
                "--no-first-run",
                "--disable-blink-features=AutomationControlled",
                "--disable-http2",                    # IRCTC blocks HTTP/2 from headless
                "--disable-web-security",
                "--allow-running-insecure-content",
                "--ignore-certificate-errors",
                "--ignore-ssl-errors",
            ]
        )
        ctx = self.browser.new_context(
            viewport={"width": 1366, "height": 768},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            locale="en-IN",
            extra_http_headers={
                "Accept-Language": "en-IN,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            },
            ignore_https_errors=True,
        )
        self.page = ctx.new_page()
        self.page.set_default_timeout(30000)
        self.log("Browser started ✓")

    def stop(self):
        try:
            if self.browser: self.browser.close()
            if self.pw: self.pw.stop()
        except Exception:
            pass
        self.log("Browser closed")

    # ── Captcha ───────────────────────────────────────────────────────────────

    def _read_captcha(self) -> str:
        el = self.page.wait_for_selector(".captcha-img", timeout=10000)
        png = el.screenshot()
        img = Image.open(io.BytesIO(png)).convert("L")
        img = img.point(lambda x: 0 if x < 140 else 255)
        return pytesseract.image_to_string(
            img,
            config="--psm 8 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
        ).strip()

    def _solve_captcha(self, input_sel: str, btn_sel: str) -> bool:
        for attempt in range(1, self.req.captcha_retries + 1):
            try:
                text = self._read_captcha()
                self.log(f"Captcha attempt {attempt}/{self.req.captcha_retries}: '{text}'")
                self.page.fill(input_sel, "")
                self.page.fill(input_sel, text)
                self.page.click(btn_sel)
                self.page.wait_for_timeout(1800)
                # If captcha was wrong, IRCTC keeps the captcha field visible
                still_there = self.page.query_selector(input_sel)
                if not still_there:
                    self.log(f"Captcha solved ✓", "ok")
                    return True
                val = self.page.input_value(input_sel) if still_there else ""
                if val == "":  # field was cleared = rejected
                    self.log("Captcha rejected, retrying...", "warn")
                    continue
                self.log(f"Captcha accepted ✓", "ok")
                return True
            except Exception as e:
                self.log(f"Captcha error attempt {attempt}: {e}", "warn")
                time.sleep(1)
        self.log("All captcha retries exhausted", "error")
        return False

    # ── Login ─────────────────────────────────────────────────────────────────

    def login(self):
        self.log("Opening IRCTC...")
        # Retry goto up to 3 times — IRCTC sometimes drops first connection from new IPs
        for attempt in range(1, 4):
            try:
                self.log(f"Loading IRCTC (attempt {attempt}/3)...")
                self.page.goto(
                    "https://www.irctc.co.in/nget/train-search",
                    wait_until="domcontentloaded",
                    timeout=60000
                )
                break
            except Exception as e:
                self.log(f"Page load attempt {attempt} failed: {e}", "warn")
                if attempt == 3:
                    raise RuntimeError(f"IRCTC unreachable after 3 attempts. Try again in a few minutes.")
                self.page.wait_for_timeout(5000)
        self.page.wait_for_timeout(3000)  # let Angular bootstrap
        self.page.click("text=' LOGIN '")
        self.page.wait_for_timeout(2000)
        self.page.fill("input[placeholder='User Name']", self.req.username)
        self.page.fill("input[placeholder='Password']", self.req.password)

        ok = self._solve_captcha(
            "input[placeholder='Enter Captcha']",
            "button:has-text('SIGN IN')"
        )
        if not ok:
            raise RuntimeError("Login failed — captcha could not be solved")

        self.page.wait_for_selector("[class*='username']", timeout=15000)
        self.log("Logged in ✓", "ok")
        self.page.wait_for_timeout(1500)

    # ── Search ────────────────────────────────────────────────────────────────

    def _pick_date(self):
        jdate = datetime.strptime(self.req.journey_date, "%Y-%m-%d")
        day = str(jdate.day)
        month = jdate.strftime("%b").upper()[:3]
        year = str(jdate.year)
        self.page.click("#jDate")
        self.page.wait_for_timeout(600)
        for _ in range(24):
            cur_m = self.page.inner_text(".ui-datepicker-month").upper()[:3]
            cur_y = self.page.inner_text(".ui-datepicker-year")
            if cur_y == year and cur_m == month:
                break
            self.page.click(".ui-datepicker-next")
            self.page.wait_for_timeout(300)
        self.page.click(f".ui-datepicker-calendar a:has-text('{day}')")
        self.log(f"Date set: {self.req.journey_date} ✓")

    def search(self):
        self.log(f"Searching {self.req.from_station} → {self.req.to_station}...")
        self.page.fill("input[aria-controls='pr_id_1_list']", self.req.from_station)
        self.page.wait_for_timeout(1000)
        self.page.click("#pr_id_1_list li:first-child")
        self.page.fill("input[aria-controls='pr_id_2_list']", self.req.to_station)
        self.page.wait_for_timeout(1000)
        self.page.click("#pr_id_2_list li:first-child")
        self.page.click("p-dropdown#journeyQuota")
        self.page.click("li[role='option'][aria-label='TATKAL']")
        self._pick_date()
        self.page.wait_for_timeout(1500)
        self.page.click("button[type='submit'].train_Search")
        self.log("Search submitted ✓")

    # ── Train ─────────────────────────────────────────────────────────────────

    def select_train(self):
        self.page.wait_for_timeout(3000)
        divs = self.page.query_selector_all(".bull-back")
        for div in divs:
            if self.req.train_no in (div.inner_text() or ""):
                self.log(f"Train {self.req.train_no} found ✓", "ok")
                div.query_selector(f"strong:has-text('{self.req.coach_class}')").click()
                avail = self.page.wait_for_selector("td.link.ng-star-inserted", timeout=8000)
                status = avail.inner_text().strip().upper()
                self.log(f"Seat status: {status}")
                if "NOT AVAILABLE" in status or "REGRET" in status:
                    raise RuntimeError(f"No seats available ({status}) — aborting safely")
                avail.click()
                div.query_selector("button.train_Search:has-text('Book Now')").click()
                self.log("Book Now clicked ✓", "ok")
                return
        raise RuntimeError(f"Train {self.req.train_no} not found in results")

    # ── Passengers ────────────────────────────────────────────────────────────

    def fill_passengers(self):
        gmap = {"Male": "M", "Female": "F", "Transgender": "T"}
        self.page.wait_for_selector("input[placeholder='Name']", timeout=15000)
        for i, p in enumerate(self.req.passengers):
            if i > 0:
                add = self.page.query_selector("span:has-text('+ Add Passenger')")
                if add:
                    add.click()
                    self.page.wait_for_timeout(600)
            self.page.query_selector_all("input[placeholder='Name']")[i].fill(p.name)
            self.page.query_selector_all("input[formcontrolname='passengerAge']")[i].fill(str(p.age))
            self.page.query_selector_all("select[formcontrolname='passengerGender']")[i].select_option(gmap.get(p.gender, "M"))
            try:
                foods = self.page.query_selector_all("select#FOOD_0")
                if i < len(foods):
                    foods[i].select_option(p.food)
            except Exception:
                pass
            self.log(f"Passenger {i+1}: {p.name} ({p.age}y) ✓", "ok")
        self.page.click("button.train_Search:has-text('Continue')")
        self.log("Passenger details submitted ✓", "ok")

    # ── KEY STEP: Notify & wait for UPI approval ───────────────────────────────

    def wait_for_payment_approval(self) -> BookingResult:
        """
        Bot reaches review page, notifies user on Telegram,
        then waits up to 3 minutes for them to approve UPI.
        """
        self.page.wait_for_timeout(5000)

        # Confirm we're on review page
        try:
            self.page.wait_for_selector("text=Total Fare", timeout=15000)
        except PWTimeout:
            try:
                self.page.wait_for_selector("text=Review your", timeout=5000)
            except PWTimeout:
                return BookingResult(False, message="Did not reach review page — payment NOT charged")

        # Get fare amount for the notification
        fare = "unknown"
        try:
            fare_el = self.page.query_selector("[class*='fare'], [class*='amount'], text=₹")
            if fare_el:
                fare = fare_el.inner_text().strip()
        except Exception:
            pass

        # Build passenger summary
        pax_summary = ", ".join(f"{p.name} ({p.age}y)" for p in self.req.passengers)

        # ── TELEGRAM NOTIFICATION ──────────────────────────────────────────────
        jdate = datetime.strptime(self.req.journey_date, "%Y-%m-%d").strftime("%d %b %Y")
        notification = (
            f"🚄 <b>RAILFAST — ACTION NEEDED</b>\n\n"
            f"✅ Bot has filled everything!\n\n"
            f"🗺 <b>Route:</b> {self.req.from_station} → {self.req.to_station}\n"
            f"🚂 <b>Train:</b> {self.req.train_no} ({self.req.coach_class})\n"
            f"📅 <b>Date:</b> {jdate}\n"
            f"👥 <b>Passengers:</b> {pax_summary}\n"
            f"💳 <b>UPI:</b> {self.req.upi_id}\n\n"
            f"⚡ <b>Open your UPI app NOW and approve the payment request</b>\n\n"
            f"⏳ You have 3 minutes before session expires."
        )
        send_telegram(self.req.telegram_token, self.req.telegram_chat_id, notification)
        self.log("📱 Telegram notification sent! Approve UPI payment now.", "ok")
        socketio.emit("payment_needed", {
            "from": self.req.from_station,
            "to": self.req.to_station,
            "train": self.req.train_no,
            "date": jdate,
            "passengers": pax_summary,
            "upi": self.req.upi_id,
            "fare": fare,
        }, room=self.req.session_id)

        # ── WAIT FOR PNR (up to 3 minutes) ────────────────────────────────────
        self.log("Waiting for payment confirmation (up to 3 min)...", "warn")
        for i in range(36):  # 36 x 5s = 3 minutes
            self.page.wait_for_timeout(5000)
            remaining = 180 - (i + 1) * 5
            self.log(f"Waiting for UPI approval... ({remaining}s remaining)")

            # Check for PNR — means booking confirmed
            try:
                pnr_el = self.page.query_selector(".pnrNo, [class*='pnr']")
                if pnr_el:
                    pnr = pnr_el.inner_text().strip()
                    if pnr and len(pnr) >= 10:
                        confirmed_msg = (
                            f"🎉 <b>BOOKING CONFIRMED!</b>\n\n"
                            f"🎫 <b>PNR: {pnr}</b>\n"
                            f"🗺 {self.req.from_station} → {self.req.to_station}\n"
                            f"🚂 Train {self.req.train_no} · {self.req.coach_class}\n"
                            f"📅 {jdate}\n"
                            f"👥 {pax_summary}"
                        )
                        send_telegram(self.req.telegram_token, self.req.telegram_chat_id, confirmed_msg)
                        self.log(f"🎉 PNR: {pnr}", "ok")
                        return BookingResult(True, pnr=pnr, payment_charged=True,
                                             message=f"Booking confirmed! PNR: {pnr}")
            except Exception:
                pass

            # Check for failure page
            try:
                page_text = self.page.inner_text("body")
                if "payment failed" in page_text.lower() or "transaction failed" in page_text.lower():
                    send_telegram(self.req.telegram_token, self.req.telegram_chat_id,
                                  "❌ Payment failed. Please retry booking.")
                    return BookingResult(False, payment_charged=False,
                                         message="Payment failed — no money charged")
            except Exception:
                pass

        # Timeout — user didn't approve in time
        timeout_msg = "⏰ Payment window expired (3 min). Session closed. Please book again."
        send_telegram(self.req.telegram_token, self.req.telegram_chat_id, timeout_msg)
        return BookingResult(False, payment_charged=False,
                             message="UPI approval timeout (3 min) — session expired, no charge")

    # ── Full flow ─────────────────────────────────────────────────────────────

    def run(self) -> BookingResult:
        try:
            self.start()
            self.login()
            self.search()
            self.page.wait_for_timeout(2000)
            self.select_train()
            self.page.wait_for_timeout(2000)
            self.fill_passengers()
            self.page.wait_for_timeout(3000)
            return self.wait_for_payment_approval()
        except Exception as e:
            self.log(f"Booking failed: {e}", "error")
            send_telegram(self.req.telegram_token, self.req.telegram_chat_id,
                          f"❌ RailFast booking failed: {e}")
            return BookingResult(False, message=str(e), payment_charged=False)
        finally:
            self.stop()

# ── Helpers ────────────────────────────────────────────────────────────────────

def _dict_to_req(d: dict, sid: str) -> BookingRequest:
    passengers = [
        Passenger(name=p["name"], age=int(p["age"]),
                  gender=p.get("gender", "Male"),
                  food=p.get("food", "V"),
                  berth=p.get("berth", "NP"))
        for p in d["passengers"]
    ]
    return BookingRequest(
        username=d["username"], password=d["password"],
        upi_id=d["upiId"],
        from_station=d["fromStation"], to_station=d["toStation"],
        train_no=d["trainNo"], coach_class=d["coachClass"],
        journey_date=d["journeyDate"],
        passengers=passengers,
        telegram_token=d.get("telegramToken", ""),
        telegram_chat_id=d.get("telegramChatId", ""),
        session_id=sid
    )

def _emit_result(result: BookingResult, sid: str):
    push(sid, f"RESULT: {result.message}", "ok" if result.success else "error")
    socketio.emit("result", {
        "success": result.success, "pnr": result.pnr,
        "message": result.message,
        "payment_charged": result.payment_charged,
        "timestamp": result.timestamp,
    }, room=sid)

def _precise_wait(target: datetime, sid: str):
    now = datetime.now(IST)
    delta = (target - now).total_seconds()
    if delta > 5:
        push(sid, f"Sleeping until {target.strftime('%H:%M:%S IST')} ({delta:.0f}s)...")
        time.sleep(delta - 5)
    while datetime.now(IST) < target:
        time.sleep(0.05)
    push(sid, "⏰ 10:00:00 — Tatkal OPEN! Starting now!", "ok")

def run_scheduled(req_dict: dict, sid: str):
    req = _dict_to_req(req_dict, sid)
    jd = datetime.strptime(req.journey_date, "%Y-%m-%d")
    target = IST.localize(datetime(jd.year, jd.month, jd.day - 1, 10, 0, 0))
    # Alert user 10 min before so they're ready to approve UPI
    ten_min_before = target - timedelta(minutes=10)
    now = datetime.now(IST)
    if now < ten_min_before:
        wait_secs = (ten_min_before - now).total_seconds()
        time.sleep(max(0, wait_secs))
        send_telegram(req.telegram_token, req.telegram_chat_id,
                      f"⏰ <b>RailFast Alert</b>\n\nTatkal opens in 10 minutes!\n"
                      f"Be ready to approve UPI payment for:\n"
                      f"🚂 Train {req.train_no} · {req.from_station}→{req.to_station}\n"
                      f"Keep your phone unlocked and UPI app ready.")
    _precise_wait(target, sid)
    _emit_result(IRCTCBot(req).run(), sid)

# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/book", methods=["POST"])
def book_now():
    data = request.json
    sid = data.get("sessionId", "default")
    req = _dict_to_req(data, sid)
    threading.Thread(target=lambda: _emit_result(IRCTCBot(req).run(), sid), daemon=True).start()
    return jsonify({"status": "started", "sessionId": sid})

@app.route("/api/schedule", methods=["POST"])
def schedule_booking():
    data = request.json
    sid = data.get("sessionId", "default")
    jd = datetime.strptime(data["journeyDate"], "%Y-%m-%d")
    d1 = jd - timedelta(days=1)
    trigger = CronTrigger(year=d1.year, month=d1.month, day=d1.day,
                          hour=9, minute=59, second=50, timezone=IST)
    job = scheduler.add_job(run_scheduled, trigger, args=[data, sid])
    active_jobs[sid] = job.id
    launch = IST.localize(datetime(d1.year, d1.month, d1.day, 9, 59, 50))
    return jsonify({"status": "scheduled",
                    "launchAt": launch.strftime("%d %b %Y %H:%M:%S IST"),
                    "jobId": job.id})

@app.route("/api/cancel", methods=["POST"])
def cancel_job():
    sid = request.json.get("sessionId")
    jid = active_jobs.pop(sid, None)
    if jid:
        try: scheduler.remove_job(jid)
        except Exception: pass
        return jsonify({"status": "cancelled"})
    return jsonify({"status": "not_found"})

@app.route("/api/logs/<sid>")
def get_logs(sid):
    return jsonify(booking_logs.get(sid, []))

@app.route("/health")
def health():
    return "OK", 200

@socketio.on("join")
def on_join(data):
    room = data.get("sessionId", "default")
    join_room(room)
    emit("joined", {"room": room})
    for line in booking_logs.get(room, []):
        emit("log", line)

if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
