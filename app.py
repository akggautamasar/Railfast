"""
RailFast - IRCTC Tatkal Auto-Booker
Deployable on Render (free tier with persistent scheduler)
Run: gunicorn --worker-class eventlet -w 1 app:app
"""

import os
import json
import time
import logging
import threading
import io
from datetime import datetime, timedelta
from dataclasses import dataclass, field, asdict
from typing import Optional, List

import pytz
from flask import Flask, render_template, request, jsonify, session
from flask_socketio import SocketIO, emit

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

# Selenium
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import TimeoutException, NoSuchElementException, StaleElementReferenceException
from PIL import Image
import pytesseract

# ── App setup ──────────────────────────────────────────────────────────────────

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "railfast-secret-2025")
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

IST = pytz.timezone("Asia/Kolkata")
scheduler = BackgroundScheduler()
scheduler.start()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("railfast")

# In-memory job store (use Redis/DB for production)
active_jobs = {}       # job_id -> APScheduler job
booking_logs = {}      # session_id -> list of log lines


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class Passenger:
    name: str
    age: int
    gender: str          # Male / Female / Transgender
    food: str            # V / NV / N
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
    journey_date: str    # YYYY-MM-DD
    passengers: List[Passenger]
    auto_pay: bool = False
    captcha_retries: int = 5
    session_id: str = ""


@dataclass
class BookingResult:
    success: bool
    pnr: Optional[str] = None
    message: str = ""
    payment_charged: bool = False   # True = money may have left account
    timestamp: str = field(default_factory=lambda: datetime.now(IST).strftime("%d %b %Y %H:%M:%S IST"))


# ── Logging helper ─────────────────────────────────────────────────────────────

def push_log(session_id: str, msg: str, level: str = "info"):
    """Send live log line to browser via WebSocket."""
    ts = datetime.now(IST).strftime("%H:%M:%S")
    line = {"ts": ts, "msg": msg, "level": level}
    booking_logs.setdefault(session_id, []).append(line)
    socketio.emit("log", line, room=session_id)
    logger.info(f"[{session_id[:8]}] {msg}")


# ── Booking Engine ─────────────────────────────────────────────────────────────

