import sqlite3
import time
import json
from pathlib import Path

AP_BSSID = ""   # replace if hotspot BSSID changed
OFFLINE_TIMEOUT = 10
POLL_INTERVAL = 1
MISSES_BEFORE_OFFLINE = 3

NAMES_FILE = Path.home() / "known_devices.json"

IGNORED_MACS = {
    AP_BSSID,
}

device_state = {}
miss_count = {}


def get_latest_kismet_db():
    files = sorted(Path.home().glob("*.kismet"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        raise FileNotFoundError("No .kismet files found")
    return files[0]


def load_names():
    if not NAMES_FILE.exists():
        return {}
    try:
        with open(NAMES_FILE, "r") as f:
            data = json.load(f)
        return {k.upper(): v for k, v in data.items()}
    except Exception:
        return {}


def label_for(mac, known_names):
    info = known_names.get(mac.upper())
    if not info:
        return mac
    return f'{info["name"]} ({mac})'


def normalize(device_data):
    if device_data is None:
        return ""
    if isinstance(device_data, bytes):
        return device_data.decode("utf-8", errors="ignore")
    return str(device_data)


def belongs_to_ap(device_data):
    return AP_BSSID in normalize(device_data).upper()


def open_readonly_db(db_path):
    db_uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(db_uri, uri=True, timeout=5)
    conn.execute("PRAGMA busy_timeout = 5000;")
    return conn


def main():
    conn = None
    cur = None
    current_db = None

    print("Starting named monitor...")

    while True:
        latest_db = get_latest_kismet_db()
        known_names = load_names()

        if latest_db != current_db:
            if conn:
                conn.close()

            conn = open_readonly_db(latest_db)
            cur = conn.cursor()
            current_db = latest_db
            print(f"\nUsing DB: {latest_db}")

        now = int(time.time())

        try:
            rows = cur.execute("""
                SELECT devmac, last_time, device
                FROM devices
                WHERE devmac IS NOT NULL
            """).fetchall()
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower():
                print("Database is temporarily locked, retrying...")
                time.sleep(POLL_INTERVAL)
                continue
            raise

        fresh_now = set()

        for mac, last_time, device_data in rows:
            if mac is None or last_time is None:
                continue

            mac = mac.upper()

            if mac in IGNORED_MACS:
                continue

            if not belongs_to_ap(device_data):
                continue

            age = now - int(last_time)
            if age <= OFFLINE_TIMEOUT:
                fresh_now.add(mac)

        for mac in fresh_now:
            miss_count[mac] = 0
            if not device_state.get(mac, False):
                device_state[mac] = True
                print(f"🟢 CONNECTED: {label_for(mac, known_names)}")

        for mac in list(device_state.keys()):
            if mac in fresh_now:
                continue

            miss_count[mac] = miss_count.get(mac, 0) + 1

            if device_state.get(mac, False) and miss_count[mac] >= MISSES_BEFORE_OFFLINE:
                device_state[mac] = False
                print(f"🔴 DISCONNECTED: {label_for(mac, known_names)}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()