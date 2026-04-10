from flask import Flask, request, jsonify, render_template
import time
import json
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path

app = Flask(__name__)

ROOMS = {
    "living_room": "Living Room",
    "bedroom": "Bedroom",
    "joes_room": "Joe's Room",
}

IGNORE_PREFIXES = [
    "",  # router/AP
]

KNOWN_DEVICES_FILE = Path.home() / "known_devices.json"

ACTIVE_TIMEOUT = 20
HOLD_TIMEOUT = 90
SWITCH_THRESHOLD = 6
REQUIRED_CONFIRMATIONS = 3
RSSI_MIN = -90
MIN_SAMPLES_PER_ROOM = 1
MAX_HISTORY = 20

ROOM_CALIBRATION = {
    "living_room": 0.0,
    "bedroom": 0.0,
    "joes_room": 0.0,
}

device_history = defaultdict(lambda: defaultdict(lambda: deque(maxlen=MAX_HISTORY)))
last_room = {}
last_seen = {}

candidate_room = {}
candidate_count = defaultdict(int)

room_durations = defaultdict(lambda: defaultdict(float))
session_first_seen = {}
session_last_seen = {}
last_update_time = {}
movement_history = defaultdict(list)
switch_count = defaultdict(int)


def now_ts():
    return time.time()


def fmt_time(ts):
    if not ts:
        return "N/A"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def weighted_average(values):
    if not values:
        return None
    weights = list(range(1, len(values) + 1))
    total_w = sum(weights)
    return sum(v * w for v, w in zip(values, weights)) / total_w


