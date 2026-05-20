"""
RailFast - IRCTC Tatkal Auto-Booker
Uses Playwright (async) for reliable headless Chrome in containers.
"""

import os, json, time, logging, threading, io, asyncio
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

# ── App ────────────────────────────────────────────────────────────────────────

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
    gender: str      # Male / Female / Transgender
    food: str        # V / NV / N
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
    journey_date: str   # YYYY-MM-DD
    passengers: List[Passenger]
    auto_pay: bool = False
    captcha_retries: int = 5
    session_id: str = ""

@dataclass
class BookingResult:
    success: bool
    pnr: Optional[str] = None
    message: str = ""
    payment_charged: bool = False
    timestamp: str = field(default_factory=lambda: datetime.now(IST).strftime("%d %b %Y %H:%M:%S IST"))

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
            ]
        )
        context = self.browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            locale="en-IN",
        )
        self.page = context.new_page()
        self.page.set_default_timeout(20000)
        self.log("Browser started (Playwright/Chromium)")

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
        text = pytesseract.image_to_string(
            img,
            config="--psm 8 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
        ).strip()
        return text

    def _solve_captcha(self, input_sel: str, btn_sel: str, error_check: str = "captcha") -> bool:
        for attempt in range(1, self.req.captcha_retries + 1):
            try:
                text = self._read_captcha()
                self.log(f"Captcha attempt {attempt}: '{text}'")
                self.page.fill(input_sel, text)
                self.page.click(btn_sel)
                self.page.wait_for_timeout(1500)
                # Check for error
                errors = self.page.query_selector_all(f"[class*='error']:has-text('{error_check}')")
                if not errors:
                    self.log(f"Captcha accepted ✓", "ok")
                    return True
                self.log("Captcha rejected, retrying...", "warn")
            except Exception as e:
                self.log(f"Captcha attempt {attempt} error: {e}", "warn")
                time.sleep(1)
        self.log("All captcha attempts failed", "error")
        return False

    # ── Login ─────────────────────────────────────────────────────────────────

    def login(self):
        self.log("Opening IRCTC...")
        self.page.goto("https://www.irctc.co.in/nget/train-search", wait_until="networkidle")
        self.page.click("text=' LOGIN '")
        self.page.wait_for_timeout(2000)
        self.page.fill("input[placeholder='User Name']", self.req.username)
        self.page.fill("input[placeholder='Password']", self.req.password)

        ok = self._solve_captcha(
            "input[placeholder='Enter Captcha']",
            "button:has-text('SIGN IN')",
            "captcha"
        )
        if not ok:
            raise RuntimeError("Login failed — captcha unsolvable after retries")

        # Wait for login confirmation
        self.page.wait_for_selector("[class*='username']", timeout=15000)
        self.log("Logged in ✓", "ok")
        self.page.wait_for_timeout(1500)

    # ── Journey ───────────────────────────────────────────────────────────────

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
        self.log(f"Date set: {self.req.journey_date}")

    def search(self):
        self.log(f"Searching {self.req.from_station} → {self.req.to_station}")

        # From station
        self.page.fill("input[aria-controls='pr_id_1_list']", self.req.from_station)
        self.page.wait_for_timeout(1000)
        self.page.click("#pr_id_1_list li:first-child")

        # To station
        self.page.fill("input[aria-controls='pr_id_2_list']", self.req.to_station)
        self.page.wait_for_timeout(1000)
        self.page.click("#pr_id_2_list li:first-child")

        # TATKAL quota
        self.page.click("p-dropdown#journeyQuota")
        self.page.click(f"li[role='option'][aria-label='TATKAL']")

        self._pick_date()
        self.page.wait_for_timeout(1500)

        self.page.click("button[type='submit'].train_Search")
        self.log("Searching trains...")

    # ── Train ─────────────────────────────────────────────────────────────────

    def select_train(self):
        self.page.wait_for_timeout(3000)
        divs = self.page.query_selector_all(".bull-back")

        for div in divs:
            if self.req.train_no in (div.inner_text() or ""):
                self.log(f"Train {self.req.train_no} found ✓", "ok")
                # Click class
                div.query_selector(f"strong:has-text('{self.req.coach_class}')").click()

                avail = self.page.wait_for_selector(
                    "td.link.ng-star-inserted", timeout=8000
                )
                status = avail.inner_text().strip().upper()
                self.log(f"Availability: {status}")

                if "NOT AVAILABLE" in status or "REGRET" in status:
                    raise RuntimeError(f"No seats ({status}) — aborting, payment NOT initiated")

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

            names = self.page.query_selector_all("input[placeholder='Name']")
            names[i].fill(p.name)

            ages = self.page.query_selector_all("input[formcontrolname='passengerAge']")
            ages[i].fill(str(p.age))

            genders = self.page.query_selector_all("select[formcontrolname='passengerGender']")
            genders[i].select_option(gmap.get(p.gender, "M"))

            try:
                foods = self.page.query_selector_all("select#FOOD_0")
                if i < len(foods):
                    foods[i].select_option(p.food)
            except Exception:
                pass

            self.log(f"Passenger {i+1}: {p.name}, {p.age}y ✓", "ok")

        self.page.click("button.train_Search:has-text('Continue')")
        self.log("Passengers submitted ✓", "ok")

    # ── Payment ───────────────────────────────────────────────────────────────

    def handle_payment(self) -> BookingResult:
        self.page.wait_for_timeout(5000)

        try:
            self.page.wait_for_selector(
                "text=Total Fare, text=Review your",
                timeout=15000
            )
            self.log("Review page loaded ✓", "ok")
        except PWTimeout:
            return BookingResult(False, message="Could not reach review page — payment NOT charged")

        if not self.req.auto_pay:
            self.log("auto_pay=OFF — stopping before payment. Pay manually in IRCTC app.", "warn")
            return BookingResult(True, message="Reached review page. Complete payment manually in IRCTC.")

        # Select UPI
        self.log("Selecting UPI payment...")
        try:
            self.page.click("text=UPI", timeout=8000)
            self.page.wait_for_timeout(800)
            upi_field = self.page.query_selector("input[placeholder*='UPI'], input[placeholder*='upi'], input[name*='upi']")
            if upi_field:
                upi_field.fill(self.req.upi_id)
                self.log(f"UPI entered: {self.req.upi_id} ✓", "ok")
        except Exception as e:
            self.log(f"UPI auto-fill skipped: {e}", "warn")

        # Final captcha
        ok = self._solve_captcha(
            "input[placeholder='Enter Captcha']",
            "button:has-text('Continue ')",
            "captcha"
        )
        if not ok:
            return BookingResult(False, payment_charged=False,
                                 message="Captcha failed before payment — money NOT charged (safe)")

        self.log("Payment submitted — waiting for PNR...", "warn")
        self.page.wait_for_timeout(10000)

        try:
            pnr_el = self.page.query_selector(".pnrNo, [class*='pnr']")
            pnr = pnr_el.inner_text().strip() if pnr_el else None
            if pnr:
                self.log(f"🎉 CONFIRMED! PNR: {pnr}", "ok")
                return BookingResult(True, pnr=pnr, payment_charged=True,
                                     message=f"Booking confirmed! PNR: {pnr}")
        except Exception:
            pass

        self.log("⚠️ Payment may have gone through — CHECK IRCTC app NOW", "error")
        return BookingResult(False, payment_charged=True,
                             message="Payment initiated but PNR not captured — CHECK IRCTC booking history")

    # ── Run ───────────────────────────────────────────────────────────────────

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
            return self.handle_payment()
        except Exception as e:
            self.log(f"Booking failed: {e}", "error")
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
        auto_pay=d.get("autoPay", False),
        session_id=sid
    )

