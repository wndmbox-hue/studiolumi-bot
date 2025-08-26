# app.py — один файл, без внешних schema/seed
import os, json, sqlite3
from datetime import datetime
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

# --- Пути и папки ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(BASE_DIR, "data"))
DB_PATH  = os.path.join(DATA_DIR, "data.sqlite")
ICS_DIR  = os.path.join(DATA_DIR, "ics")
os.makedirs(ICS_DIR, exist_ok=True)

# --- Константы демо-студии ---
HALLS_SEED = [
    ("A", "Daylight",  10000, 1.10),
    ("B", "Loft",      12000, 1.15),
    ("C", "Cyclorama", 15000, 1.20),
]
ADDONS_PRICE = {
    "Набор свет A": 3000,
    "Фон белый":    1500,
    "Стойки":       1000,
}
WORK_START = 9 * 60   # 09:00
WORK_END   = 21 * 60  # 21:00
SLOT_DUR   = 60
BUFFER     = 15       # мин между бронированиями
BASE_URL   = os.environ.get("BASE_URL", "http://localhost:8000")

app = FastAPI(title="Studio Lumi API (single-file)")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

# --- База данных и инициализация ---
def get_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as db:
        db.execute("PRAGMA journal_mode=WAL;")
        db.executescript("""
        CREATE TABLE IF NOT EXISTS halls(
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            base_price INTEGER NOT NULL,
            weekend_coef REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS bookings(
            booking_id TEXT PRIMARY KEY,
            hall_id TEXT NOT NULL,
            date TEXT NOT NULL,        -- YYYY-MM-DD
            start_min INTEGER NOT NULL,
            end_min INTEGER NOT NULL,
            name TEXT,
            phone TEXT,
            addons TEXT,
            price INTEGER,
            status TEXT NOT NULL DEFAULT 'confirmed',
            created_at TEXT NOT NULL
        );
        """)
        c = db.execute("SELECT COUNT(*) FROM halls").fetchone()[0]
        if c == 0:
            db.executemany(
                "INSERT INTO halls (id,title,base_price,weekend_coef) VALUES (?,?,?,?)",
                HALLS_SEED
            )
            db.commit()

def is_weekend(date_iso: str) -> bool:
    y, m, d = map(int, date_iso.split("-"))
    return datetime(y, m, d).isoweekday() in (6, 7)