class IRCTCBot:
    def __init__(self, req: BookingRequest):
        self.req = req
        self.driver = None
        self.wait = None

    def _log(self, msg, level="info"):
        push_log(self.req.session_id, msg, level)

    # ── Driver setup ──────────────────────────────────────────────────────────

    def start(self):
        opts = Options()
        # Core headless flags
        opts.add_argument("--headless=new")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--disable-gpu")
        opts.add_argument("--window-size=1366,768")
        # Crash prevention in Docker/container environments
        opts.add_argument("--disable-setuid-sandbox")
        opts.add_argument("--disable-software-rasterizer")
        opts.add_argument("--disable-extensions")
        opts.add_argument("--disable-background-networking")
        opts.add_argument("--disable-default-apps")
        opts.add_argument("--disable-sync")
        opts.add_argument("--disable-translate")
        opts.add_argument("--hide-scrollbars")
        opts.add_argument("--mute-audio")
        opts.add_argument("--no-first-run")
        opts.add_argument("--safebrowsing-disable-auto-update")
        opts.add_argument("--ignore-certificate-errors")
        opts.add_argument("--disable-blink-features=AutomationControlled")
        # Use /tmp for all disk IO (writable in Render containers)
        opts.add_argument("--disk-cache-dir=/tmp/chrome-cache")
        opts.add_argument("--user-data-dir=/tmp/chrome-userdata")
        opts.add_argument("--crash-dumps-dir=/tmp/chrome-crashes")
        # Memory limits for 512MB Render starter
        opts.add_argument("--memory-pressure-off")
        opts.add_argument("--max_old_space_size=256")
        opts.add_experimental_option("excludeSwitches", ["enable-automation"])
        opts.add_experimental_option("useAutomationExtension", False)
        # Explicit paths installed by Dockerfile
        opts.binary_location = "/usr/bin/google-chrome"
        from selenium.webdriver.chrome.service import Service
        import subprocess, os
        # Log Chrome version for debugging
        try:
            ver = subprocess.check_output(["google-chrome", "--version"]).decode().strip()
            self._log(f"Chrome: {ver}")
        except Exception:
            pass
        service = Service(
            executable_path="/usr/local/bin/chromedriver",
            log_output="/tmp/chromedriver.log"
        )
        self.driver = webdriver.Chrome(service=service, options=opts)
        self.driver.execute_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
        )
        self.wait = WebDriverWait(self.driver, 20)
        self._log("Chrome launched (headless, containerized)")

    def stop(self):
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
            self._log("Browser closed")

    # ── Captcha ───────────────────────────────────────────────────────────────

    def _solve_captcha(self) -> str:
        el = self.wait.until(EC.presence_of_element_located((By.CLASS_NAME, "captcha-img")))
        img = Image.open(io.BytesIO(el.screenshot_as_png)).convert("L")
        img = img.point(lambda x: 0 if x < 140 else 255)
        text = pytesseract.image_to_string(
            img,
            config="--psm 8 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
        ).strip()
        return text

    def _submit_captcha_with_retry(self, input_xpath: str, btn_xpath: str) -> bool:
        for attempt in range(1, self.req.captcha_retries + 1):
            try:
                text = self._solve_captcha()
                self._log(f"Captcha attempt {attempt}: '{text}'")
                field = self.wait.until(EC.element_to_be_clickable((By.XPATH, input_xpath)))
                field.clear()
                field.send_keys(text)
                self.wait.until(EC.element_to_be_clickable((By.XPATH, btn_xpath))).click()
                time.sleep(1.5)
                errors = self.driver.find_elements(
                    By.XPATH, "//*[contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'captcha') and contains(@class,'error')]"
                )
                if not errors:
                    self._log(f"Captcha solved on attempt {attempt}", "ok")
                    return True
                self._log("Captcha rejected, retrying...", "warn")
            except (TimeoutException, NoSuchElementException, StaleElementReferenceException) as e:
                self._log(f"Captcha error: {e}", "warn")
                time.sleep(1)
        self._log("All captcha attempts failed", "error")
        return False

    # ── Login ─────────────────────────────────────────────────────────────────

    def login(self):
        self._log("Opening IRCTC...")
        self.driver.get("https://www.irctc.co.in/nget/train-search")
        self.wait.until(EC.element_to_be_clickable((By.XPATH, "//a[text()=' LOGIN ']"))).click()
        time.sleep(2)
        self.driver.find_element(By.XPATH, "//input[@placeholder='User Name']").send_keys(self.req.username)
        self.driver.find_element(By.XPATH, "//input[@placeholder='Password']").send_keys(self.req.password)
        ok = self._submit_captcha_with_retry(
            "//input[@placeholder='Enter Captcha']",
            "//button[text()='SIGN IN']"
        )
        if not ok:
            raise RuntimeError("Login failed — captcha unsolvable")
        # Confirm login
        self.wait.until(EC.presence_of_element_located((By.XPATH, "//*[contains(@class,'username')]")))
        self._log("Logged in successfully", "ok")
        time.sleep(1.5)

    # ── Journey search ────────────────────────────────────────────────────────

    def _pick_date(self):
        jdate = datetime.strptime(self.req.journey_date, "%Y-%m-%d")
        day = str(jdate.day)
        month = jdate.strftime("%b").upper()[:3]
        year = str(jdate.year)

        self.wait.until(EC.element_to_be_clickable((By.ID, "jDate"))).click()
        time.sleep(0.5)
        for _ in range(24):
            cur_m = self.driver.find_element(By.XPATH, "//span[contains(@class,'ui-datepicker-month')]").text.upper()[:3]
            cur_y = self.driver.find_element(By.XPATH, "//span[contains(@class,'ui-datepicker-year')]").text
            if cur_y == year and cur_m == month:
                break
            self.driver.find_element(By.CLASS_NAME, "ui-datepicker-next").click()
            time.sleep(0.3)
        self.wait.until(EC.element_to_be_clickable((By.XPATH, f"//a[text()='{int(day)}']"))).click()
        self._log(f"Date selected: {self.req.journey_date}")

    def search_trains(self):
        self._log(f"Searching {self.req.from_station} → {self.req.to_station}")
        for fid, val in [("pr_id_1_list", self.req.from_station), ("pr_id_2_list", self.req.to_station)]:
            f = self.wait.until(EC.element_to_be_clickable((By.XPATH, f"//input[@aria-controls='{fid}']")))
            f.send_keys(val)
            time.sleep(1)
            self.wait.until(EC.element_to_be_clickable((By.XPATH, f"//ul[@id='{fid}']/li[1]"))).click()

        # TATKAL quota
        self.wait.until(EC.element_to_be_clickable((By.XPATH, "//p-dropdown[@id='journeyQuota']"))).click()
        self.wait.until(EC.element_to_be_clickable((By.XPATH, "//li[@role='option' and @aria-label='TATKAL']"))).click()

        self._pick_date()
        time.sleep(1.5)

        self.wait.until(EC.element_to_be_clickable(
            (By.XPATH, "//button[@type='submit' and contains(@class,'train_Search')]")
        )).click()
        self._log("Search submitted, waiting for results...")

    # ── Train & class ─────────────────────────────────────────────────────────

    def select_train(self):
        time.sleep(3)
        divs = self.driver.find_elements(By.CLASS_NAME, "bull-back")
        for div in divs:
            if self.req.train_no in div.text:
                self._log(f"Train {self.req.train_no} found")
                div.find_element(By.XPATH, f".//strong[text()='{self.req.coach_class}']").click()

                avail = self.wait.until(EC.presence_of_element_located(
                    (By.XPATH, "//td[contains(@class,'link') and contains(@class,'ng-star-inserted')]")
                ))
                status = avail.text.strip().upper()
                self._log(f"Availability: {status}")

                if "NOT AVAILABLE" in status or "REGRET" in status:
                    raise RuntimeError(f"No seats available ({status}) — payment NOT initiated")

                avail.click()
                div.find_element(By.XPATH, ".//button[contains(@class,'train_Search') and contains(text(),'Book Now')]").click()
                self._log(f"Clicked Book Now — class {self.req.coach_class}", "ok")
                return

        raise RuntimeError(f"Train {self.req.train_no} not found in search results")

    # ── Passengers ────────────────────────────────────────────────────────────

    def fill_passengers(self):
        gmap = {"Male": "M", "Female": "F", "Transgender": "T"}
        self.wait.until(EC.visibility_of_element_located((By.XPATH, "//input[@placeholder='Name']")))

        for i, p in enumerate(self.req.passengers):
            if i > 0:
                btns = self.driver.find_elements(By.XPATH, "//span[contains(text(),'+ Add Passenger')]")
                if btns:
                    btns[0].click()
                    time.sleep(0.6)

            names = self.driver.find_elements(By.XPATH, "//input[@placeholder='Name']")
            names[i].clear(); names[i].send_keys(p.name)

            ages = self.driver.find_elements(By.XPATH, "//input[@formcontrolname='passengerAge']")
            ages[i].clear(); ages[i].send_keys(str(p.age))

            genders = self.driver.find_elements(By.XPATH, "//select[@formcontrolname='passengerGender']")
            Select(genders[i]).select_by_value(gmap.get(p.gender, "M"))

            try:
                foods = self.driver.find_elements(By.XPATH, "//select[@id='FOOD_0']")
                if i < len(foods):
                    Select(foods[i]).select_by_value(p.food)
            except Exception:
                pass

            self._log(f"Passenger {i+1}: {p.name}, {p.age}y, {p.gender}", "ok")

        self.wait.until(EC.element_to_be_clickable(
            (By.XPATH, "//button[contains(@class,'train_Search') and contains(text(),'Continue')]")
        )).click()
        self._log("Passenger details submitted")

    # ── Payment via UPI ───────────────────────────────────────────────────────

    def handle_payment(self) -> BookingResult:
        time.sleep(5)
        # Confirm we're on review page
        try:
            self.wait.until(EC.presence_of_element_located(
                (By.XPATH, "//*[contains(text(),'Total Fare') or contains(text(),'Review your')]")
            ))
            self._log("Review page loaded — verifying fare before payment", "ok")
        except TimeoutException:
            return BookingResult(
                success=False,
                message="Did not reach review page — payment NOT charged"
            )

        if not self.req.auto_pay:
            self._log("auto_pay=OFF — stopping. Open IRCTC app to complete payment manually.", "warn")
            return BookingResult(success=True, message="Reached review page. Complete payment manually in IRCTC app.")

        # -- AUTO PAY PATH --
        # IRCTC payment page: select UPI option
        self._log("Selecting UPI payment...")
        try:
            upi_tab = self.wait.until(EC.element_to_be_clickable(
                (By.XPATH, "//*[contains(text(),'UPI') or contains(text(),'Bhim')]")
            ))
            upi_tab.click()
            time.sleep(1)

            upi_field = self.wait.until(EC.element_to_be_clickable(
                (By.XPATH, "//input[contains(@placeholder,'UPI') or contains(@placeholder,'upi') or contains(@name,'upi')]")
            ))
            upi_field.clear()
            upi_field.send_keys(self.req.upi_id)
            self._log(f"UPI ID entered: {self.req.upi_id}", "ok")
            time.sleep(0.5)
        except Exception as e:
            self._log(f"Could not auto-fill UPI — fill manually: {e}", "warn")

        # Final captcha before payment
        ok = self._submit_captcha_with_retry(
            "//input[@placeholder='Enter Captcha']",
            "//button[text()='Continue ']"
        )
        if not ok:
            return BookingResult(
                success=False,
                payment_charged=False,
                message="Captcha failed before payment — money NOT charged (safe to retry)"
            )

        self._log("Payment submitted — waiting for confirmation...", "warn")
        time.sleep(10)

        # Try to grab PNR
        try:
            pnr_el = self.driver.find_element(By.XPATH,
                "//*[contains(text(),'PNR')]//following-sibling::*[1] | //*[@class='pnrNo']"
            )
            pnr = pnr_el.text.strip()
            self._log(f"🎉 BOOKING CONFIRMED! PNR: {pnr}", "ok")
            return BookingResult(success=True, pnr=pnr, payment_charged=True,
                                 message=f"Confirmed. PNR: {pnr}")
        except NoSuchElementException:
            self._log("⚠️ Payment may have been initiated but PNR not captured. CHECK IRCTC booking history!", "error")
            return BookingResult(success=False, payment_charged=True,
                                 message="Payment initiated but PNR unknown — CHECK IRCTC booking history NOW")

    # ── Full flow ─────────────────────────────────────────────────────────────

    def run(self) -> BookingResult:
        try:
            self.start()
            self.login()
            self.search_trains()
            time.sleep(2)
            self.select_train()
            time.sleep(2)
            self.fill_passengers()
            time.sleep(3)
            return self.handle_payment()
        except Exception as e:
            self._log(f"Booking failed: {e}", "error")
            return BookingResult(success=False, message=str(e), payment_charged=False)
        finally:
            self.stop()


