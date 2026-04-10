from flask import Flask, request, render_template_string, redirect, url_for
from pathlib import Path
import subprocess
import json
from datetime import datetime
import re

app = Flask(__name__)

DATA_FILE = Path.home() / "known_devices.json"

HTML = """
<!doctype html>
<html>
<head>
    <meta charset="utf-8">
    <title>Device Registration</title>
    <style>
        body {
            font-family: Arial, sans-serif;
            max-width: 700px;
            margin: 40px auto;
            padding: 20px;
            background: #f8fafc;
        }
        input, button, a.button {
            padding: 12px;
            margin: 8px 0;
            width: 100%;
            font-size: 16px;
            box-sizing: border-box;
        }
        .box {
            border: 1px solid #ccc;
            border-radius: 10px;
            padding: 20px;
            background: white;
        }
        .msg {
            margin: 12px 0;
            padding: 12px;
            border-radius: 8px;
            background: #f3f4f6;
        }
        table {
            width: 100%;
            border-collapse: collapse;
            margin-top: 24px;
            background: white;
        }
        th, td {
            border: 1px solid #ddd;
            padding: 10px;
            text-align: left;
        }
        .small {
            color: #666;
            font-size: 14px;
        }
        a.button {
            display: inline-block;
            text-decoration: none;
            text-align: center;
            background: #2563eb;
            color: white;
            border-radius: 8px;
        }
    </style>
</head>
<body>
    <div class="box">
        <h2>Register Your Device</h2>
        <form method="post" action="/register">
            <label>Your name</label>
            <input type="text" name="name" required>
            <button type="submit">Register</button>
        </form>

        <p class="small">
            Connect to this hotspot, enter your name, then keep the tracking page open.
        </p>

        {% if message %}
        <div class="msg">{{ message }}</div>
        {% endif %}

        {% if show_tracking_button %}
        <a class="button" href="/traffic">Open Tracking Page</a>
        {% endif %}
    </div>

    <h3>Registered Devices</h3>
    <table>
        <tr>
            <th>Name</th>
            <th>MAC</th>
            <th>IP</th>
            <th>Saved At</th>
        </tr>
        {% for mac, info in devices.items() %}
        <tr>
            <td>{{ info["name"] }}</td>
            <td>{{ mac }}</td>
            <td>{{ info.get("ip", "") }}</td>
            <td>{{ info["saved_at"] }}</td>
        </tr>
        {% endfor %}
    </table>
</body>
</html>
"""

TRAFFIC_HTML = """
<!doctype html>
<html>
<head>
    <meta charset="utf-8">
    <title>Tracking Active</title>
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <style>
        body {
            font-family: Arial, sans-serif;
            background: #0f172a;
            color: white;
            display: flex;
            justify-content: center;
            align-items: center;
            min-height: 100vh;
            margin: 0;
            text-align: center;
            padding: 20px;
            box-sizing: border-box;
        }
        .card {
            background: #1e293b;
            padding: 30px;
            border-radius: 16px;
            max-width: 500px;
            width: 100%;
        }
        .status {
            margin-top: 16px;
            font-size: 18px;
            color: #22c55e;
        }
        .small {
            margin-top: 12px;
            color: #cbd5e1;
            font-size: 14px;
        }
    </style>
</head>
<body>
    <div class="card">
        <h1>Tracking Active</h1>
        <p>Keep this page open to generate local Wi-Fi traffic for room tracking.</p>
        <div class="status" id="status">Starting...</div>
        <div class="small">This works without internet.</div>
    </div>

<script>
async function pingServer() {
    try {
        await fetch('/ping', { cache: 'no-store' });
        document.getElementById('status').textContent = 'Sending traffic...';
    } catch (e) {
        document.getElementById('status').textContent = 'Retrying...';
    }
}

setInterval(pingServer, 1000);
pingServer();
</script>
</body>
</html>
"""


def load_devices():
    if not DATA_FILE.exists():
        return {}
    try:
        with open(DATA_FILE, "r") as f:
            data = json.load(f)
        return {k.upper(): v for k, v in data.items()}
    except Exception:
        return {}


def save_devices(devices):
    with open(DATA_FILE, "w") as f:
        json.dump(devices, f, indent=2)


def is_valid_mac(mac: str) -> bool:
    return bool(re.fullmatch(r"([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", mac.strip()))


def get_client_ip() -> str:
    forwarded = request.headers.get("X-Forwarded-For", "").strip()
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or ""


def lookup_mac_from_ip(ip: str):
    if not ip:
        return None

    try:
        result = subprocess.run(
            ["ip", "neigh", "show", ip],
            capture_output=True,
            text=True,
            check=False,
        )
        output = result.stdout.strip()
        match = re.search(r"lladdr\s+([0-9a-fA-F:]{17})", output)
        if match:
            mac = match.group(1).upper()
            if is_valid_mac(mac):
                return mac
    except Exception:
        pass

    return None


@app.route("/", methods=["GET"])
def index():
    devices = load_devices()
    return render_template_string(
        HTML,
        devices=devices,
        message="",
        show_tracking_button=False
    )


@app.route("/register", methods=["POST"])
def register():
    name = request.form.get("name", "").strip()
    devices = load_devices()

    if not name:
        return render_template_string(
            HTML,
            devices=devices,
            message="Name is required.",
            show_tracking_button=False
        )

    ip = get_client_ip()
    mac = lookup_mac_from_ip(ip)

    if not mac:
        return render_template_string(
            HTML,
            devices=devices,
            message=f"Could not detect your device MAC automatically from IP {ip}. Make sure you are connected to this hotspot and reload the page.",
            show_tracking_button=False
        )

    devices[mac] = {
        "name": name,
        "ip": ip,
        "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }
    save_devices(devices)

    return render_template_string(
        HTML,
        devices=devices,
        message=f"Registered {name} with MAC {mac}. Now open the tracking page and keep it open.",
        show_tracking_button=True
    )


@app.route("/traffic", methods=["GET"])
def traffic():
    return render_template_string(TRAFFIC_HTML)


@app.route("/ping", methods=["GET"])
def ping():
    return "ok", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)