def calc_price(hall: sqlite3.Row, date_iso: str, start_min: int, addons: list[dict]) -> int:
    price = int(hall["base_price"])
    if is_weekend(date_iso):
        price = round(price * float(hall["weekend_coef"]))
    if 17 <= (start_min // 60) < 21:
        price = round(price * 1.3)  # прайм-тайм
    for a in addons:
        price += int(a.get("price", 0))
    return price

def time_to_min(hhmm: str) -> int:
    h, m = map(int, hhmm.split(":"))
    return h * 60 + m

def parse_slot(slot: str) -> int:
    # принимаем 15:00–16:00, 15:00-16:00, 15:00—16:00
    s = str(slot).replace("—", "-").replace("–", "-")
    start = s.split("-")[0].strip()
    return time_to_min(start)

def min_to_range(start_min: int, dur: int = 60) -> str:
    end = start_min + dur
    h1, m1 = divmod(start_min, 60)
    h2, m2 = divmod(end, 60)
    return f"{h1:02d}:{m1:02d}–{h2:02d}:{m2:02d}"

def make_ics(booking_id: str, hall_id: str, date: str, start_min: int, end_min: int, name: str|None, phone: str|None) -> str:
    # простая генерация .ics без внешних библиотек
    y, m, d = map(int, date.split("-"))
    sh, sm = divmod(start_min, 60)
    eh, em = divmod(end_min, 60)

    def z2(n): return f"{n:02d}"
    dtstamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    dtstart = f"{y}{z2(m)}{z2(d)}T{z2(sh)}{z2(sm)}00"
    dtend   = f"{y}{z2(m)}{z2(d)}T{z2(eh)}{z2(em)}00"

    content = (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:-//StudioLumi//EN\r\n"
        "BEGIN:VEVENT\r\n"
        f"UID:{booking_id}\r\n"
        f"DTSTAMP:{dtstamp}\r\n"
        f"DTSTART:{dtstart}\r\n"
        f"DTEND:{dtend}\r\n"
        f"SUMMARY:Съёмка — Studio Lumi ({hall_id})\r\n"
        f"DESCRIPTION:Бронь {booking_id}\\nКлиент: {name or ''} {phone or ''}\r\n"
        "LOCATION:Studio Lumi\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )

    rel = f"/ics/{booking_id}.ics"
    path = os.path.join(ICS_DIR, f"{booking_id}.ics")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return rel

# --- Инициализируем БД при импорте ---
init_db()

# --- Эндпоинты ---
@app.get("/health")
def health():
    return {"ok": True}

@app.get("/slots")
def slots(hall_id: str, date: str):
    if not hall_id or not date:
        raise HTTPException(400, "hall_id and date required")
    with get_db() as db:
        rows = db.execute(
            "SELECT start_min,end_min FROM bookings WHERE hall_id=? AND date=? AND status='confirmed'",
            (hall_id, date)
        ).fetchall()
    busy = [(r["start_min"], r["end_min"]) for r in rows]
    out = []
    start = WORK_START
    while start + SLOT_DUR <= WORK_END:
        end = start + SLOT_DUR
        conflict = any(not (end + BUFFER <= s or start >= e + BUFFER) for s, e in busy)
        if not conflict:
            out.append(min_to_range(start, SLOT_DUR))
        start += SLOT_DUR
    return {"date": date, "hall_id": hall_id, "slots": out}

@app.post("/book")
def book(payload: dict):
    hall_id = payload.get("hall_id")
    date    = payload.get("date")
    slot    = payload.get("slot")
    name    = payload.get("name")
    phone   = payload.get("phone")
    addons_names = payload.get("addons", [])
    if not (hall_id and date and slot and phone):
        raise HTTPException(400, "hall_id, date, slot, phone required")

    start_min = parse_slot(slot)
    end_min   = start_min + SLOT_DUR

    with get_db() as db:
        # проверка коллизий
        row = db.execute(
            """
            SELECT 1 FROM bookings
            WHERE hall_id=? AND date=? AND status='confirmed'
              AND NOT (? + ? <= start_min OR ? >= end_min + ?)
            LIMIT 1
            """,
            (hall_id, date, end_min, BUFFER, start_min, BUFFER)
        ).fetchone()
        if row:
            raise HTTPException(409, "Slot not available")

        hall = db.execute("SELECT * FROM halls WHERE id=?", (hall_id,)).fetchone()
        if not hall:
            raise HTTPException(400, "Unknown hall")

        addons_d = [{"name": n, "price": ADDONS_PRICE.get(n, 0)} for n in addons_names]
        price = calc_price(hall, date, start_min, addons_d)
        booking_id = f"BK-{date}-{hall_id}-{start_min//60:02d}00"

        db.execute(
            "INSERT INTO bookings (booking_id,hall_id,date,start_min,end_min,name,phone,addons,price,status,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,'confirmed',datetime('now'))",
            (booking_id, hall_id, date, start_min, end_min, name, phone, json.dumps(addons_d, ensure_ascii=False), price)
        )
        db.commit()

    ics_url = f"{BASE_URL}{make_ics(booking_id, hall_id, date, start_min, end_min, name, phone)}"
    return {"booking_id": booking_id, "price": price, "status": "confirmed", "ics_url": ics_url}

@app.post("/cancel")
def cancel(payload: dict):
    booking_id = payload.get("booking_id")
    if not booking_id:
        raise HTTPException(400, "booking_id required")
    with get_db() as db:
        db.execute("UPDATE bookings SET status='canceled' WHERE booking_id=?", (booking_id,))
        db.commit()
    return {"ok": True}

@app.get("/bookings")
def bookings(phone: str):
    with get_db() as db:
        rows = db.execute(
            "SELECT booking_id,hall_id,date,start_min,price FROM bookings WHERE phone=? AND status='confirmed' ORDER BY date,start_min",
            (phone,)
        ).fetchall()
    return [
        {"booking_id": r["booking_id"], "hall_id": r["hall_id"], "date": r["date"], "slot": min_to_range(r["start_min"]), "price": r["price"]}
        for r in rows
    ]

@app.get("/ics/{fname}")
def ics_files(fname: str):
    path = os.path.join(ICS_DIR, fname)
    if not os.path.isfile(path):
        raise HTTPException(404, "not found")
    return FileResponse(path, media_type="text/calendar")

# Запуск: uvicorn app:app --host 0.0.0.0 --port 8000