# ── Scheduler helper ───────────────────────────────────────────────────────────

def _precise_wait(target_ist: datetime, session_id: str):
    """Sleep until 5s before target, then busy-wait to the exact second."""
    now = datetime.now(IST)
    delta = (target_ist - now).total_seconds()
    if delta > 5:
        push_log(session_id, f"Sleeping {delta:.0f}s until {target_ist.strftime('%H:%M:%S IST')}")
        time.sleep(delta - 5)
    while datetime.now(IST) < target_ist:
        time.sleep(0.05)
    push_log(session_id, "⏰ Tatkal window OPEN — starting booking NOW!", "ok")


def run_scheduled_booking(req_dict: dict, session_id: str):
    req = _dict_to_req(req_dict, session_id)
    journey_dt = datetime.strptime(req.journey_date, "%Y-%m-%d")
    # AC tatkal opens D-1 at 10:00:00 IST
    target = IST.localize(datetime(journey_dt.year, journey_dt.month, journey_dt.day - 1, 10, 0, 0))
    _precise_wait(target, session_id)
    bot = IRCTCBot(req)
    result = bot.run()
    _emit_result(result, session_id)


def _dict_to_req(d: dict, session_id: str) -> BookingRequest:
    passengers = [
        Passenger(
            name=p["name"], age=int(p["age"]),
            gender=p.get("gender", "Male"),
            food=p.get("food", "V"),
            berth=p.get("berth", "NP")
        )
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
        session_id=session_id
    )