def load_known_devices():
    print("Using known devices file:", KNOWN_DEVICES_FILE)

    if not KNOWN_DEVICES_FILE.exists():
        print("known_devices.json not found")
        return {}

    try:
        with open(KNOWN_DEVICES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        normalized = {}
        for mac, info in data.items():
            norm_mac = str(mac).upper().strip()
            normalized[norm_mac] = info

        print("Loaded known devices:", list(normalized.keys()))
        return normalized

    except Exception as e:
        print(f"Could not load known devices: {e}")
        return {}


def get_device_name(mac, known_devices):
    info = known_devices.get(mac.upper())
    if not info:
        return None
    return info.get("name")


def accumulate_room_time(mac, current_time):
    if mac not in last_update_time:
        last_update_time[mac] = current_time
        return

    prev_time = last_update_time[mac]
    elapsed = current_time - prev_time
    if elapsed < 0:
        elapsed = 0

    prev_room = last_room.get(mac)
    if prev_room:
        room_durations[mac][prev_room] += elapsed

    last_update_time[mac] = current_time


def get_live_devices():
    now = now_ts()
    result = []
    known_devices = load_known_devices()

    for mac in list(device_history.keys()):
        if mac.upper() not in known_devices:
            continue

        room_scores = {}

        for sensor in device_history[mac]:
            samples = device_history[mac][sensor]
            fresh = [rssi for ts, rssi in samples if now - ts < ACTIVE_TIMEOUT]

            if len(fresh) >= MIN_SAMPLES_PER_ROOM:
                avg = weighted_average(fresh)
                avg += ROOM_CALIBRATION.get(sensor, 0.0)
                room_scores[sensor] = avg

        device_name = get_device_name(mac, known_devices)

        if not room_scores:
            if now - last_seen.get(mac, 0) < HOLD_TIMEOUT and mac in last_room:
                result.append({
                    "mac": mac,
                    "name": device_name,
                    "current_room_key": last_room[mac],
                    "current_room": ROOMS.get(last_room[mac], last_room[mac]),
                    "best_rssi": None,
                    "confidence": None,
                })
            continue

        sorted_rooms = sorted(room_scores.items(), key=lambda x: x[1], reverse=True)
        measured_best_room, measured_best_rssi = sorted_rooms[0]

        if len(sorted_rooms) > 1:
            second_best_rssi = sorted_rooms[1][1]
            confidence = measured_best_rssi - second_best_rssi
        else:
            confidence = 999

        chosen_room = measured_best_room
        chosen_rssi = measured_best_rssi

        if mac in last_room:
            prev_room = last_room[mac]

            if prev_room in room_scores and measured_best_room != prev_room:
                prev_rssi = room_scores[prev_room]

                strong_enough = measured_best_rssi >= prev_rssi + SWITCH_THRESHOLD
                confident_enough = confidence >= 3

                if strong_enough and confident_enough:
                    if candidate_room.get(mac) == measured_best_room:
                        candidate_count[mac] += 1
                    else:
                        candidate_room[mac] = measured_best_room
                        candidate_count[mac] = 1

                    if candidate_count[mac] >= REQUIRED_CONFIRMATIONS:
                        chosen_room = measured_best_room
                        chosen_rssi = measured_best_rssi
                        candidate_room[mac] = None
                        candidate_count[mac] = 0
                    else:
                        chosen_room = prev_room
                        chosen_rssi = prev_rssi
                else:
                    chosen_room = prev_room
                    chosen_rssi = prev_rssi
                    candidate_room[mac] = None
                    candidate_count[mac] = 0
            else:
                if prev_room in room_scores:
                    chosen_room = prev_room
                    chosen_rssi = room_scores[prev_room]
                candidate_room[mac] = None
                candidate_count[mac] = 0
        else:
            last_room[mac] = chosen_room

        accumulate_room_time(mac, now)

        prev_room = last_room.get(mac)
        if mac not in movement_history:
            movement_history[mac].append(chosen_room)
        elif prev_room and prev_room != chosen_room:
            movement_history[mac].append(chosen_room)
            switch_count[mac] += 1

        last_room[mac] = chosen_room
        last_seen[mac] = now

        result.append({
            "mac": mac,
            "name": device_name,
            "current_room_key": chosen_room,
            "current_room": ROOMS.get(chosen_room, chosen_room),
            "best_rssi": round(chosen_rssi, 1),
            "confidence": round(confidence, 1),
        })

    print("Live devices returned:", result)
    return result


@app.route("/")
def index():
    return render_template("index.html", rooms=ROOMS)


@app.route("/devices")
def devices():
    return jsonify(get_live_devices())


@app.route("/update", methods=["POST"])
def update():
    data = request.get_json(force=True)

    sensor = data.get("sensor")
    mac = str(data.get("mac", "")).upper().strip()
    rssi = data.get("rssi")

    if not sensor or not mac or rssi is None:
        return jsonify({"error": "missing data"}), 400

    if sensor not in ROOMS:
        return jsonify({"error": f"unknown sensor '{sensor}'"}), 400

    rssi = int(rssi)

    if any(mac.startswith(prefix) for prefix in IGNORE_PREFIXES):
        print(f"IGNORED infra MAC: {mac}")
        return jsonify({"ignored": True, "reason": "infra device"})

    if rssi < RSSI_MIN:
        print(f"IGNORED weak MAC: {mac}, rssi={rssi}")
        return jsonify({"ignored": True, "reason": "weak signal"})

    known_devices = load_known_devices()
    print(f"POST update -> sensor={sensor}, mac={mac}, rssi={rssi}")
    print(f"Known MACs -> {list(known_devices.keys())}")

    if mac not in known_devices:
        print(f"IGNORED unknown device: {mac}")
        return jsonify({"ignored": True, "reason": "unknown device"})

    current_time = now_ts()
    device_history[mac][sensor].append((current_time, rssi))

    if mac not in session_first_seen:
        session_first_seen[mac] = current_time
    session_last_seen[mac] = current_time

    print(f"ACCEPTED known device: {mac}")
    return jsonify({"ok": True})


def finalize_durations():
    end_time = now_ts()

    for mac in list(last_room.keys()):
        prev_room = last_room.get(mac)
        prev_time = last_update_time.get(mac)
        if prev_room and prev_time:
            elapsed = end_time - prev_time
            if elapsed > 0:
                room_durations[mac][prev_room] += elapsed
                last_update_time[mac] = end_time


def build_report():
    finalize_durations()
    known_devices = load_known_devices()

    report = []
    all_macs = sorted(set(session_first_seen.keys()) | set(room_durations.keys()))

    for mac in all_macs:
        if mac.upper() not in known_devices:
            continue

        durations = dict(room_durations[mac])

        pretty_durations = {
            ROOMS.get(room, room): round(seconds, 1)
            for room, seconds in durations.items()
        }

        most_time_room = None
        if durations:
            best_key = max(durations, key=durations.get)
            most_time_room = ROOMS.get(best_key, best_key)

        timeline = movement_history.get(mac, [])
        pretty_timeline = " → ".join([ROOMS.get(r, r) for r in timeline])

        report.append({
            "name": get_device_name(mac, known_devices),
            "mac": mac,
            "first_seen": fmt_time(session_first_seen.get(mac)),
            "last_seen": fmt_time(session_last_seen.get(mac)),
            "time_by_room_seconds": pretty_durations,
            "most_of_time_in": most_time_room,
            "movement_timeline": pretty_timeline,
            "room_switches": switch_count.get(mac, 0),
        })

    return report


def save_report():
    report = build_report()

    with open("session_report.txt", "w", encoding="utf-8") as f:
        f.write("Indoor Tracking Session Report\n")
        f.write("=" * 40 + "\n\n")

        if not report:
            f.write("No tracked devices in this session.\n")
        else:
            for item in report:
                f.write(f"Name: {item['name']}\n")
                f.write(f"MAC: {item['mac']}\n")
                f.write(f"First seen: {item['first_seen']}\n")
                f.write(f"Last seen : {item['last_seen']}\n")
                f.write("Time by room:\n")

                if item["time_by_room_seconds"]:
                    for room, seconds in item["time_by_room_seconds"].items():
                        f.write(f"  - {room}: {seconds} sec\n")
                else:
                    f.write("  - No room data\n")

                f.write(f"Most of the time in: {item['most_of_time_in']}\n")
                f.write(f"Room switches: {item['room_switches']}\n")
                f.write(f"Movement: {item['movement_timeline']}\n")
                f.write("-" * 40 + "\n")

    with open("session_report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    print("\nSession report saved:")
    print("  session_report.txt")
    print("  session_report.json")


if __name__ == "__main__":
    try:
        app.run(host="0.0.0.0", port=5000, debug=False)
    except KeyboardInterrupt:
        print("\nStopping server...")
    finally:
        save_report()