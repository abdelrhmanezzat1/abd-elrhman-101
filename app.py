import requests
import time
import threading
import json
import uuid
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, emit
import random
import logging
import os

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'webook_bot_super_secret')
# تعديل async_mode ليكون متوافقاً مع gunicorn
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# ------------------- إعدادات أساسية -------------------
BASE_CONFIG = {
    "WORKSPACE_KEY": "66e63c10464382fb1f049832",
    "EVENT_SLUG": "nassr-vs-hilal",
    "CHART_KEY": "38bd4175-2082-4161-8c13-b396b98d477c",
    "CHECKOUT_URL": "https://webook.com/ar/checkout"
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
    "Referer": "https://webook.com/",
    "Origin": "https://webook.com",
    "Content-Type": "application/json"
}

# ------------------- الحالة العامة -------------------
accounts = []          
proxies_list = []
bot_running = False
bot_mode = "scanner"   
active_threads = []
sound_alerts = True
max_per_account = 2

# ------------------- دوال تسجيل الدخول وإدارة التوكن -------------------
def login_to_webook(email, password, captcha="", signature="", proxy=None):
    url = "https://api.webook.com/api/v2/login"
    payload = {
        "email": email,
        "password": password,
        "app_source": "rs",
        "login_with": "email",
        "lang": "ar"
    }
    if captcha: payload["captcha"] = captcha
    if signature: payload["signature"] = signature
    
    headers = {
        "Content-Type": "application/json",
        "authorization": "Bearer ",
        "token": "e9aac1f2f0b6c07d6be070ed14829de684264278359148d6a582ca65a50934d2",
        "origin": "https://webook.com"
    }
    proxy_dict = {"http": proxy, "https": proxy} if proxy else None
    try:
        resp = requests.post(url, json=payload, headers=headers, proxies=proxy_dict, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            access_token = data.get('access_token')
            refresh_token = data.get('refresh_token')
            expires_in = data.get('expires_in', 3600)
            expiry = time.time() + expires_in
            return access_token, refresh_token, expiry
        return None, None, None
    except:
        return None, None, None

def ensure_valid_token(account):
    if account['token_expiry'] and time.time() >= account['token_expiry'] - 60:
        access, refresh, expiry = login_to_webook(account['email'], account['password'], account.get('captcha',''), account.get('signature',''), account.get('proxy'))
        if access:
            account['access_token'] = access
            account['refresh_token'] = refresh
            account['token_expiry'] = expiry
            return True
        return False
    return True

# ------------------- دوال SeatCloud API -------------------
def fetch_chart_data(workspace_key, chart_key, account, proxy=None):
    url = f"https://api.seatcloud.com/api/v2/{workspace_key}/map/{chart_key}/data"
    headers = {
        "Authorization": f"Bearer {account['access_token']}",
        "Accept": "application/json",
        "User-Agent": HEADERS["User-Agent"]
    }
    proxy_dict = {"http": proxy, "https": proxy} if proxy else None
    try:
        resp = requests.get(url, headers=headers, proxies=proxy_dict, timeout=5)
        if resp.status_code == 200: return resp.json()
        return None
    except:
        return None

def hold_seats(workspace_key, event_slug, seat_ids, account):
    url = f"https://api.seatcloud.com/api/v2/{workspace_key}/event/{event_slug}/items/hold"
    payload = {
        "hold_token": account.get('hold_token', str(uuid.uuid4())),
        "seats": [{"seat_id": sid} for sid in seat_ids]
    }
    headers = {
        "Authorization": f"Bearer {account['access_token']}",
        "Content-Type": "application/json"
    }
    proxy_dict = {"http": account.get('proxy'), "https": account.get('proxy')} if account.get('proxy') else None
    try:
        resp = requests.post(url, json=payload, headers=headers, proxies=proxy_dict, timeout=5)
        return resp.status_code in [200, 201]
    except:
        return False

def extract_available_seats(chart_data, max_seats=2):
    available = []
    try:
        if isinstance(chart_data, dict):
            seats = chart_data.get('seats', [])
            for seat in seats:
                if seat.get('available') and not seat.get('held'):
                    available.append(seat['id'])
                    if len(available) >= max_seats: break
    except: pass
    return available

def run_scanner_for_account(account, stop_event):
    while not stop_event.is_set() and bot_running:
        if not ensure_valid_token(account):
            time.sleep(5); continue
        chart = fetch_chart_data(BASE_CONFIG['WORKSPACE_KEY'], BASE_CONFIG['CHART_KEY'], account, account.get('proxy'))
        if chart:
            available_seats = extract_available_seats(chart, max_per_account)
            if available_seats:
                if hold_seats(BASE_CONFIG['WORKSPACE_KEY'], BASE_CONFIG['EVENT_SLUG'], available_seats, account):
                    account['status'] = "hold_success"
                    socketio.emit('log', {'message': f"✅ نجاح حجز {account['email']}", 'type': 'success'})
                    socketio.emit('update_accounts', accounts)
                    break
        time.sleep(0.5)

# ------------------- واجهات Flask -------------------
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/start', methods=['POST'])
def start_bot():
    global bot_running, active_threads
    bot_running = True
    for acc in accounts:
        stop_event = threading.Event()
        t = threading.Thread(target=run_scanner_for_account, args=(acc, stop_event))
        t.daemon = True
        t.start()
        active_threads.append((t, stop_event))
    return jsonify({"status": "started"})

@app.route('/api/accounts', methods=['POST'])
def add_accounts():
    global accounts
    data = request.json
    for acc_data in data.get('accounts', []):
        access, refresh, expiry = login_to_webook(acc_data['email'], acc_data['password'])
        if access:
            accounts.append({
                "email": acc_data['email'], "password": acc_data['password'],
                "access_token": access, "token_expiry": expiry, "hold_token": str(uuid.uuid4()),
                "status": "ready"
            })
    socketio.emit('update_accounts', accounts)
    return jsonify({"status": "ok"})

if __name__ == '__main__':
    # استخدام بورت ديناميكي ليتوافق مع Heroku
    port = int(os.environ.get('PORT', 5000))
    socketio.run(app, host='0.0.0.0', port=port)