def _emit_result(result: BookingResult, session_id: str):
    level = "ok" if result.success else ("error" if result.payment_charged else "error")
    push_log(session_id, f"RESULT: {result.message}", level)
    socketio.emit("result", {
        "success": result.success,
        "pnr": result.pnr,
        "message": result.message,
        "payment_charged": result.payment_charged,
        "timestamp": result.timestamp
    }, room=session_id)


# ── Flask Routes ───────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/book", methods=["POST"])
def book_now():
    """Immediate booking — runs in background thread."""
    data = request.json
    session_id = data.get("sessionId", "default")
    req = _dict_to_req(data, session_id)
    thread = threading.Thread(target=lambda: _emit_result(IRCTCBot(req).run(), session_id), daemon=True)
    thread.start()
    return jsonify({"status": "started", "sessionId": session_id})


@app.route("/api/schedule", methods=["POST"])
def schedule_booking():
    """Schedule booking for D-1 at 10:00 IST."""
    data = request.json
    session_id = data.get("sessionId", "default")

    journey_dt = datetime.strptime(data["journeyDate"], "%Y-%m-%d")
    schedule_dt = journey_dt - timedelta(days=1)
    # Start at 9:59:50 to give bot time to launch
    trigger = CronTrigger(
        year=schedule_dt.year, month=schedule_dt.month, day=schedule_dt.day,
        hour=9, minute=59, second=50, timezone=IST
    )
    job = scheduler.add_job(
        run_scheduled_booking, trigger=trigger,
        args=[data, session_id]
    )
    active_jobs[session_id] = job.id

    launch_time = IST.localize(datetime(schedule_dt.year, schedule_dt.month, schedule_dt.day, 9, 59, 50))
    return jsonify({
        "status": "scheduled",
        "launchAt": launch_time.strftime("%d %b %Y %H:%M:%S IST"),
        "jobId": job.id
    })


@app.route("/api/cancel", methods=["POST"])
def cancel_job():
    session_id = request.json.get("sessionId")
    job_id = active_jobs.pop(session_id, None)
    if job_id:
        try:
            scheduler.remove_job(job_id)
            return jsonify({"status": "cancelled"})
        except Exception:
            pass
    return jsonify({"status": "not_found"})


@app.route("/api/logs/<session_id>")
def get_logs(session_id):
    return jsonify(booking_logs.get(session_id, []))


@app.route("/health")
def health():
    return "OK", 200


# ── WebSocket ─────────────────────────────────────────────────────────────────

@socketio.on("join")
def on_join(data):
    from flask_socketio import join_room
    room = data.get("sessionId", "default")
    join_room(room)
    emit("joined", {"room": room})


if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