def _emit_result(result: BookingResult, sid: str):
    level = "ok" if result.success else "error"
    push(sid, f"RESULT: {result.message}", level)
    socketio.emit("result", {
        "success": result.success,
        "pnr": result.pnr,
        "message": result.message,
        "payment_charged": result.payment_charged,
        "timestamp": result.timestamp,
    }, room=sid)

def _precise_wait(target: datetime, sid: str):
    now = datetime.now(IST)
    delta = (target - now).total_seconds()
    if delta > 5:
        push(sid, f"Waiting {delta:.0f}s until {target.strftime('%H:%M:%S IST')}")
        time.sleep(delta - 5)
    while datetime.now(IST) < target:
        time.sleep(0.05)
    push(sid, "⏰ Tatkal window OPEN — booking now!", "ok")

def run_scheduled(req_dict: dict, sid: str):
    req = _dict_to_req(req_dict, sid)
    jd = datetime.strptime(req.journey_date, "%Y-%m-%d")
    target = IST.localize(datetime(jd.year, jd.month, jd.day - 1, 10, 0, 0))
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
    return jsonify({"status": "scheduled", "launchAt": launch.strftime("%d %b %Y %H:%M:%S IST"), "jobId": job.id})

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

# ── WebSocket ─────────────────────────────────────────────────────────────────

@socketio.on("join")
def on_join(data):
    room = data.get("sessionId", "default")
    join_room(room)
    emit("joined", {"room": room})
    # Send existing logs for this session
    for line in booking_logs.get(room, []):
        emit("log", line)

if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
