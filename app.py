"""
KR Stock Scanner v4 — 주식 + 코인(BTC/ETH/XRP)
설치: pip install finance-datareader pandas flask yfinance
실행: python stock_scanner_v4.py
브라우저: http://localhost:5000
"""
from flask import Flask, render_template_string, jsonify, request, session, redirect, url_for
import hashlib, json, os, secrets, smtplib, threading

# ── 데이터 디렉토리 (환경변수 or 앱 디렉토리) ──
DATA_DIR = os.environ.get("KRSCAN_DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
os.makedirs(DATA_DIR, exist_ok=True)
from email.mime.multipart import MIMEMultipart
from email.mime.text      import MIMEText
from datetime import datetime, timedelta
try:
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron         import CronTrigger
    HAS_SCHEDULER = True
except ImportError:
    HAS_SCHEDULER = False
import FinanceDataReader as fdr
import pandas as pd
import numpy as np
import yfinance as yf
import ccxt
from datetime import datetime, timedelta
import math

app = Flask(__name__)

# ── Secret Key: 고정 키 (재시작해도 세션 유지) ──
_SECRET_FILE = os.path.join(DATA_DIR, ".secret_key")
def _get_secret_key():
    if os.path.exists(_SECRET_FILE):
        with open(_SECRET_FILE,"r") as f:
            return f.read().strip()
    key = secrets.token_hex(32)
    with open(_SECRET_FILE,"w") as f:
        f.write(key)
    return key
app.secret_key = os.environ.get("KRSCAN_SECRET") or _get_secret_key()

# ── 사용자 관리 (users.json) ──────────────────────────────
USERS_FILE = os.path.join(DATA_DIR, "users.json")


ADMIN_ID = "langpoon"   # 관리자 아이디

EMAIL_CFG_FILE  = os.path.join(DATA_DIR, "email_config.json")
SERVER_FAV_FILE = os.path.join(DATA_DIR, "server_favorites.json")

def load_email_cfg():
    if os.path.exists(EMAIL_CFG_FILE):
        try:
            with open(EMAIL_CFG_FILE,"r",encoding="utf-8") as f: return json.load(f)
        except: pass
    return {}

def save_email_cfg(cfg):
    with open(EMAIL_CFG_FILE,"w",encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

def load_server_favs():
    if os.path.exists(SERVER_FAV_FILE):
        try:
            with open(SERVER_FAV_FILE,"r",encoding="utf-8") as f: return json.load(f)
        except: pass
    return []

def save_server_favs(favs):
    with open(SERVER_FAV_FILE,"w",encoding="utf-8") as f:
        json.dump(favs, f, ensure_ascii=False, indent=2)

def load_users():
    if os.path.exists(USERS_FILE):
        try:
            with open(USERS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError):
            # 파일 깨진 경우 백업 후 초기화
            import shutil
            shutil.copy(USERS_FILE, USERS_FILE + ".bak")
            return {}
    return {}

def save_users(users):
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(users, f, ensure_ascii=False, indent=2)

def hash_pw(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

def login_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user"):
            # API 요청이면 JSON 반환, 페이지 요청이면 리다이렉트
            if request.is_json or request.method == "POST":
                return jsonify({"error": "로그인이 필요합니다", "redirect": "/login"}), 401
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if session.get("user") != ADMIN_ID:
            return "❌ 관리자 전용 페이지입니다.", 403
        return f(*args, **kwargs)
    return decorated

# ── Binance 헬퍼 ──────────────────────────────────────────
_bnb = None
_bnb_lock = None

def get_binance():
    """스레드 안전한 바이낸스 인스턴스 반환"""
    global _bnb, _bnb_lock
    import threading
    if _bnb_lock is None:
        _bnb_lock = threading.Lock()
    with _bnb_lock:
        if _bnb is None:
            _bnb = ccxt.binance({
                "enableRateLimit": True,
                "rateLimit": 200,         # 200ms 간격 (기본 1200ms → 6배 빠름)
                "options": {"defaultType": "spot"},
                "timeout": 15000,          # 15초 타임아웃
            })
    return _bnb

COIN_SYMBOL = {
    "BTC-USD": "BTC/USDT",
    "ETH-USD": "ETH/USDT",
    "XRP-USD": "XRP/USDT",
}
COIN_NAME = {
    "BTC-USD": "Bitcoin",
    "ETH-USD": "Ethereum",
    "XRP-USD": "XRP",
}

# ccxt timeframe → (limit, 캔들 수)
BNB_TF_MAP = {
    "1m":  ("1m",  500),
    "5m":  ("5m",  500),
    "15m": ("15m", 500),
    "1h":  ("1h",  500),
    "4h":  ("4h",  500),
    "1d":  ("1d",  500),
    "1wk": ("1w",  200),
    "1mo": ("1M",  60),
}

def binance_price(ticker):
    """현재가 + 24H 변동률 (바이낸스 ticker)"""
    sym = COIN_SYMBOL.get(ticker, ticker.replace("-", "/"))
    name = COIN_NAME.get(ticker, sym.split("/")[0])
    try:
        b = get_binance()
        t = b.fetch_ticker(sym)
        price = float(t["last"])
        chg24 = round(float(t.get("percentage", 0) or 0), 2)
        return price, chg24, name
    except Exception:
        # fallback: 1d 캔들 마지막 종가
        try:
            df = binance_ohlcv(ticker, "1d", limit=2)
            if not df.empty:
                price = float(df["Close"].iloc[-1])
                prev  = float(df["Close"].iloc[-2]) if len(df)>1 else price
                chg24 = round((price-prev)/prev*100,2) if prev else 0
                return price, chg24, name
        except Exception:
            pass
        return 0.0, 0.0, name

def binance_ohlcv(ticker, tf_key, limit=500):
    """바이낸스 OHLCV → DataFrame(Open,High,Low,Close,Volume)"""
    sym = COIN_SYMBOL.get(ticker, ticker.replace("-", "/"))
    bnb_tf, lim = BNB_TF_MAP.get(tf_key, ("1h", limit))
    b = get_binance()
    try:
        raw = b.fetch_ohlcv(sym, timeframe=bnb_tf, limit=min(lim, 500))
    except Exception:
        return pd.DataFrame()
    if not raw:
        return pd.DataFrame()
    df = pd.DataFrame(raw, columns=["timestamp","Open","High","Low","Close","Volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df.set_index("timestamp", inplace=True)
    return df.astype(float)

def is_crypto_ticker(ticker):
    return ticker in COIN_SYMBOL or ticker.endswith("-USD")

# ── FVG 탐지 ──────────────────────────────────────────────
def detect_fvg(df):
    df = df.tail(80).copy()
    if len(df) < 3:
        return {"signal":"NONE","count":0,"gap_high":0,"gap_low":0,"gap_mid":0}
    hi, lo = df["High"].values, df["Low"].values
    bull, bear = [], []
    for i in range(2, len(df)):
        if lo[i] > hi[i-2]:
            m = (float(lo[i]) + float(hi[i-2])) / 2
            bull.append({"high":float(lo[i]),"low":float(hi[i-2]),"mid":m})
        if hi[i] < lo[i-2]:
            m = (float(lo[i-2]) + float(hi[i])) / 2
            bear.append({"high":float(lo[i-2]),"low":float(hi[i]),"mid":m})
    if bull:
        l=bull[-1]; return {"signal":"BULL","count":len(bull),"gap_high":round(l["high"],6),"gap_low":round(l["low"],6),"gap_mid":round(l["mid"],6)}
    if bear:
        l=bear[-1]; return {"signal":"BEAR","count":len(bear),"gap_high":round(l["high"],6),"gap_low":round(l["low"],6),"gap_mid":round(l["mid"],6)}
    return {"signal":"NONE","count":0,"gap_high":0,"gap_low":0,"gap_mid":0}

def calc_setup(fvg_info, price, account=10_000_000, risk_pct=1.0):
    if fvg_info["signal"] == "NONE": return None
    sig = fvg_info["signal"]
    gh, gl, gm = fvg_info["gap_high"], fvg_info["gap_low"], fvg_info["gap_mid"]
    buf = (gh - gl) * 0.05
    if sig == "BULL":
        entry=round(gm,6); sl=round(gl-buf,6); risk=entry-sl
        tp1=round(entry+risk*2,6); tp2=round(entry+risk*3,6); tp3=round(entry+risk*5,6)
        direction="매수 (LONG)"; sl_label="FVG 하단 아래"
        status="✅ FVG 구간 진입 가능" if gl<=price<=gh else "리테스트 대기"
    else:
        entry=round(gm,6); sl=round(gh+buf,6); risk=sl-entry
        tp1=round(entry-risk*2,6); tp2=round(entry-risk*3,6); tp3=round(entry-risk*5,6)
        direction="매도 (SHORT)"; sl_label="FVG 상단 위"
        status="✅ FVG 구간 진입 가능" if gl<=price<=gh else "리테스트 대기"
    if risk<=0: return None
    risk_amt = account*(risk_pct/100)
    shares = max(1,math.floor(risk_amt/risk))
    return {"signal":sig,"direction":direction,"gap_high":gh,"gap_low":gl,"gap_mid":gm,
            "entry":entry,"sl":sl,"sl_label":sl_label,"sl_pct":round(abs(sl-entry)/entry*100,2),
            "tp1":tp1,"tp1_pct":round(abs(tp1-entry)/entry*100,2),
            "tp2":tp2,"tp2_pct":round(abs(tp2-entry)/entry*100,2),
            "tp3":tp3,"tp3_pct":round(abs(tp3-entry)/entry*100,2),
            "shares":shares,"invest_amt":round(shares*entry,2),
            "risk_amt":round(risk_amt,2),"rr":round(abs(tp1-entry)/risk,2),"status":status,"current":price}

def get_yf_data(ticker):
    """주식은 yfinance, 코인은 바이낸스"""
    if is_crypto_ticker(ticker):
        price, chg, name = binance_price(ticker)
        return None, price, chg, name   # stk=None for crypto
    stk = yf.Ticker(ticker)
    info = stk.info
    price = float(info.get("currentPrice") or info.get("regularMarketPrice") or info.get("previousClose") or 0)
    prev  = float(info.get("regularMarketPreviousClose") or info.get("previousClose") or price)
    chg   = round((price - prev) / prev * 100, 2) if prev else 0
    name  = info.get("longName") or info.get("shortName") or ticker.split(".")[0]
    return stk, price, chg, name

def fetch_all_tf(stk_or_ticker, ticker=None):
    """주식은 yfinance stk 객체, 코인은 ticker 문자열로 바이낸스 호출"""
    # 코인 경로
    if ticker and is_crypto_ticker(ticker):
        tf_keys = ["1mo","1wk","1d","4h","1h","15m","5m","1m"]
        tf_data = {}
        for tf_key in tf_keys:
            try:
                df = binance_ohlcv(ticker, tf_key)
                tf_data[tf_key] = detect_fvg(df) if not df.empty else \
                    {"signal":"NONE","count":0,"gap_high":0,"gap_low":0,"gap_mid":0}
            except Exception:
                tf_data[tf_key] = {"signal":"NONE","count":0,"gap_high":0,"gap_low":0,"gap_mid":0}
        return tf_data

    # 주식 경로 (yfinance)
    stk = stk_or_ticker
    period_map = {"1mo":"5y","1wk":"2y","1d":"1y","1h":"60d","15m":"5d","5m":"5d","1m":"1d"}
    configs = [("1mo","1mo"),("1wk","1wk"),("1d","1d"),("1h","4h"),("1h","1h"),("15m","15m"),("5m","5m"),("1m","1m")]
    tf_data = {}; done4h = False
    for yf_iv, tf_key in configs:
        try:
            df = stk.history(period=period_map[yf_iv], interval=yf_iv)
            if df.empty or len(df) < 3:
                tf_data[tf_key] = {"signal":"NONE","count":0,"gap_high":0,"gap_low":0,"gap_mid":0}; continue
            if tf_key == "4h" and not done4h:
                df4 = df.resample("4h").agg({"Open":"first","High":"max","Low":"min","Close":"last","Volume":"sum"}).dropna()
                tf_data["4h"] = detect_fvg(df4); done4h = True; continue
            if tf_key not in tf_data:
                tf_data[tf_key] = detect_fvg(df)
        except Exception:
            tf_data[tf_key] = {"signal":"NONE","count":0,"gap_high":0,"gap_low":0,"gap_mid":0}
    return tf_data

# ── HTML ──────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>KRSCAN v4 — 주식+코인</title>
<link href="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=Noto+Sans+KR:wght@300;400;500;700&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>
:root{--bg:#07090d;--s1:#0d1117;--s2:#161b22;--s3:#1c2128;--bd:#21262d;--bd2:#30363d;
  --green:#00ff88;--red:#ff4757;--yellow:#ffa502;--blue:#58a6ff;--purple:#c084fc;--orange:#ff6b35;
  --eth:#627eea;--xrp:#00aae4;--txt:#e6edf3;--muted:#7d8590;--muted2:#484f58}
*{margin:0;padding:0;box-sizing:border-box}
body{background:var(--bg);color:var(--txt);font-family:'Noto Sans KR',sans-serif;min-height:100vh}
body::before{content:'';position:fixed;inset:0;background-image:linear-gradient(rgba(0,255,136,.018) 1px,transparent 1px),linear-gradient(90deg,rgba(0,255,136,.018) 1px,transparent 1px);background-size:48px 48px;pointer-events:none;z-index:0}
.wrap{position:relative;z-index:1;max-width:1380px;margin:0 auto;padding:24px 20px}

header{display:flex;justify-content:space-between;align-items:flex-end;margin-bottom:20px;padding-bottom:16px;border-bottom:1px solid var(--bd)}
.logo{font-family:'Bebas Neue';font-size:42px;letter-spacing:3px;line-height:1}
.logo .kr{color:var(--green)}.logo .sc{color:var(--red)}
.hright{text-align:right;font-size:10px;color:var(--muted);letter-spacing:2px}
.now{font-family:'JetBrains Mono';font-size:11px;color:var(--muted);margin-top:3px}

.tabs{display:flex;gap:2px;border-bottom:1px solid var(--bd);flex-wrap:wrap}
.tab{padding:10px 20px;font-size:12px;font-weight:500;cursor:pointer;border:1px solid transparent;border-bottom:none;border-radius:6px 6px 0 0;color:var(--muted);transition:all .2s;position:relative;bottom:-1px;white-space:nowrap}
.tab:hover{color:var(--txt);background:var(--s2)}
.tab.active{background:var(--s2);color:var(--txt);border-color:var(--bd);border-bottom-color:var(--s2)}
.t-surge.active{border-top:2px solid var(--green)}.t-high.active{border-top:2px solid var(--yellow)}
/* ── VOL SCAN ── */
.vs-table-wrap{background:var(--s2);border:1px solid var(--bd);border-radius:12px;overflow:hidden;margin-top:14px}
.vs-thead{display:grid;grid-template-columns:32px 44px 72px 1fr 90px 84px 90px 90px 80px;padding:9px 14px;background:var(--s3);border-bottom:1px solid var(--bd);font-size:10px;color:var(--muted);letter-spacing:1px;text-transform:uppercase;gap:4px}
.vs-row{display:grid;grid-template-columns:32px 44px 72px 1fr 90px 84px 90px 90px 80px;padding:10px 14px;border-bottom:1px solid var(--bd);font-size:13px;gap:4px;animation:fr .2s ease both;transition:background .12s}
.vs-row:last-child{border-bottom:none}.vs-row:hover{background:rgba(255,255,255,.025)}
.vs-rank{font-family:'JetBrains Mono';font-size:11px;color:var(--muted2);font-weight:700}
.vs-code{font-family:'JetBrains Mono';font-size:11px;color:var(--muted)}
.vs-name{font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.vs-price{font-family:'JetBrains Mono';font-size:12px;font-weight:600;text-align:right}
.vs-chg{font-family:'JetBrains Mono';font-size:12px;font-weight:700;text-align:right}
.vs-ratio{font-family:'JetBrains Mono';font-size:13px;font-weight:700;text-align:right}
.vs-ratio.hot{color:#fb923c}.vs-ratio.warm{color:var(--yellow)}.vs-ratio.norm{color:var(--muted)}
.vs-bull{font-size:11px;text-align:right;font-family:'JetBrains Mono'}
.vs-vol{font-family:'JetBrains Mono';font-size:11px;color:var(--muted);text-align:right}
.vs-bar-wrap{height:3px;background:var(--bd);border-radius:2px;margin-top:3px;overflow:hidden}
.vs-bar-fill{height:100%;border-radius:2px}
.vs-badge{font-size:10px;font-weight:700;padding:2px 7px;border-radius:10px}
.vs-badge.kp{background:rgba(88,166,255,.15);color:var(--blue);border:1px solid rgba(88,166,255,.25)}
.vs-badge.kq{background:rgba(192,132,252,.12);color:var(--purple);border:1px solid rgba(192,132,252,.2)}
.vs-summary{display:flex;gap:14px;flex-wrap:wrap;padding:11px 16px;background:var(--s3);border-bottom:1px solid var(--bd);font-size:12px;align-items:center}
.vs-sum-lbl{color:var(--muted);font-size:11px;margin-right:3px}

/* ── CROSS SCAN ── */
.cross-opts{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:14px}
.cross-opt{padding:12px 16px;border-radius:10px;border:1px solid var(--bd);background:var(--s1);cursor:pointer;transition:all .2s;min-width:160px;position:relative}
.cross-opt:hover{border-color:var(--bd2);background:var(--s2)}
.cross-opt.sel{border-color:#06b6d4;background:rgba(6,182,212,.07)}
.cross-opt-icon{font-size:22px;display:block;margin-bottom:6px}
.cross-opt-name{font-size:13px;font-weight:700;color:var(--txt)}
.cross-opt-desc{font-size:11px;color:var(--muted);margin-top:3px;line-height:1.4}
.cross-opt-check{position:absolute;top:8px;right:10px;width:18px;height:18px;border-radius:50%;background:var(--bd);border:2px solid var(--bd2);display:flex;align-items:center;justify-content:center;font-size:10px;transition:all .2s}
.cross-opt.sel .cross-opt-check{background:#06b6d4;border-color:#06b6d4;color:#000;content:'✓'}
.cross-prog{background:var(--s2);border:1px solid var(--bd);border-radius:10px;padding:18px;margin-bottom:14px;text-align:center}
.cross-prog-lbl{font-size:12px;color:var(--muted);margin-top:8px}
.cross-prog-detail{font-size:11px;color:var(--muted2);margin-top:4px;font-family:'JetBrains Mono'}
.cross-prog-bar{background:var(--bd);border-radius:4px;height:6px;overflow:hidden;margin:10px 0}
.cross-prog-fill{height:100%;background:linear-gradient(90deg,#06b6d4,#a78bfa);border-radius:4px;transition:width .4s}
.cross-result-header{display:flex;align-items:center;gap:10px;padding:12px 16px;background:var(--s3);border-bottom:1px solid var(--bd);flex-wrap:wrap}
.cross-tag{font-size:11px;font-weight:700;padding:3px 10px;border-radius:12px}
.cross-tag.surge{background:rgba(0,255,136,.12);color:var(--green);border:1px solid rgba(0,255,136,.25)}
.cross-tag.high{background:rgba(255,165,2,.12);color:var(--yellow);border:1px solid rgba(255,165,2,.25)}
.cross-tag.vol{background:rgba(251,146,60,.12);color:#fb923c;border:1px solid rgba(251,146,60,.25)}
.cross-thead{display:grid;grid-template-columns:44px 72px 1fr 80px 80px 80px 80px 80px 80px;padding:9px 14px;background:var(--s3);border-bottom:1px solid var(--bd);font-size:10px;color:var(--muted);letter-spacing:1px;text-transform:uppercase;gap:4px}
.cross-row{display:grid;grid-template-columns:44px 72px 1fr 80px 80px 80px 80px 80px 80px;padding:10px 14px;border-bottom:1px solid var(--bd);font-size:12px;gap:4px;animation:fr .2s ease both}
@media(max-width:900px){
  .cross-thead{grid-template-columns:40px 66px 1fr 70px 70px 70px 70px 70px 70px}
  .cross-row{grid-template-columns:40px 66px 1fr 70px 70px 70px 70px 70px 70px}
}
.cross-row:last-child{border-bottom:none}
.cross-row:hover{background:rgba(255,255,255,.025)}
.cross-badge-wrap{display:flex;gap:3px;flex-wrap:wrap}
.cross-b{font-size:9px;font-weight:700;padding:1px 5px;border-radius:8px}
.cross-b.surge{background:rgba(0,255,136,.15);color:var(--green)}
.cross-b.high{background:rgba(255,165,2,.15);color:var(--yellow)}
.cross-b.vol{background:rgba(251,146,60,.15);color:#fb923c}
.cross-match-3{background:linear-gradient(135deg,rgba(6,182,212,.1),rgba(167,139,250,.1));border-left:3px solid #06b6d4!important}
.cross-match-2{border-left:3px solid rgba(255,165,2,.6)!important}

/* ── FVG 재진입 스캐너 ── */
.fs-table-wrap{background:var(--s2);border:1px solid var(--bd);border-radius:12px;overflow:hidden;margin-top:14px}
.fs-thead{display:grid;grid-template-columns:44px 72px 1fr 90px 80px 90px 90px 100px;padding:9px 14px;background:var(--s3);border-bottom:1px solid var(--bd);font-size:10px;color:var(--muted);letter-spacing:1px;text-transform:uppercase;gap:4px}
.fs-row{display:grid;grid-template-columns:44px 72px 1fr 90px 80px 90px 90px 100px;padding:10px 14px;border-bottom:1px solid var(--bd);font-size:12px;gap:4px;animation:fr .18s ease both;transition:background .12s}
.fs-row:last-child{border-bottom:none}
.fs-row:hover{background:rgba(255,255,255,.025)}
.fs-row.bull{border-left:3px solid rgba(0,255,136,.5)}
.fs-row.bear{border-left:3px solid rgba(255,71,87,.5)}
.fs-rank{font-family:'JetBrains Mono';font-size:11px;color:var(--muted2);font-weight:700}
.fs-code{font-family:'JetBrains Mono';font-size:11px;color:var(--muted)}
.fs-name{font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.fs-price{font-family:'JetBrains Mono';font-size:12px;font-weight:600;text-align:right}
.fs-chg{font-family:'JetBrains Mono';font-size:12px;font-weight:700;text-align:right}
.fs-fvg{font-family:'JetBrains Mono';font-size:11px;text-align:right}
.fs-near{font-family:'JetBrains Mono';font-size:12px;font-weight:700;text-align:right}
.fs-near.hot{color:#fb923c}.fs-near.warm{color:var(--yellow)}.fs-near.ok{color:var(--green)}
.fs-type{text-align:center}
.fs-type-badge{font-size:10px;font-weight:700;padding:2px 8px;border-radius:10px;white-space:nowrap}
.fs-type-badge.bull{background:rgba(0,255,136,.12);color:var(--green);border:1px solid rgba(0,255,136,.25)}
.fs-type-badge.bear{background:rgba(255,71,87,.1);color:var(--red);border:1px solid rgba(255,71,87,.2)}
.fs-summary{display:flex;gap:14px;flex-wrap:wrap;padding:11px 16px;background:var(--s3);border-bottom:1px solid var(--bd);font-size:12px;align-items:center}
.fs-sum-lbl{color:var(--muted);font-size:11px;margin-right:3px}
.fs-sum-val{font-family:'JetBrains Mono';font-weight:700}
.fs-tf-badge{font-size:10px;font-weight:700;padding:2px 8px;border-radius:10px;background:rgba(88,166,255,.12);color:var(--blue);border:1px solid rgba(88,166,255,.2)}

/* ── 즐겨찾기 ── */
.fav-list{margin-top:0}
.fav-group-title{padding:12px 16px 8px;font-size:10px;color:var(--muted);letter-spacing:2px;text-transform:uppercase;border-bottom:1px solid var(--bd)}
.fav-row{display:grid;grid-template-columns:32px 68px 1fr 90px 90px 110px 100px 40px;align-items:center;padding:10px 14px;border-bottom:1px solid var(--bd);font-size:12px;gap:6px;transition:background .12s;animation:fr .18s ease both}
.fav-row:last-child{border-bottom:none}
.fav-row:hover{background:rgba(255,255,255,.025)}
.fav-star{font-size:16px;cursor:pointer;opacity:.6;transition:all .2s;text-align:center;user-select:none}
.fav-star:hover{opacity:1;transform:scale(1.2)}
.fav-star.active{opacity:1;color:#ffd700}
.fav-code{font-family:'JetBrains Mono';font-size:11px;color:var(--muted)}
.fav-name{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.fav-price{font-family:'JetBrains Mono';font-size:12px;font-weight:600;text-align:right}
.fav-chg{font-family:'JetBrains Mono';font-size:12px;font-weight:700;text-align:right}
.fav-from{font-size:10px;padding:2px 8px;border-radius:10px;font-weight:700;white-space:nowrap}
.fav-from.surge{background:rgba(0,255,136,.12);color:var(--green);border:1px solid rgba(0,255,136,.25)}
.fav-from.high{background:rgba(255,165,2,.12);color:var(--yellow);border:1px solid rgba(255,165,2,.25)}
.fav-from.volscan{background:rgba(251,146,60,.12);color:#fb923c;border:1px solid rgba(251,146,60,.25)}
.fav-from.cross{background:rgba(6,182,212,.12);color:#06b6d4;border:1px solid rgba(6,182,212,.25)}
.fav-from.fvgscan{background:rgba(88,166,255,.12);color:var(--blue);border:1px solid rgba(88,166,255,.25)}
.fav-time{font-size:10px;color:var(--muted2);font-family:'JetBrains Mono'}
.fav-del{font-size:14px;cursor:pointer;color:var(--muted2);text-align:center;transition:all .2s}
.fav-del:hover{color:var(--red)}
.fav-header{display:flex;align-items:center;justify-content:space-between;padding:12px 16px;background:var(--s3);border-bottom:1px solid var(--bd)}
.fav-thead{display:grid;grid-template-columns:32px 68px 1fr 90px 90px 110px 100px 40px;padding:8px 14px;background:var(--s3);border-bottom:1px solid var(--bd);font-size:10px;color:var(--muted);letter-spacing:1px;text-transform:uppercase;gap:6px}
.fav-empty{padding:48px;text-align:center;color:var(--muted)}
.fav-empty-icon{font-size:40px;display:block;margin-bottom:12px;opacity:.3}

/* ── 즐겨찾기 히스토리 히트맵 ── */
.hist-wrap{background:var(--s2);border:1px solid var(--bd);border-radius:12px;overflow:hidden;margin-top:14px}
.hist-header{display:flex;align-items:center;justify-content:space-between;padding:12px 16px;background:var(--s3);border-bottom:1px solid var(--bd)}
.hist-tabs{display:flex;gap:6px}
.hist-tab{padding:5px 14px;border-radius:6px;font-size:12px;font-weight:700;cursor:pointer;border:1px solid var(--bd);color:var(--muted);background:transparent;transition:all .2s}
.hist-tab.active{background:var(--s2);color:var(--txt);border-color:var(--bd2)}
.hist-date-row{display:flex;align-items:center;padding:0 16px;background:var(--s3);border-bottom:1px solid var(--bd);overflow-x:auto}
.hist-date-row::-webkit-scrollbar{height:0}
.hist-corner{width:280px;min-width:280px;font-size:10px;color:var(--muted);padding:8px 0;letter-spacing:1px;text-transform:uppercase;flex-shrink:0}
.hist-dates{display:flex;gap:3px;padding:6px 0}
.hist-date-lbl{width:32px;min-width:32px;font-size:10px;color:var(--muted2);text-align:center;font-family:'JetBrains Mono';line-height:1.2}
.hist-stock-row{display:flex;align-items:center;padding:0 16px;border-bottom:1px solid var(--bd);transition:background .12s}
.hist-stock-row:last-child{border-bottom:none}
.hist-stock-row:hover{background:rgba(255,255,255,.025)}
.hist-stock-info{width:280px;min-width:280px;display:flex;align-items:center;gap:10px;padding:10px 0;flex-shrink:0}
.hist-stock-name{font-size:13px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.hist-stock-sub{font-size:10px;color:var(--muted);margin-top:1px}
.hist-stock-pct{font-family:'JetBrains Mono';font-size:12px;font-weight:700;text-align:right;min-width:54px}
.hist-cells{display:flex;gap:3px;padding:6px 0;align-items:center}
.hist-cell{width:32px;min-width:32px;height:32px;border-radius:4px;display:flex;align-items:center;justify-content:center;font-size:9px;font-family:'JetBrains Mono';font-weight:700;cursor:default;transition:transform .1s;position:relative}
.hist-cell:hover{transform:scale(1.2);z-index:10}
.hist-cell.up-3{background:rgba(0,200,100,.9);color:#003311}
.hist-cell.up-2{background:rgba(0,200,100,.6);color:#003311}
.hist-cell.up-1{background:rgba(0,200,100,.3);color:var(--green)}
.hist-cell.dn-3{background:rgba(255,60,60,.9);color:#330000}
.hist-cell.dn-2{background:rgba(255,60,60,.6);color:#330000}
.hist-cell.dn-1{background:rgba(255,60,60,.25);color:var(--red)}
.hist-cell.flat{background:rgba(255,255,255,.06);color:var(--muted)}
.hist-cell.nodata{background:transparent;color:var(--bd)}
.hist-legend{display:flex;align-items:center;gap:6px;font-size:10px;color:var(--muted)}
.hist-legend-cell{width:16px;height:16px;border-radius:3px;display:inline-block}
.hist-summary-row{display:flex;gap:8px;padding:6px 0;align-items:center}
.hist-sum-bar{height:4px;border-radius:2px;flex:1;background:var(--bd)}
.hist-sum-fill{height:100%;border-radius:2px;background:var(--green)}

/* ── 상승 준비 구간 스캐너 ── */
.bo-table-wrap{background:var(--s2);border:1px solid var(--bd);border-radius:12px;overflow:hidden;margin-top:14px}
.bo-thead{display:grid;grid-template-columns:32px 44px 72px 1fr 80px 80px 70px 70px 80px 80px;padding:9px 14px;background:var(--s3);border-bottom:1px solid var(--bd);font-size:10px;color:var(--muted);letter-spacing:1px;text-transform:uppercase;gap:4px}
.bo-row{display:grid;grid-template-columns:32px 44px 72px 1fr 80px 80px 70px 70px 80px 80px;padding:10px 14px;border-bottom:1px solid var(--bd);font-size:12px;gap:4px;animation:fr .18s ease both;transition:background .12s}
.bo-row:last-child{border-bottom:none}
.bo-row:hover{background:rgba(255,255,255,.025)}

/* ── 고확신 신호 ── */
.hcs-card{background:var(--s2);border:1px solid var(--bd);border-radius:12px;overflow:hidden;margin-bottom:10px;animation:fr .25s ease both;transition:background .12s}
.hcs-card:hover{background:var(--s3)}
.hcs-card.combo-a{border-left:4px solid #fb923c}
.hcs-card.combo-b{border-left:4px solid #a78bfa}
.hcs-card.combo-c{border-left:4px solid #06b6d4}
.hcs-card-head{display:flex;align-items:center;gap:12px;padding:14px 18px;border-bottom:1px solid var(--bd)}
.hcs-badge{font-size:11px;font-weight:800;padding:4px 12px;border-radius:20px;white-space:nowrap}
.hcs-badge.a{background:rgba(251,146,60,.15);color:#fb923c;border:1px solid rgba(251,146,60,.3)}
.hcs-badge.b{background:rgba(167,139,250,.12);color:#a78bfa;border:1px solid rgba(167,139,250,.25)}
.hcs-badge.c{background:rgba(6,182,212,.12);color:#06b6d4;border:1px solid rgba(6,182,212,.25)}
.hcs-name{font-size:17px;font-weight:700}
.hcs-sub{font-size:11px;color:var(--muted);margin-top:2px}
.hcs-price{font-family:'JetBrains Mono';font-size:18px;font-weight:800;margin-left:auto}
.hcs-chg{font-family:'JetBrains Mono';font-size:14px;font-weight:700;margin-left:8px}
.hcs-body{padding:12px 18px;display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
.hcs-cond{background:var(--s1);border:1px solid var(--bd);border-radius:8px;padding:10px 12px}
.hcs-cond.met{border-color:rgba(0,255,136,.3);background:rgba(0,255,136,.04)}
.hcs-cond.unmet{border-color:var(--bd);opacity:.5}
.hcs-cond-icon{font-size:16px;margin-bottom:4px}
.hcs-cond-name{font-size:11px;font-weight:700;color:var(--txt)}
.hcs-cond-val{font-size:11px;font-family:'JetBrains Mono';color:var(--muted);margin-top:2px}
.hcs-summary{display:flex;gap:10px;align-items:center;padding:10px 18px;background:var(--s3);border-top:1px solid var(--bd);flex-wrap:wrap}
.hcs-strategy-label{font-size:13px;font-weight:700;color:var(--txt)}
.hcs-strategy-desc{font-size:11px;color:var(--muted);margin-top:2px;line-height:1.6}
.hcs-win-rate{font-family:'Bebas Neue';font-size:28px;color:#ffa502;margin-left:auto}
.hcs-win-lbl{font-size:10px;color:var(--muted);text-align:right;letter-spacing:1px}
/* 조건 알림 */
.alert-card{background:var(--s2);border:1px solid var(--bd);border-radius:10px;padding:14px 16px;margin-bottom:8px;display:flex;align-items:center;gap:10px;animation:fr .2s ease}
.alert-active{border-color:rgba(0,255,136,.3)}
.alert-inactive{opacity:.55}
.alert-dot{width:8px;height:8px;border-radius:50%;flex-shrink:0}
.alert-dot.on{background:#00ff88}
.alert-dot.off{background:var(--muted2)}
.alert-name{font-size:13px;font-weight:700;flex:1}
.alert-cond{font-size:11px;color:var(--muted);margin-top:2px}
.alert-toggle{padding:4px 12px;border-radius:6px;font-size:11px;font-weight:700;cursor:pointer;border:1px solid;transition:all .2s}
.alert-toggle.on{background:rgba(255,71,87,.1);color:var(--red);border-color:rgba(255,71,87,.25)}
.alert-toggle.off{background:rgba(0,255,136,.1);color:var(--green);border-color:rgba(0,255,136,.25)}
.bo-score-bar{width:100%;height:5px;background:var(--bd);border-radius:3px;margin-top:3px;overflow:hidden}
.bo-score-fill{height:100%;border-radius:3px}
.bo-score{font-family:'JetBrains Mono';font-size:13px;font-weight:800;text-align:right}
.bo-score.s5{color:#00ff88}.bo-score.s4{color:#4ade80}.bo-score.s3{color:#ffa502}
.bo-score.s2{color:#fb923c}.bo-score.s1{color:var(--muted)}
.bo-rsi{font-family:'JetBrains Mono';font-size:11px;text-align:right}
.bo-fvg{font-family:'JetBrains Mono';font-size:11px;font-weight:700;text-align:center}
.bo-fvg.many{color:#00ff88}.bo-fvg.some{color:var(--yellow)}.bo-fvg.few{color:var(--muted)}
.bo-summary{display:flex;gap:14px;flex-wrap:wrap;padding:11px 16px;background:var(--s3);border-bottom:1px solid var(--bd);font-size:12px;align-items:center}
.bo-cond-tags{display:flex;gap:3px;flex-wrap:wrap}

/* ── 고승률 자리 스캐너 ── */
.hp-combo-cards{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:16px}
@media(max-width:900px){.hp-combo-cards{grid-template-columns:1fr}}
.hp-card{background:var(--s2);border:1px solid var(--bd);border-radius:12px;padding:16px 18px;cursor:pointer;transition:all .2s;position:relative;overflow:hidden}
.hp-card:hover{border-color:var(--bd2);background:var(--s3)}
.hp-card.sel{border-width:2px}
.hp-card.sel-a{border-color:rgba(0,255,136,.5);background:rgba(0,255,136,.05)}
.hp-card.sel-b{border-color:rgba(167,139,250,.5);background:rgba(167,139,250,.05)}
.hp-card.sel-c{border-color:rgba(251,146,60,.5);background:rgba(251,146,60,.05)}
.hp-card-icon{font-size:28px;margin-bottom:8px}
.hp-card-title{font-family:'Bebas Neue';font-size:18px;letter-spacing:1px;margin-bottom:6px}
.hp-card.sel-a .hp-card-title{color:var(--green)}
.hp-card.sel-b .hp-card-title{color:#a78bfa}
.hp-card.sel-c .hp-card-title{color:#fb923c}
.hp-card-conds{display:flex;flex-direction:column;gap:4px;margin-top:8px}
.hp-cond-item{font-size:11px;color:var(--muted);display:flex;align-items:center;gap:5px}
.hp-cond-item::before{content:'✅';font-size:10px}
.hp-winrate{position:absolute;top:12px;right:12px;font-family:'Bebas Neue';font-size:22px}
.hp-card.sel-a .hp-winrate{color:var(--green)}
.hp-card.sel-b .hp-winrate{color:#a78bfa}
.hp-card.sel-c .hp-winrate{color:#fb923c}
.hp-table-wrap{background:var(--s2);border:1px solid var(--bd);border-radius:12px;overflow:hidden;margin-top:14px}
.hp-thead{display:grid;grid-template-columns:32px 44px 72px 1fr 80px 80px 70px 70px 110px 36px;padding:9px 14px;background:var(--s3);border-bottom:1px solid var(--bd);font-size:10px;color:var(--muted);letter-spacing:1px;text-transform:uppercase;gap:4px}
.hp-row{display:grid;grid-template-columns:32px 44px 72px 1fr 80px 80px 70px 70px 110px 36px;padding:10px 14px;border-bottom:1px solid var(--bd);font-size:12px;gap:4px;animation:fr .18s ease both;transition:background .12s}
.hp-row:last-child{border-bottom:none}
.hp-row:hover{background:rgba(255,255,255,.025)}
.hp-score{font-family:'Bebas Neue';font-size:20px;text-align:center}
.hp-score.s3{color:var(--green)}.hp-score.s2{color:var(--yellow)}.hp-score.s1{color:var(--muted)}
.hp-badge-wrap{display:flex;gap:3px;flex-wrap:wrap}
.hp-b{font-size:9px;font-weight:700;padding:1px 5px;border-radius:6px}
.hp-b.surge{background:rgba(0,255,136,.15);color:var(--green)}
.hp-b.fvg{background:rgba(88,166,255,.15);color:var(--blue)}
.hp-b.vol{background:rgba(251,146,60,.15);color:#fb923c}
.hp-b.breakout{background:rgba(167,139,250,.15);color:#a78bfa}
.hp-b.retr{background:rgba(255,165,2,.15);color:var(--yellow)}
.bo-tag{font-size:9px;font-weight:700;padding:1px 6px;border-radius:8px}
.bo-tag.ok{background:rgba(0,255,136,.12);color:var(--green);border:1px solid rgba(0,255,136,.2)}
.bo-tag.warn{background:rgba(255,165,2,.1);color:var(--yellow);border:1px solid rgba(255,165,2,.2)}
.vs-sum-val{font-family:'JetBrains Mono';font-weight:700}
/* ── VOL SCAN ── */
.vs-table-wrap{background:var(--s2);border:1px solid var(--bd);border-radius:12px;overflow:hidden;margin-top:14px}
.vs-thead{display:grid;grid-template-columns:50px 80px 1fr 100px 90px 90px 90px 90px;padding:9px 14px;background:var(--s3);border-bottom:1px solid var(--bd);font-size:10px;color:var(--muted);letter-spacing:1px;text-transform:uppercase;gap:4px;cursor:pointer}
.vs-thead span:hover{color:var(--txt)}
.vs-row{display:grid;grid-template-columns:50px 80px 1fr 100px 90px 90px 90px 90px;padding:10px 14px;border-bottom:1px solid var(--bd);font-size:13px;gap:4px;animation:fr .2s ease both;transition:background .12s;cursor:pointer}
.vs-row:last-child{border-bottom:none}
.vs-row:hover{background:rgba(255,255,255,.025)}
.vs-rank{font-family:'JetBrains Mono';font-size:11px;color:var(--muted2);font-weight:700}
.vs-code{font-family:'JetBrains Mono';font-size:11px;color:var(--muted)}
.vs-name{font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.vs-price{font-family:'JetBrains Mono';font-size:12px;font-weight:600;text-align:right}
.vs-chg{font-family:'JetBrains Mono';font-size:12px;font-weight:700;text-align:right}
.vs-ratio{font-family:'JetBrains Mono';font-size:13px;font-weight:700;text-align:right}
.vs-ratio.hot{color:#fb923c}.vs-ratio.warm{color:var(--yellow)}.vs-ratio.cool{color:var(--muted)}
.vs-bull{font-size:11px;text-align:right}.vs-vol{font-family:'JetBrains Mono';font-size:11px;color:var(--muted);text-align:right}
.vs-bar-wrap{height:4px;background:var(--bd);border-radius:2px;margin-top:3px;overflow:hidden}
.vs-bar-fill{height:100%;border-radius:2px}
.vs-badge{font-size:10px;font-weight:700;padding:2px 7px;border-radius:10px;white-space:nowrap}
.vs-badge.mkt-kp{background:rgba(88,166,255,.15);color:var(--blue);border:1px solid rgba(88,166,255,.25)}
.vs-badge.mkt-kq{background:rgba(192,132,252,.12);color:var(--purple);border:1px solid rgba(192,132,252,.2)}
.vs-summary{display:flex;gap:12px;flex-wrap:wrap;padding:12px 16px;background:var(--s3);border-bottom:1px solid var(--bd);font-size:12px;align-items:center}
.vs-sum-item{display:flex;align-items:center;gap:5px}
.vs-sum-lbl{color:var(--muted);font-size:11px}
.vs-sum-val{font-family:'JetBrains Mono';font-weight:700}
/* ── GROUPED TABS ── */
.tabs-main{display:flex;gap:0;background:var(--s1);border-bottom:2px solid var(--bd);flex-wrap:wrap}
.mtab{padding:13px 24px;font-size:13px;font-weight:600;cursor:pointer;color:var(--muted);transition:all .2s;border-bottom:3px solid transparent;white-space:nowrap}
.mtab:hover{color:var(--txt);background:rgba(255,255,255,.03)}
.mtab.active{color:var(--txt)}
.mtab#mg-scanner.active{border-bottom-color:var(--green);color:var(--green)}
.mtab#mg-setup.active{border-bottom-color:var(--orange);color:var(--orange)}
.mtab#mg-market.active{border-bottom-color:var(--blue);color:var(--blue)}
.mtab#mg-guide.active{border-bottom-color:var(--purple);color:var(--purple)}
.mtab#mg-bt.active{border-bottom-color:#e879f9;color:#e879f9}
.tabs-sub{display:flex;gap:3px;background:var(--s2);border-bottom:1px solid var(--bd);padding:5px 8px;flex-wrap:wrap;animation:fr .15s ease}
.stab{padding:7px 16px;font-size:12px;font-weight:500;cursor:pointer;border-radius:5px;color:var(--muted);transition:all .15s;border:1px solid transparent;white-space:nowrap}
.stab:hover{color:var(--txt);background:rgba(255,255,255,.04)}
.stab.active{color:var(--txt);background:var(--s3);border-color:var(--bd2)}
.bts-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(155px,1fr));gap:9px;margin-bottom:14px}
.bts-btn{padding:12px 10px;border-radius:9px;font-size:11px;cursor:pointer;border:1px solid var(--bd);background:var(--s1);color:var(--muted);transition:all .2s;text-align:center;font-family:'Noto Sans KR'}
.bts-btn:hover{border-color:var(--bd2);color:var(--txt)}
.bts-btn.sel{border-color:#e879f9;color:#e879f9;background:rgba(232,121,249,.08)}
.bts-icon{font-size:22px;display:block;margin-bottom:5px}
.bts-name{font-size:12px;font-weight:700;display:block}
.bts-desc{font-size:10px;color:var(--muted);display:block;margin-top:3px;line-height:1.4}

/* ── GROUPED TABS ── */
.tabs-main{display:flex;gap:0;background:var(--s1);border-bottom:2px solid var(--bd);flex-wrap:wrap}
.mtab{padding:13px 24px;font-size:13px;font-weight:600;cursor:pointer;color:var(--muted);transition:all .2s;border-bottom:3px solid transparent;white-space:nowrap}
.mtab:hover{color:var(--txt);background:rgba(255,255,255,.03)}
.mtab.active{color:var(--txt)}
.mtab#mg-scanner.active{border-bottom-color:var(--green);color:var(--green)}
.mtab#mg-setup.active{border-bottom-color:var(--orange);color:var(--orange)}
.mtab#mg-market.active{border-bottom-color:var(--blue);color:var(--blue)}
.mtab#mg-guide.active{border-bottom-color:var(--purple);color:var(--purple)}
.mtab#mg-bt.active{border-bottom-color:#e879f9;color:#e879f9}
.tabs-sub{display:flex;gap:3px;background:var(--s2);border-bottom:1px solid var(--bd);padding:5px 8px;flex-wrap:wrap;animation:fr .15s ease}
.stab{padding:7px 16px;font-size:12px;font-weight:500;cursor:pointer;border-radius:5px;color:var(--muted);transition:all .15s;border:1px solid transparent;white-space:nowrap}
.stab:hover{color:var(--txt);background:rgba(255,255,255,.04)}
.stab.active{color:var(--txt);background:var(--s3);border-color:var(--bd2)}
/* backtest card strategy buttons */
.bts-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(155px,1fr));gap:9px;margin-bottom:14px}
.bts-btn{padding:12px 10px;border-radius:9px;font-size:11px;cursor:pointer;border:1px solid var(--bd);background:var(--s1);color:var(--muted);transition:all .2s;text-align:center;font-family:'Noto Sans KR'}
.bts-btn:hover{border-color:var(--bd2);color:var(--txt)}
.bts-btn.sel{border-color:#e879f9;color:#e879f9;background:rgba(232,121,249,.08)}
.bts-icon{font-size:22px;display:block;margin-bottom:5px}
.bts-name{font-size:12px;font-weight:700;display:block}
.bts-desc{font-size:10px;color:var(--muted);display:block;margin-top:3px;line-height:1.4}
.t-fvg.active{border-top:2px solid var(--blue)}.t-guide.active{border-top:2px solid var(--purple)}
.t-setup.active{border-top:2px solid var(--orange)}
.t-trend.active{border-top:2px solid #a78bfa}
.t-vwap.active{border-top:2px solid #06b6d4}
.t-combo.active{border-top:2px solid #f43f5e}
.t-backtest.active{border-top:2px solid #e879f9}
.t-emafvg.active{border-top:2px solid #34d399}
.t-krma.active{border-top:2px solid #fb923c}

/* ── KR MA+VOL ── */
/* ── 수급 (외국인/기관) ── */
.inv-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:14px}
.inv-card{background:var(--s2);border:1px solid var(--bd);border-radius:11px;overflow:hidden;animation:fr .3s ease}
.inv-head{padding:11px 15px;background:var(--s3);border-bottom:1px solid var(--bd);display:flex;align-items:center;justify-content:space-between}
.inv-title{font-size:12px;font-weight:700;letter-spacing:1px}
.inv-title.forg{color:#58a6ff}
.inv-title.inst{color:#c084fc}
.inv-badge{font-size:11px;font-weight:700;padding:3px 10px;border-radius:20px}
.inv-badge.buy{background:rgba(0,255,136,.12);color:var(--green);border:1px solid rgba(0,255,136,.25)}
.inv-badge.sell{background:rgba(255,71,87,.12);color:var(--red);border:1px solid rgba(255,71,87,.25)}
.inv-badge.neutral{background:rgba(120,120,120,.1);color:var(--muted);border:1px solid var(--bd)}
.inv-body{padding:13px 15px}
.inv-row{display:flex;align-items:center;justify-content:space-between;padding:7px 0;border-bottom:1px solid rgba(255,255,255,.04)}
.inv-row:last-child{border-bottom:none}
.inv-lbl{font-size:11px;color:var(--muted)}
.inv-val{font-family:'JetBrains Mono';font-size:13px;font-weight:700}
.inv-val.pos{color:var(--green)}.inv-val.neg{color:var(--red)}.inv-val.neu{color:var(--muted)}
.inv-bar-section{margin-top:12px}
.inv-bar-lbl{font-size:10px;color:var(--muted);letter-spacing:1px;text-transform:uppercase;margin-bottom:6px}
.inv-mini-chart{display:flex;align-items:flex-end;gap:2px;height:40px;padding:4px 0}
.inv-bar{flex:1;border-radius:2px;min-height:2px;transition:height .3s}
.inv-bar.pos{background:rgba(0,255,136,.5)}
.inv-bar.neg{background:rgba(255,71,87,.5)}
.inv-no-data{padding:16px;color:var(--muted);font-size:12px;text-align:center}
/* ── 25% 되돌림 전략 ── */
.retr-hero{background:linear-gradient(135deg,rgba(251,146,60,.06),rgba(167,139,250,.06));border:1px solid rgba(251,146,60,.3);border-radius:14px;padding:20px 24px;margin-bottom:16px;display:flex;align-items:center;gap:16px;flex-wrap:wrap;animation:fr .3s ease}
.retr-title{font-family:'Bebas Neue';font-size:40px;background:linear-gradient(135deg,#fb923c,#a78bfa);-webkit-background-clip:text;-webkit-text-fill-color:transparent;letter-spacing:2px}
.retr-price{font-family:'JetBrains Mono';font-size:22px;margin-left:auto}
.retr-chg{font-family:'JetBrains Mono';font-size:13px;margin-left:6px}
.retr-diverge-card{background:var(--s2);border:1px solid var(--bd);border-radius:12px;padding:16px 20px;margin-bottom:14px;animation:fr .3s ease}
.retr-diverge-title{font-size:11px;color:var(--muted);letter-spacing:2px;text-transform:uppercase;margin-bottom:12px}
.retr-diverge-bar{height:12px;background:var(--bd);border-radius:6px;overflow:hidden;position:relative;margin-bottom:6px}
.retr-diverge-fill{height:100%;border-radius:6px;transition:width .8s}
.retr-diverge-label{display:flex;justify-content:space-between;font-size:11px;font-family:'JetBrains Mono';margin-bottom:3px}
.retr-signal{padding:14px 18px;border-radius:10px;margin-bottom:14px;display:flex;align-items:center;gap:14px;animation:fr .3s ease}
.retr-signal.strong-long{background:rgba(0,255,136,.08);border:2px solid rgba(0,255,136,.4)}
.retr-signal.long{background:rgba(0,255,136,.05);border:1px solid rgba(0,255,136,.25)}
.retr-signal.strong-short{background:rgba(255,71,87,.08);border:2px solid rgba(255,71,87,.4)}
.retr-signal.short{background:rgba(255,71,87,.05);border:1px solid rgba(255,71,87,.25)}
.retr-signal.neutral{background:rgba(255,165,2,.05);border:1px solid rgba(255,165,2,.2)}
.retr-signal-icon{font-size:36px;flex-shrink:0}
.retr-signal-title{font-family:'Bebas Neue';font-size:24px;letter-spacing:1px}
.retr-signal.strong-long .retr-signal-title,.retr-signal.long .retr-signal-title{color:var(--green)}
.retr-signal.strong-short .retr-signal-title,.retr-signal.short .retr-signal-title{color:var(--red)}
.retr-signal.neutral .retr-signal-title{color:var(--yellow)}
.retr-signal-sub{font-size:12px;color:var(--muted);margin-top:3px;line-height:1.6}
.retr-levels-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:14px}
.retr-level-card{background:var(--s2);border:1px solid var(--bd);border-radius:11px;padding:16px 18px;animation:fr .3s ease both}
.retr-level-card.strategy-a{border-left:3px solid var(--green)}
.retr-level-card.strategy-b{border-left:3px solid #a78bfa}
.retr-lc-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px}
.retr-lc-title{font-family:'Bebas Neue';font-size:18px;letter-spacing:1px}
.retr-level-card.strategy-a .retr-lc-title{color:var(--green)}
.retr-level-card.strategy-b .retr-lc-title{color:#a78bfa}
.retr-lc-badge{font-size:10px;font-weight:700;padding:2px 9px;border-radius:12px}
.retr-level-card.strategy-a .retr-lc-badge{background:rgba(0,255,136,.12);color:var(--green);border:1px solid rgba(0,255,136,.25)}
.retr-level-card.strategy-b .retr-lc-badge{background:rgba(167,139,250,.12);color:#a78bfa;border:1px solid rgba(167,139,250,.25)}
.retr-lv{display:flex;align-items:center;justify-content:space-between;padding:7px 0;border-bottom:1px solid rgba(255,255,255,.04);font-size:13px}
.retr-lv:last-child{border-bottom:none}
.retr-lv-lbl{font-size:11px;color:var(--muted)}
.retr-lv-val{font-family:'JetBrains Mono';font-weight:700}
.retr-lv-pct{font-size:10px;font-family:'JetBrains Mono';padding:1px 6px;border-radius:4px;margin-left:4px}
.retr-winrate{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:14px}
.retr-wr-box{background:var(--s2);border:1px solid var(--bd);border-radius:9px;padding:11px;text-align:center;animation:fr .3s ease both}
.retr-wr-pct{font-family:'Bebas Neue';font-size:28px;letter-spacing:1px}
.retr-wr-lbl{font-size:10px;color:var(--muted);margin-top:2px;letter-spacing:1px}
.retr-wr-fill{height:5px;border-radius:3px;margin-top:6px}
.retr-guide-box{background:rgba(251,146,60,.05);border:1px solid rgba(251,146,60,.2);border-radius:10px;padding:14px 18px;font-size:12px;color:var(--muted);line-height:1.9}
.retr-guide-box strong{color:var(--txt)}
.retr-guide-box .hl{color:#fb923c;font-weight:700}
.kr-hero{background:var(--s2);border:1px solid rgba(251,146,60,.3);border-radius:14px;padding:20px 24px;margin-bottom:16px;display:flex;align-items:center;gap:16px;flex-wrap:wrap;animation:fr .3s ease}
.kr-code{font-family:'Bebas Neue';font-size:44px;color:#fb923c;letter-spacing:2px}
.kr-name{font-size:15px;font-weight:700}.kr-sub{font-size:11px;color:var(--muted);margin-top:2px}
.kr-price{font-family:'JetBrains Mono';font-size:24px;margin-left:auto}
.kr-chg{font-family:'JetBrains Mono';font-size:13px;margin-left:6px}

/* MA status grid */
.ma-status-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:14px}
.ma-box{background:var(--s2);border:1px solid var(--bd);border-radius:10px;padding:13px 15px;text-align:center;animation:fr .3s ease both}
.ma-box.above{border-color:rgba(0,255,136,.35);background:rgba(0,255,136,.04)}
.ma-box.below{border-color:rgba(255,71,87,.35);background:rgba(255,71,87,.04)}
.ma-box.near{border-color:rgba(255,165,2,.3);background:rgba(255,165,2,.03)}
.ma-name{font-size:11px;color:var(--muted);letter-spacing:1px;margin-bottom:6px;font-weight:600}
.ma-val{font-family:'JetBrains Mono';font-size:14px;font-weight:700}
.ma-val.g{color:var(--green)}.ma-val.r{color:var(--red)}.ma-val.y{color:var(--yellow)}
.ma-diff{font-size:11px;font-family:'JetBrains Mono';margin-top:3px}
.ma-pos-badge{font-size:10px;font-weight:700;padding:2px 8px;border-radius:10px;margin-top:5px;display:inline-block}
.ma-pos-badge.above{background:rgba(0,255,136,.15);color:var(--green)}
.ma-pos-badge.below{background:rgba(255,71,87,.15);color:var(--red)}
.ma-pos-badge.near{background:rgba(255,165,2,.12);color:var(--yellow)}

/* Volume bar */
.vol-bar-wrap{background:var(--s2);border:1px solid var(--bd);border-radius:10px;padding:16px 18px;margin-bottom:14px}
.vol-bar-title{font-size:11px;color:var(--muted);letter-spacing:2px;text-transform:uppercase;margin-bottom:10px}
.vol-row{display:flex;align-items:center;gap:10px;margin-bottom:7px}
.vol-row-lbl{font-size:11px;color:var(--muted);min-width:70px;font-family:'JetBrains Mono'}
.vol-row-bar{flex:1;height:10px;background:var(--bd);border-radius:5px;overflow:hidden}
.vol-row-fill{height:100%;border-radius:5px;transition:width .6s}
.vol-row-val{font-family:'JetBrains Mono';font-size:12px;font-weight:600;min-width:90px;text-align:right}
.vol-signal{display:flex;align-items:center;gap:8px;margin-top:10px;padding:9px 12px;border-radius:8px;font-size:12px;font-weight:600}
.vol-signal.surge{background:rgba(251,146,60,.1);border:1px solid rgba(251,146,60,.3);color:#fb923c}
.vol-signal.normal{background:rgba(120,120,120,.08);border:1px solid var(--bd);color:var(--muted)}
.vol-signal.low{background:rgba(255,71,87,.07);border:1px solid rgba(255,71,87,.2);color:var(--red)}

/* Signal cards */
.kr-signal-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:14px}
@media(max-width:800px){.kr-signal-grid{grid-template-columns:1fr}}
.krs-card{background:var(--s2);border:1px solid var(--bd);border-radius:12px;overflow:hidden;animation:fr .3s ease both}
.krs-card.buy{border-left:4px solid var(--green)}
.krs-card.sell{border-left:4px solid var(--red)}
.krs-card.watch{border-left:4px solid var(--yellow)}
.krs-head{padding:12px 15px;background:var(--s3);border-bottom:1px solid var(--bd);display:flex;align-items:center;justify-content:space-between}
.krs-title{font-family:'Bebas Neue';font-size:20px;letter-spacing:1px}
.krs-card.buy .krs-title{color:var(--green)}
.krs-card.sell .krs-title{color:var(--red)}
.krs-card.watch .krs-title{color:var(--yellow)}
.krs-badge{font-size:11px;font-weight:700;padding:3px 10px;border-radius:20px}
.krs-card.buy .krs-badge{background:rgba(0,255,136,.15);color:var(--green);border:1px solid rgba(0,255,136,.3)}
.krs-card.sell .krs-badge{background:rgba(255,71,87,.15);color:var(--red);border:1px solid rgba(255,71,87,.3)}
.krs-card.watch .krs-badge{background:rgba(255,165,2,.12);color:var(--yellow);border:1px solid rgba(255,165,2,.25)}
.krs-body{padding:14px 16px}
.krs-levels{display:flex;flex-direction:column;gap:4px;margin-bottom:12px}
.krs-lv{display:flex;align-items:center;justify-content:space-between;padding:8px 11px;border-radius:7px;font-size:13px}
.krs-lv:nth-child(odd){background:rgba(255,255,255,.025)}
.krs-lv-lbl{color:var(--muted);font-size:11px;min-width:120px}
.krs-lv-val{font-family:'JetBrains Mono';font-weight:700;font-size:14px}
.krs-lv-pct{font-family:'JetBrains Mono';font-size:10px;padding:2px 7px;border-radius:4px;margin-left:5px}
.krs-entry-v{color:var(--yellow)}.krs-entry-p{background:rgba(255,165,2,.1);color:var(--yellow)}
.krs-sl-v{color:var(--red)}.krs-sl-p{background:rgba(255,71,87,.1);color:var(--red)}
.krs-tp1-v{color:#4ade80}.krs-tp1-p{background:rgba(74,222,128,.1);color:#4ade80}
.krs-tp2-v{color:var(--green)}.krs-tp2-p{background:rgba(0,255,136,.1);color:var(--green)}
.krs-tp3-v{color:#34d399}.krs-tp3-p{background:rgba(52,211,153,.08);color:#34d399}
.krs-cur-v{color:var(--blue)}
.krs-tags{display:flex;flex-wrap:wrap;gap:5px;margin-bottom:10px}
.krs-tag{font-size:10px;padding:3px 9px;border-radius:12px;font-weight:600}
.krs-tag.ok{background:rgba(0,255,136,.1);color:var(--green);border:1px solid rgba(0,255,136,.2)}
.krs-tag.warn{background:rgba(255,165,2,.1);color:var(--yellow);border:1px solid rgba(255,165,2,.2)}
.krs-tag.no{background:rgba(255,71,87,.08);color:var(--red);border:1px solid rgba(255,71,87,.15)}
.krs-reason{padding:10px 12px;background:var(--s3);border-radius:7px;border:1px solid var(--bd);font-size:11px;color:var(--muted);line-height:1.7}
.krs-reason strong{color:var(--txt)}
.krs-money{display:grid;grid-template-columns:1fr 1fr 1fr 1fr;gap:7px;margin-top:10px}
.krs-mbox{background:var(--s3);border:1px solid var(--bd);border-radius:7px;padding:9px 11px}
.krs-mlbl{font-size:9px;color:var(--muted);letter-spacing:1px;text-transform:uppercase;margin-bottom:3px}
.krs-mval{font-family:'JetBrains Mono';font-size:13px;font-weight:700}
.krs-mval.g{color:var(--green)}.krs-mval.r{color:var(--red)}.krs-mval.y{color:var(--yellow)}.krs-mval.b{color:var(--blue)}

/* MA align bar */
.ma-align-wrap{background:var(--s2);border:1px solid var(--bd);border-radius:10px;padding:13px 16px;margin-bottom:14px}
.ma-align-title{font-size:11px;color:var(--muted);letter-spacing:2px;text-transform:uppercase;margin-bottom:8px}
.ma-align-track{display:flex;gap:4px;align-items:center;flex-wrap:wrap}
.ma-align-pill{padding:5px 14px;border-radius:6px;font-size:12px;font-weight:700;font-family:'JetBrains Mono'}
.ma-align-pill.above{background:rgba(0,255,136,.12);color:var(--green);border:1px solid rgba(0,255,136,.25)}
.ma-align-pill.below{background:rgba(255,71,87,.12);color:var(--red);border:1px solid rgba(255,71,87,.25)}
.ma-align-pill.near{background:rgba(255,165,2,.1);color:var(--yellow);border:1px solid rgba(255,165,2,.2)}
.ma-align-arr{color:var(--muted);font-size:14px}
.ma-overall{margin-top:9px;padding:7px 12px;border-radius:7px;font-size:12px;font-weight:700;text-align:center}
.ma-overall.bull{background:rgba(0,255,136,.1);color:var(--green);border:1px solid rgba(0,255,136,.25)}
.ma-overall.bear{background:rgba(255,71,87,.1);color:var(--red);border:1px solid rgba(255,71,87,.25)}
.ma-overall.mixed{background:rgba(255,165,2,.08);color:var(--yellow);border:1px solid rgba(255,165,2,.2)}

/* ── EMA+FVG ── */
.ef-hero{background:var(--s2);border:1px solid rgba(52,211,153,.3);border-radius:14px;padding:22px 26px;margin-bottom:18px;display:flex;align-items:center;gap:18px;flex-wrap:wrap;animation:fr .3s ease}
.ef-code{font-family:'Bebas Neue';font-size:50px;color:#34d399;letter-spacing:2px}
.ef-name{font-size:17px;font-weight:700}.ef-sub{font-size:12px;color:var(--muted);margin-top:3px}
.ef-price{font-family:'JetBrains Mono';font-size:28px;margin-left:auto}
.ef-chg{font-family:'JetBrains Mono';font-size:15px;margin-left:8px}
/* EMA alignment bar */
.ema-align-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin-bottom:18px}
.ema-tf-card{background:var(--s2);border:1px solid var(--bd);border-radius:12px;padding:18px 20px;animation:fr .3s ease both}
.ema-tf-label{font-size:11px;color:var(--muted);letter-spacing:2px;text-transform:uppercase;margin-bottom:12px;font-weight:600}
.ema-stack{display:flex;flex-direction:column;gap:7px}
.ema-row{display:flex;align-items:center;justify-content:space-between;padding:8px 12px;border-radius:7px;font-size:13px}
.ema-row.above{background:rgba(0,255,136,.07);border:1px solid rgba(0,255,136,.2)}
.ema-row.below{background:rgba(255,71,87,.07);border:1px solid rgba(255,71,87,.2)}
.ema-row.near{background:rgba(255,165,2,.06);border:1px solid rgba(255,165,2,.15)}
.ema-name{font-family:'JetBrains Mono';font-size:12px;color:var(--muted);min-width:60px}
.ema-val{font-family:'JetBrains Mono';font-size:13px;font-weight:700}
.ema-val.g{color:var(--green)}.ema-val.r{color:var(--red)}.ema-val.y{color:var(--yellow)}
.ema-pos{font-size:11px;font-weight:700;padding:3px 10px;border-radius:10px}
.ema-pos.above{background:rgba(0,255,136,.15);color:var(--green)}
.ema-pos.below{background:rgba(255,71,87,.15);color:var(--red)}
.ema-pos.near{background:rgba(255,165,2,.12);color:var(--yellow)}
.ema-align-badge{margin-top:10px;text-align:center;font-size:13px;font-weight:700;padding:7px;border-radius:8px}
.ema-align-badge.full{background:rgba(52,211,153,.15);color:#34d399;border:1px solid rgba(52,211,153,.3)}
.ema-align-badge.partial{background:rgba(255,165,2,.12);color:var(--yellow);border:1px solid rgba(255,165,2,.25)}
.ema-align-badge.none{background:rgba(255,71,87,.1);color:var(--red);border:1px solid rgba(255,71,87,.2)}

/* FVG+EMA confluence */
.confluence-grid{display:grid;grid-template-columns:1fr;gap:16px;margin-bottom:14px}
@media(min-width:1100px){.confluence-grid{grid-template-columns:1fr 1fr}}
.cf-card{background:var(--s2);border:1px solid var(--bd);border-radius:14px;overflow:hidden;animation:fr .3s ease both;transition:border-color .2s}
.cf-card:hover{border-color:var(--bd2)}
.cf-card.high{border-left:4px solid #34d399}
.cf-card.mid{border-left:4px solid var(--yellow)}
.cf-card.low{border-left:4px solid var(--muted2)}
.cf-head{padding:16px 20px;background:var(--s3);border-bottom:1px solid var(--bd);display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px}
.cf-tf{font-family:'Bebas Neue';font-size:30px;letter-spacing:1px;color:#34d399}
.cf-quality{font-size:12px;font-weight:700;padding:4px 12px;border-radius:20px}
.cf-quality.high{background:rgba(52,211,153,.15);color:#34d399;border:1px solid rgba(52,211,153,.3)}
.cf-quality.mid{background:rgba(255,165,2,.12);color:var(--yellow);border:1px solid rgba(255,165,2,.25)}
.cf-quality.low{background:rgba(120,120,120,.1);color:var(--muted);border:1px solid var(--bd)}
.cf-body{padding:18px 20px}
.cf-section{margin-bottom:14px}
.cf-sec-lbl{font-size:10px;color:var(--muted);letter-spacing:2px;text-transform:uppercase;margin-bottom:8px}
.price-visual{position:relative;height:140px;background:var(--s3);border-radius:10px;overflow:hidden;margin-bottom:14px;border:1px solid var(--bd)}
.ef-levels{display:flex;flex-direction:column;gap:4px}
.ef-lv{display:flex;align-items:center;justify-content:space-between;padding:9px 13px;border-radius:7px;font-size:14px}
.ef-lv:nth-child(odd){background:rgba(255,255,255,.025)}
.ef-lv-name{color:var(--muted);font-size:12px;min-width:130px}
.ef-lv-val{font-family:'JetBrains Mono';font-weight:700;font-size:16px}
.ef-lv-pct{font-family:'JetBrains Mono';font-size:11px;padding:2px 8px;border-radius:5px;margin-left:6px}
.ef-entry-val{color:var(--yellow)}.ef-entry-pct{background:rgba(255,165,2,.12);color:var(--yellow)}
.ef-sl-val{color:var(--red)}.ef-sl-pct{background:rgba(255,71,87,.12);color:var(--red)}
.ef-tp1-val{color:#4ade80}.ef-tp1-pct{background:rgba(74,222,128,.12);color:#4ade80}
.ef-tp2-val{color:var(--green)}.ef-tp2-pct{background:rgba(0,255,136,.12);color:var(--green)}
.ef-tp3-val{color:#34d399}.ef-tp3-pct{background:rgba(52,211,153,.1);color:#34d399}
.ef-cur-val{color:var(--blue)}
.cf-score-row{display:flex;gap:7px;flex-wrap:wrap;margin-top:12px}
.cf-tag{font-size:11px;padding:4px 10px;border-radius:12px;font-weight:600}
.cf-tag.ok{background:rgba(52,211,153,.1);color:#34d399;border:1px solid rgba(52,211,153,.2)}
.cf-tag.warn{background:rgba(255,165,2,.1);color:var(--yellow);border:1px solid rgba(255,165,2,.2)}
.cf-tag.no{background:rgba(255,71,87,.08);color:var(--red);border:1px solid rgba(255,71,87,.15)}
.reason-box{margin-top:12px;padding:12px 14px;background:var(--s3);border-radius:8px;border:1px solid var(--bd);font-size:12px;color:var(--muted);line-height:1.8}
.reason-box strong{color:var(--txt)}
.ef-money{display:grid;grid-template-columns:1fr 1fr 1fr 1fr;gap:9px;margin-top:13px}
.ef-mbox{background:var(--s3);border:1px solid var(--bd);border-radius:8px;padding:12px 14px}
.ef-mlbl{font-size:10px;color:var(--muted);letter-spacing:1px;text-transform:uppercase;margin-bottom:4px}
.ef-mval{font-family:'JetBrains Mono';font-size:17px;font-weight:700}
.ef-mval.g{color:var(--green)}.ef-mval.r{color:var(--red)}.ef-mval.y{color:var(--yellow)}.ef-mval.b{color:var(--blue)}
.cf-head{padding:12px 15px;background:var(--s3);border-bottom:1px solid var(--bd);display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px}
.cf-tf{font-family:'Bebas Neue';font-size:22px;letter-spacing:1px;color:#34d399}
.cf-quality{font-size:11px;font-weight:700;padding:3px 10px;border-radius:20px}
.cf-quality.high{background:rgba(52,211,153,.15);color:#34d399;border:1px solid rgba(52,211,153,.3)}
.cf-quality.mid{background:rgba(255,165,2,.12);color:var(--yellow);border:1px solid rgba(255,165,2,.25)}
.cf-quality.low{background:rgba(120,120,120,.1);color:var(--muted);border:1px solid var(--bd)}
.cf-body{padding:13px 15px}
.cf-section{margin-bottom:11px}
.cf-sec-lbl{font-size:10px;color:var(--muted);letter-spacing:2px;text-transform:uppercase;margin-bottom:6px}

/* Price level visual */
.price-visual{position:relative;height:90px;background:var(--s3);border-radius:8px;overflow:hidden;margin-bottom:10px;border:1px solid var(--bd)}
.pv-zone{position:absolute;left:0;right:0;background:rgba(0,255,136,.12);border-top:1px solid rgba(0,255,136,.4);border-bottom:1px solid rgba(0,255,136,.4)}
.pv-ema{position:absolute;left:0;right:0;height:1px}
.pv-price{position:absolute;left:8px;right:8px;height:2px;background:var(--blue);border-radius:1px}
.pv-price::after{content:'◀ 현재가';position:absolute;right:-52px;top:-8px;font-size:9px;color:var(--blue);font-family:'JetBrains Mono';white-space:nowrap}
.pv-label{position:absolute;left:6px;font-size:9px;font-family:'JetBrains Mono'}

/* Entry detail rows */
.ef-levels{display:flex;flex-direction:column;gap:3px}
.ef-lv{display:flex;align-items:center;justify-content:space-between;padding:6px 10px;border-radius:6px;font-size:12px}
.ef-lv:nth-child(odd){background:rgba(255,255,255,.02)}
.ef-lv-name{color:var(--muted);font-size:11px;min-width:90px}
.ef-lv-val{font-family:'JetBrains Mono';font-weight:600;font-size:13px}
.ef-lv-pct{font-family:'JetBrains Mono';font-size:10px;padding:2px 6px;border-radius:4px;margin-left:5px}
.ef-entry-val{color:var(--yellow)}.ef-entry-pct{background:rgba(255,165,2,.1);color:var(--yellow)}
.ef-sl-val{color:var(--red)}.ef-sl-pct{background:rgba(255,71,87,.1);color:var(--red)}
.ef-tp1-val{color:#4ade80}.ef-tp1-pct{background:rgba(74,222,128,.1);color:#4ade80}
.ef-tp2-val{color:var(--green)}.ef-tp2-pct{background:rgba(0,255,136,.1);color:var(--green)}
.ef-tp3-val{color:#34d399}.ef-tp3-pct{background:rgba(52,211,153,.08);color:#34d399}
.ef-cur-val{color:var(--blue)}

/* Confluence score */
.cf-score-row{display:flex;gap:6px;flex-wrap:wrap;margin-top:9px}
.cf-tag{font-size:10px;padding:3px 8px;border-radius:12px;font-weight:600}
.cf-tag.ok{background:rgba(52,211,153,.1);color:#34d399;border:1px solid rgba(52,211,153,.2)}
.cf-tag.warn{background:rgba(255,165,2,.1);color:var(--yellow);border:1px solid rgba(255,165,2,.2)}
.cf-tag.no{background:rgba(255,71,87,.08);color:var(--red);border:1px solid rgba(255,71,87,.15)}

/* Reason box */
.reason-box{margin-top:9px;padding:9px 11px;background:var(--s3);border-radius:7px;border:1px solid var(--bd);font-size:11px;color:var(--muted);line-height:1.7}
.reason-box strong{color:var(--txt)}

/* Money row */
.ef-money{display:grid;grid-template-columns:1fr 1fr 1fr 1fr;gap:7px;margin-top:10px}
.ef-mbox{background:var(--s3);border:1px solid var(--bd);border-radius:7px;padding:9px 11px}
.ef-mlbl{font-size:9px;color:var(--muted);letter-spacing:1px;text-transform:uppercase;margin-bottom:3px}
.ef-mval{font-family:'JetBrains Mono';font-size:13px;font-weight:700}
.ef-mval.g{color:var(--green)}.ef-mval.r{color:var(--red)}.ef-mval.y{color:var(--yellow)}.ef-mval.b{color:var(--blue)}

/* ── BACKTEST ── */
.bt-summary{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:10px;margin-bottom:16px}
.bt-box{background:var(--s2);border:1px solid var(--bd);border-radius:10px;padding:13px 15px;animation:fr .3s ease both}
.bt-lbl{font-size:10px;color:var(--muted);letter-spacing:2px;text-transform:uppercase;margin-bottom:5px}
.bt-val{font-family:'Bebas Neue';font-size:26px;letter-spacing:1px}
.bt-val.g{color:var(--green)}.bt-val.r{color:var(--red)}.bt-val.y{color:var(--yellow)}
.bt-val.b{color:var(--blue)}.bt-val.p{color:#e879f9}.bt-val.w{color:var(--txt)}
.bt-sub{font-size:10px;color:var(--muted);margin-top:3px}
.equity-wrap{background:var(--s2);border:1px solid var(--bd);border-radius:10px;padding:16px 18px;margin-bottom:14px}
.equity-title{font-size:11px;color:var(--muted);letter-spacing:2px;text-transform:uppercase;margin-bottom:10px}
.equity-svg{width:100%;height:180px;border-radius:6px;background:var(--s3)}
.trade-list{background:var(--s2);border:1px solid var(--bd);border-radius:10px;overflow:hidden;margin-bottom:14px}
.tl-head{display:grid;grid-template-columns:60px 90px 1fr 1fr 1fr 1fr 70px 70px;padding:9px 14px;background:var(--s3);border-bottom:1px solid var(--bd);font-size:10px;color:var(--muted);letter-spacing:1px;text-transform:uppercase;gap:4px}
.tl-row{display:grid;grid-template-columns:60px 90px 1fr 1fr 1fr 1fr 70px 70px;padding:9px 14px;border-bottom:1px solid var(--bd);font-size:12px;gap:4px;animation:fr .2s ease both;transition:background .12s}
.tl-row:last-child{border-bottom:none}
.tl-row:hover{background:rgba(255,255,255,.02)}
.tl-row.win{border-left:3px solid var(--green)}
.tl-row.loss{border-left:3px solid var(--red)}
.tl-row.timeout{border-left:3px solid var(--muted2)}
.tl-num{font-family:'JetBrains Mono';font-size:11px;color:var(--muted2)}
.tl-dir{font-size:10px;font-weight:700;padding:2px 7px;border-radius:4px;width:fit-content}
.tl-long{background:rgba(0,255,136,.12);color:var(--green)}.tl-short{background:rgba(255,71,87,.12);color:var(--red)}
.tl-price{font-family:'JetBrains Mono';font-size:11px}
.tl-pnl{font-family:'JetBrains Mono';font-size:12px;font-weight:700}
.tl-pnl.pos{color:var(--green)}.tl-pnl.neg{color:var(--red)}
.tl-outcome{font-size:10px;font-weight:600;padding:2px 7px;border-radius:4px}
.tl-outcome.tp1{background:rgba(74,222,128,.12);color:#4ade80}
.tl-outcome.tp2{background:rgba(0,255,136,.12);color:var(--green)}
.tl-outcome.sl{background:rgba(255,71,87,.12);color:var(--red)}
.tl-outcome.to{background:rgba(120,120,120,.12);color:var(--muted)}
.tl-score{font-family:'JetBrains Mono';font-size:11px}
.bt-progress{background:var(--s2);border:1px solid var(--bd);border-radius:10px;padding:20px;text-align:center;margin-bottom:14px}
.bt-prog-bar{background:var(--bd);border-radius:4px;height:8px;overflow:hidden;margin:12px 0}
.bt-prog-fill{height:100%;background:linear-gradient(90deg,#e879f9,#f43f5e);border-radius:4px;transition:width .4s;animation:glow 1.5s infinite}
.bt-filter-row{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:12px;padding:10px 14px;background:var(--s3);border-radius:8px;border:1px solid var(--bd)}
.bt-filter-lbl{font-size:11px;color:var(--muted)}
.bt-filter-btn{padding:4px 12px;border-radius:5px;font-size:11px;font-weight:600;cursor:pointer;border:1px solid var(--bd);background:var(--s1);color:var(--muted);transition:all .2s}
.bt-filter-btn.active{border-color:#e879f9;color:#e879f9;background:rgba(232,121,249,.08)}

/* ── COMBO ANALYSIS ── */
.combo-hero{background:linear-gradient(135deg,rgba(244,63,94,.08),rgba(6,182,212,.08));border:1px solid rgba(244,63,94,.3);border-radius:14px;padding:20px 22px;margin-bottom:16px;display:flex;align-items:center;gap:16px;flex-wrap:wrap;animation:fr .3s ease}
.ch-icon{width:48px;height:48px;border-radius:12px;display:flex;align-items:center;justify-content:center;font-size:22px;font-weight:700;font-family:'JetBrains Mono';flex-shrink:0}
.ch-icon.btc{background:rgba(255,165,2,.2);color:var(--yellow)}
.ch-icon.eth{background:rgba(98,126,234,.2);color:#627eea}
.ch-icon.xrp{background:rgba(0,170,228,.2);color:#00aae4}
.ch-icon.stock{background:rgba(0,255,136,.2);color:var(--green)}
.ch-name{font-size:16px;font-weight:700}
.ch-sub{font-size:11px;color:var(--muted);margin-top:2px}
.ch-price{font-family:'JetBrains Mono';font-size:22px;margin-left:auto}
.ch-chg{font-family:'JetBrains Mono';font-size:13px;margin-left:8px}
.ch-score-wrap{margin-left:auto;text-align:center;min-width:90px}
.ch-score-num{font-family:'Bebas Neue';font-size:52px;line-height:1}
.ch-score-lbl{font-size:10px;color:var(--muted);letter-spacing:2px;text-transform:uppercase}

/* verdict bar */
.verdict{margin:14px 0;padding:14px 18px;border-radius:10px;display:flex;align-items:center;gap:14px;animation:fr .4s ease}
.verdict.strong-long{background:rgba(0,255,136,.1);border:2px solid rgba(0,255,136,.5)}
.verdict.long{background:rgba(0,255,136,.06);border:1px solid rgba(0,255,136,.3)}
.verdict.weak-long{background:rgba(0,255,136,.03);border:1px solid rgba(0,255,136,.15)}
.verdict.neutral{background:rgba(255,165,2,.06);border:1px solid rgba(255,165,2,.25)}
.verdict.weak-short{background:rgba(255,71,87,.03);border:1px solid rgba(255,71,87,.15)}
.verdict.short{background:rgba(255,71,87,.06);border:1px solid rgba(255,71,87,.3)}
.verdict.strong-short{background:rgba(255,71,87,.1);border:2px solid rgba(255,71,87,.5)}
.verdict-icon{font-size:36px;flex-shrink:0}
.verdict-txt{flex:1}
.verdict-title{font-family:'Bebas Neue';font-size:26px;letter-spacing:1px}
.verdict.strong-long .verdict-title,.verdict.long .verdict-title,.verdict.weak-long .verdict-title{color:var(--green)}
.verdict.strong-short .verdict-title,.verdict.short .verdict-title,.verdict.weak-short .verdict-title{color:var(--red)}
.verdict.neutral .verdict-title{color:var(--yellow)}
.verdict-sub{font-size:12px;color:var(--muted);margin-top:3px;line-height:1.6}
.verdict-side{font-family:'Bebas Neue';font-size:18px;padding:8px 16px;border-radius:8px;text-align:center;min-width:80px;flex-shrink:0}
.verdict.strong-long .verdict-side,.verdict.long .verdict-side{background:rgba(0,255,136,.2);color:var(--green)}
.verdict.strong-short .verdict-side,.verdict.short .verdict-side{background:rgba(255,71,87,.2);color:var(--red)}
.verdict.weak-long .verdict-side,.verdict.weak-short .verdict-side,.verdict.neutral .verdict-side{background:rgba(255,165,2,.15);color:var(--yellow)}

/* signal cards grid */
.signal-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:14px}
@media(max-width:700px){.signal-grid{grid-template-columns:1fr}}
.sig-card{background:var(--s2);border:1px solid var(--bd);border-radius:10px;padding:13px 14px;animation:fr .3s ease both}
.sig-card-title{font-size:10px;color:var(--muted);letter-spacing:2px;text-transform:uppercase;margin-bottom:8px;display:flex;align-items:center;gap:5px}
.sig-card-sig{font-family:'Bebas Neue';font-size:20px;letter-spacing:1px;margin-bottom:5px}
.sig-card-sig.bull{color:var(--green)}.sig-card-sig.bear{color:var(--red)}.sig-card-sig.neut{color:var(--yellow)}
.sig-card-detail{font-size:11px;color:var(--muted);line-height:1.6}
.sig-card-detail strong{color:var(--txt)}
.sig-score-dot{width:9px;height:9px;border-radius:50%;flex-shrink:0}
.sig-score-dot.bull{background:var(--green)}.sig-score-dot.bear{background:var(--red)}.sig-score-dot.neut{background:var(--yellow)}

/* entry proposal */
.entry-box{background:var(--s2);border:1px solid var(--bd);border-radius:12px;overflow:hidden;margin-bottom:12px}
.eb-head{padding:13px 16px;border-bottom:1px solid var(--bd);display:flex;align-items:center;gap:10px;background:var(--s3)}
.eb-tf{font-family:'Bebas Neue';font-size:24px;letter-spacing:1px;color:#06b6d4}
.eb-label{font-size:12px;color:var(--muted)}
.eb-dir{font-size:11px;font-weight:700;padding:3px 10px;border-radius:20px}
.eb-dir.long{background:rgba(0,255,136,.15);color:var(--green);border:1px solid rgba(0,255,136,.3)}
.eb-dir.short{background:rgba(255,71,87,.15);color:var(--red);border:1px solid rgba(255,71,87,.3)}
.eb-dir.wait{background:rgba(255,165,2,.12);color:var(--yellow);border:1px solid rgba(255,165,2,.25)}
.eb-body{padding:16px 18px;display:grid;grid-template-columns:1fr 1fr;gap:10px}
.ep-item{background:var(--s3);border:1px solid var(--bd);border-radius:8px;padding:10px 12px}
.ep-lbl{font-size:10px;color:var(--muted);letter-spacing:1px;text-transform:uppercase;margin-bottom:4px}
.ep-val{font-family:'JetBrains Mono';font-size:14px;font-weight:700}
.ep-val.entry{color:var(--yellow)}.ep-val.sl{color:var(--red)}
.ep-val.tp1{color:#4ade80}.ep-val.tp2{color:var(--green)}.ep-val.tp3{color:#a7f3d0}
.ep-val.cur{color:var(--blue)}
.ep-pct{font-size:10px;font-family:'JetBrains Mono';margin-top:2px}
.ep-pct.pos{color:var(--green)}.ep-pct.neg{color:var(--red)}
.conf-bar{margin-top:12px;padding:10px 14px;background:var(--s3);border-radius:8px;border:1px solid var(--bd)}
.cb-label{font-size:10px;color:var(--muted);letter-spacing:2px;text-transform:uppercase;margin-bottom:6px;display:flex;justify-content:space-between}
.cb-track{background:var(--bd);border-radius:4px;height:8px;overflow:hidden}
.cb-fill{height:100%;border-radius:4px;transition:width .8s}
.cond-list{margin-top:10px;display:flex;flex-direction:column;gap:4px}
.cl-item{display:flex;align-items:center;gap:8px;font-size:11px;padding:5px 8px;border-radius:5px;background:rgba(255,255,255,.02)}
.cl-ok{color:var(--green)}.cl-warn{color:var(--yellow)}.cl-no{color:var(--red)}

/* score circle */
.score-circle{width:64px;height:64px;border-radius:50%;display:flex;flex-direction:column;align-items:center;justify-content:center;flex-shrink:0}
.sc-num{font-family:'Bebas Neue';font-size:28px;line-height:1}
.sc-max{font-size:9px;color:rgba(255,255,255,.5);letter-spacing:1px}

/* ── VWAP ── */
.vwap-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(310px,1fr));gap:14px;margin-top:14px}
.vcard{background:var(--s2);border:1px solid var(--bd);border-radius:12px;overflow:hidden;animation:fr .3s ease both}
.vc-head{padding:16px 18px;border-bottom:1px solid var(--bd);display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.vc-icon{width:38px;height:38px;border-radius:9px;display:flex;align-items:center;justify-content:center;font-size:16px;font-weight:700;font-family:'JetBrains Mono'}
.vc-icon.btc{background:rgba(255,165,2,.15);color:var(--yellow)}
.vc-icon.eth{background:rgba(98,126,234,.15);color:#627eea}
.vc-icon.xrp{background:rgba(0,170,228,.15);color:#00aae4}
.vc-icon.stock{background:rgba(0,255,136,.15);color:var(--green)}
.vc-name{font-size:13px;font-weight:600}.vc-sub{font-size:10px;color:var(--muted)}
.vc-price-wrap{margin-left:auto;text-align:right}
.vc-price{font-family:'JetBrains Mono';font-size:15px}
.vc-chg{font-family:'JetBrains Mono';font-size:11px}
.vc-body{padding:12px 15px;display:flex;flex-direction:column;gap:8px}
.vwap-row{background:var(--s3);border:1px solid var(--bd);border-radius:9px;padding:11px 13px;transition:border-color .2s}
.vwap-row:hover{border-color:var(--bd2)}
.vr-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:8px}
.vr-tf{font-family:'Bebas Neue';font-size:21px;letter-spacing:1px;color:var(--muted)}
.vr-signal{font-size:11px;font-weight:700;padding:3px 10px;border-radius:20px;letter-spacing:.5px}
.sig-above{background:rgba(0,255,136,.12);color:var(--green);border:1px solid rgba(0,255,136,.3)}
.sig-below{background:rgba(255,71,87,.12);color:var(--red);border:1px solid rgba(255,71,87,.3)}
.sig-near{background:rgba(255,165,2,.12);color:var(--yellow);border:1px solid rgba(255,165,2,.3)}
.vr-bars{display:flex;flex-direction:column;gap:5px}
.vr-bar-row{display:flex;align-items:center;gap:8px;font-size:11px}
.vr-bar-label{color:var(--muted);min-width:70px;font-family:'JetBrains Mono';font-size:10px}
.vr-bar-val{font-family:'JetBrains Mono';font-weight:600;min-width:90px;font-size:12px}
.vr-bar-track{flex:1;height:6px;background:var(--bd);border-radius:3px;overflow:hidden;position:relative}
.vr-bar-fill{height:100%;border-radius:3px;transition:width .6s}
.vr-bar-marker{position:absolute;top:0;height:100%;width:2px;background:var(--yellow)}
.vr-meta{display:flex;gap:8px;margin-top:8px;flex-wrap:wrap}
.vm-box{background:var(--s1);border:1px solid var(--bd);border-radius:6px;padding:7px 10px;flex:1;min-width:80px}
.vm-lbl{font-size:9px;color:var(--muted);letter-spacing:1px;text-transform:uppercase;margin-bottom:3px}
.vm-val{font-family:'JetBrains Mono';font-size:12px;font-weight:600}
.vm-val.g{color:var(--green)}.vm-val.r{color:var(--red)}.vm-val.y{color:var(--yellow)}.vm-val.c{color:#06b6d4}
.vwap-summary{display:flex;gap:0;border:1px solid var(--bd);border-radius:8px;overflow:hidden;background:var(--s3);margin-bottom:12px}
.vs-item{flex:1;padding:11px 8px;text-align:center;border-right:1px solid var(--bd)}
.vs-item:last-child{border-right:none}
.vs-tf{font-size:9px;color:var(--muted);letter-spacing:2px;text-transform:uppercase;margin-bottom:5px}
.vs-sig{font-size:18px;margin-bottom:3px}
.vs-txt{font-size:10px;font-weight:700}
.vs-txt.above{color:var(--green)}.vs-txt.below{color:var(--red)}.vs-txt.near{color:var(--yellow)}

/* ── TREND ── */
.trend-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:14px;margin-top:16px}
.asset-block{background:var(--s2);border:1px solid var(--bd);border-radius:12px;overflow:hidden;animation:fr .3s ease both}
.ab-head{padding:16px 18px;display:flex;align-items:center;gap:12px;border-bottom:1px solid var(--bd);flex-wrap:wrap}
.ab-icon{font-size:18px;width:38px;height:38px;border-radius:9px;display:flex;align-items:center;justify-content:center;font-weight:700;font-family:'JetBrains Mono'}
.ab-icon.btc{background:rgba(255,165,2,.15);color:var(--yellow)}
.ab-icon.eth{background:rgba(98,126,234,.15);color:#627eea}
.ab-icon.xrp{background:rgba(0,170,228,.15);color:#00aae4}
.ab-icon.stock{background:rgba(0,255,136,.15);color:var(--green)}
.ab-name{font-size:13px;font-weight:600}.ab-ticker{font-size:10px;color:var(--muted)}
.ab-price{font-family:'JetBrains Mono';font-size:14px;margin-left:auto}
.ab-chg{font-family:'JetBrains Mono';font-size:11px;margin-left:5px}
.tf-rows{padding:10px 14px;display:flex;flex-direction:column;gap:7px}
.tf-row{display:flex;align-items:center;gap:10px;padding:9px 11px;border-radius:8px;background:var(--s3);border:1px solid var(--bd);transition:all .2s}
.tf-row.up-strong{border-color:rgba(0,255,136,.55);background:rgba(0,255,136,.07)}
.tf-row.up{border-color:rgba(0,255,136,.28);background:rgba(0,255,136,.03)}
.tf-row.up-weak{border-color:rgba(0,255,136,.14)}
.tf-row.down-strong{border-color:rgba(255,71,87,.55);background:rgba(255,71,87,.07)}
.tf-row.down{border-color:rgba(255,71,87,.28);background:rgba(255,71,87,.03)}
.tf-row.down-weak{border-color:rgba(255,71,87,.14)}
.tf-row.side{border-color:rgba(255,165,2,.22)}
.tf-lbl{font-family:'Bebas Neue';font-size:22px;letter-spacing:1px;min-width:48px}
.tf-arrow{font-size:20px;min-width:26px;text-align:center}
.tf-trend-txt{font-size:12px;font-weight:700;min-width:76px}
.tf-row.up-strong .tf-trend-txt,.tf-row.up .tf-trend-txt,.tf-row.up-weak .tf-trend-txt{color:var(--green)}
.tf-row.down-strong .tf-trend-txt,.tf-row.down .tf-trend-txt,.tf-row.down-weak .tf-trend-txt{color:var(--red)}
.tf-row.side .tf-trend-txt{color:var(--yellow)}
.tf-meta{flex:1;font-size:10px;color:var(--muted);line-height:1.5}
.tf-emas{font-family:'JetBrains Mono';font-size:9px;color:var(--muted2);margin-top:2px}
.tf-rsi-wrap{display:flex;flex-direction:column;align-items:flex-end;gap:3px;min-width:52px}
.tf-rsi-val{font-family:'JetBrains Mono';font-size:11px;font-weight:600}
.tf-rsi-bar{width:48px;height:4px;background:var(--bd);border-radius:3px;overflow:hidden}
.tf-rsi-fill{height:100%;border-radius:3px;transition:width .6s}
.strength-dots{display:flex;gap:2px;align-items:center;margin:0 6px}
.sdot{width:7px;height:7px;border-radius:2px;background:var(--bd)}
.sdot.aup{background:var(--green)}.sdot.adn{background:var(--red)}
.trend-top{display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end;margin-bottom:0}
.trend-summary-bar{display:flex;gap:0;border:1px solid var(--bd);border-radius:8px;overflow:hidden;margin:12px 0;background:var(--s3)}
.tsb-item{flex:1;padding:12px 8px;text-align:center;border-right:1px solid var(--bd)}
.tsb-item:last-child{border-right:none}
.tsb-tf{font-size:10px;color:var(--muted);letter-spacing:2px;text-transform:uppercase;margin-bottom:6px}
.tsb-arrow{font-size:26px;line-height:1}
.tsb-txt{font-size:11px;font-weight:700;margin-top:4px}
.tsb-txt.up{color:var(--green)}.tsb-txt.down{color:var(--red)}.tsb-txt.side{color:var(--yellow)}

.panel{background:var(--s2);border:1px solid var(--bd);border-top:none;border-radius:0 0 8px 8px;padding:18px 22px;margin-bottom:16px}
.pr{display:flex;gap:12px;flex-wrap:wrap;align-items:flex-end}
.ctrl{display:flex;flex-direction:column;gap:5px}
.ctrl label{font-size:10px;color:var(--muted);letter-spacing:2px;text-transform:uppercase;font-weight:500}
.ctrl select,.ctrl input{background:var(--s1);border:1px solid var(--bd2);color:var(--txt);padding:8px 11px;border-radius:6px;font-family:'Noto Sans KR';font-size:13px;outline:none;transition:border-color .2s}
.ctrl select:focus,.ctrl input:focus{border-color:var(--green)}
.ctrl input[type=number]{width:130px}.ctrl input[type=text]{width:150px}

/* ── ASSET TOGGLE ── */
.at-wrap{display:flex;flex-direction:column;gap:5px}
.at-label{font-size:10px;color:var(--muted);letter-spacing:2px;text-transform:uppercase;font-weight:500}
.at-btns{display:flex;gap:0;background:var(--s1);border:1px solid var(--bd2);border-radius:6px;padding:3px;width:fit-content}
.at-btn{padding:6px 20px;border-radius:4px;font-size:12px;font-weight:700;cursor:pointer;border:none;color:var(--muted);background:transparent;font-family:'Noto Sans KR';transition:all .2s;white-space:nowrap}
.at-btn.on-stock{background:var(--s2);color:var(--green);border:1px solid rgba(0,255,136,.3)}
.at-btn.on-crypto{background:var(--s2);color:var(--yellow);border:1px solid rgba(255,165,2,.3)}
.at-btn.on-blue{background:var(--s2);color:#06b6d4;border:1px solid rgba(6,182,212,.3)}
.at-btn.on-purple{background:var(--s2);color:#a78bfa;border:1px solid rgba(167,139,250,.3)}

/* ── COIN BTNS ── */
.coin-row{display:flex;gap:6px;align-items:center;flex-wrap:wrap}
.cb{padding:7px 18px;border-radius:6px;font-size:13px;font-weight:700;cursor:pointer;border:1px solid var(--bd2);background:var(--s1);color:var(--muted);transition:all .2s;font-family:'JetBrains Mono'}
.cb:hover{border-color:var(--yellow);color:var(--yellow)}
.cb.sel-btc{background:var(--yellow);border-color:var(--yellow);color:#000}
.cb.sel-eth{background:var(--eth);border-color:var(--eth);color:#fff}
.cb.sel-xrp{background:var(--xrp);border-color:var(--xrp);color:#fff}

.btn{padding:9px 24px;border-radius:6px;font-family:'Noto Sans KR';font-size:13px;font-weight:700;border:none;cursor:pointer;transition:all .2s}
.btn-g{background:var(--green);color:#000}.btn-g:hover{background:#00dd77;transform:translateY(-1px)}
.btn-b{background:var(--blue);color:#000}.btn-b:hover{background:#4090ee;transform:translateY(-1px)}
.btn-o{background:var(--orange);color:#fff}.btn-o:hover{background:#e55a25;transform:translateY(-1px)}
.btn:disabled{background:var(--bd);color:var(--muted);cursor:not-allowed;transform:none!important}
.hint{font-size:11px;color:var(--muted);margin-top:7px}

.prog-wrap{margin:10px 0;display:none}
.prog-bg{background:var(--bd);border-radius:3px;height:3px;overflow:hidden;margin-bottom:6px}
.prog-fill{height:100%;background:linear-gradient(90deg,var(--green),var(--blue));width:0%;transition:width .4s;animation:glow 1.8s ease-in-out infinite}
@keyframes glow{0%,100%{opacity:1}50%{opacity:.4}}
.prog-txt{font-size:11px;color:var(--muted);font-family:'JetBrains Mono'}

.rh{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}
.rc{font-family:'Bebas Neue';font-size:28px;color:var(--green)}
.rc span{font-size:12px;color:var(--muted);font-family:'Noto Sans KR';font-weight:300;margin-left:4px}
.btn-csv{padding:6px 14px;border-radius:6px;background:transparent;color:var(--green);font-size:11px;border:1px solid var(--green);cursor:pointer;transition:all .2s;font-weight:600}
.btn-csv:hover{background:var(--green);color:#000}
.tw{overflow-x:auto;border-radius:8px;border:1px solid var(--bd)}
table{width:100%;border-collapse:collapse;font-size:13px}
thead tr{background:var(--s1)}
thead th{padding:10px 13px;text-align:left;font-size:10px;color:var(--muted);letter-spacing:2px;text-transform:uppercase;cursor:pointer;white-space:nowrap;user-select:none}
thead th:hover{color:var(--txt)}
thead th.desc::after{content:' ↓';color:var(--green)}thead th.asc::after{content:' ↑';color:var(--green)}
tbody tr{border-top:1px solid var(--bd);transition:background .12s;animation:fr .2s ease both}
@keyframes fr{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}
tbody tr:hover{background:rgba(0,255,136,.03)}
td{padding:10px 13px;white-space:nowrap}
.r{color:var(--muted2);font-family:'JetBrains Mono';font-size:11px}
.c{font-family:'JetBrains Mono';font-size:11px;color:var(--muted)}
.badge{display:inline-block;padding:2px 8px;border-radius:4px;font-size:10px;font-weight:700}
.bkp{background:rgba(0,255,136,.1);color:var(--green)}.bkq{background:rgba(255,71,87,.1);color:var(--red)}
.pct{font-family:'JetBrains Mono';font-weight:700}
.up{color:var(--green)}.mid{color:var(--yellow)}.dn{color:var(--txt)}
.pr2{font-family:'JetBrains Mono'}.sub{font-family:'JetBrains Mono';font-size:11px;color:var(--muted)}
.empty{text-align:center;padding:50px;color:var(--muted)}
.spin{display:inline-block;width:22px;height:22px;border:2px solid var(--bd);border-top-color:var(--orange);border-radius:50%;animation:sp .8s linear infinite;margin-bottom:10px}
@keyframes sp{to{transform:rotate(360deg)}}

/* FVG cards */
.fvg-hdr{display:flex;align-items:center;gap:14px;margin-bottom:14px;padding:13px 16px;background:var(--s3);border:1px solid var(--bd);border-radius:8px;flex-wrap:wrap}
.fticker{font-family:'Bebas Neue';font-size:28px;color:var(--green);letter-spacing:2px}
.fname{font-size:13px;font-weight:500}.fsub{font-size:11px;color:var(--muted);margin-top:2px}
.fprice{font-family:'JetBrains Mono';font-size:17px;margin-left:auto}
.legend{display:flex;gap:14px;margin-bottom:10px;font-size:11px;color:var(--muted);flex-wrap:wrap}
.legend span{display:flex;align-items:center;gap:5px}
.dot{width:8px;height:8px;border-radius:50%}
.db{background:var(--green)}.dr{background:var(--red)}.dn2{background:var(--muted2)}
.tf-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(148px,1fr));gap:9px}
.tfc{background:var(--s3);border:1px solid var(--bd);border-radius:10px;padding:14px;transition:all .25s;animation:fr .3s ease both}
.tfc:hover{transform:translateY(-2px);border-color:var(--bd2)}
.tfc.bull{border-color:rgba(0,255,136,.45);background:rgba(0,255,136,.04)}
.tfc.bear{border-color:rgba(255,71,87,.45);background:rgba(255,71,87,.04)}
.tfc.none{opacity:.5}
.tfl{font-size:10px;color:var(--muted);letter-spacing:2px;text-transform:uppercase;margin-bottom:6px}
.tfs{font-family:'Bebas Neue';font-size:19px;letter-spacing:1px}
.tfs.bull{color:var(--green)}.tfs.bear{color:var(--red)}.tfs.none{color:var(--muted)}
.tfd{margin-top:6px;font-size:11px;font-family:'JetBrains Mono';color:var(--muted);line-height:1.7}
.tfd .gh{color:var(--green);font-size:10px}.tfd .gl{color:var(--red);font-size:10px}.tfd .gc{color:var(--yellow)}

/* coin badge */
.cbadge{display:inline-block;padding:4px 12px;border-radius:6px;font-size:14px;font-weight:700;font-family:'JetBrains Mono'}
.cb-btc{background:rgba(255,165,2,.15);color:var(--yellow);border:1px solid rgba(255,165,2,.3)}
.cb-eth{background:rgba(98,126,234,.15);color:var(--eth);border:1px solid rgba(98,126,234,.3)}
.cb-xrp{background:rgba(0,170,228,.15);color:var(--xrp);border:1px solid rgba(0,170,228,.3)}

/* setup cards */
.hero{display:flex;align-items:center;gap:16px;margin-bottom:14px;padding:14px 18px;background:var(--s3);border:1px solid var(--bd);border-radius:10px;flex-wrap:wrap}
.hcode{font-family:'Bebas Neue';font-size:34px;color:var(--orange);letter-spacing:2px}
.hname{font-size:14px;font-weight:600}.hmkt{font-size:11px;color:var(--muted);margin-top:2px}
.hprice{font-family:'JetBrains Mono';font-size:20px;margin-left:auto}
.hchg{font-family:'JetBrains Mono';font-size:12px;margin-left:8px}
.sum-bar{display:grid;grid-template-columns:repeat(auto-fill,minmax(155px,1fr));gap:9px;margin-bottom:14px}
.sbox{background:var(--s3);border:1px solid var(--bd);border-radius:8px;padding:11px 13px}
.slbl{font-size:10px;color:var(--muted);letter-spacing:2px;text-transform:uppercase;margin-bottom:4px}
.sval{font-family:'Bebas Neue';font-size:22px;letter-spacing:1px}
.sval.g{color:var(--green)}.sval.r{color:var(--red)}.sval.y{color:var(--yellow)}.sval.b{color:var(--blue)}.sval.o{color:var(--orange)}
.mtf-bar{display:flex;gap:5px;margin-bottom:14px;flex-wrap:wrap;align-items:center}
.mtf-lbl{font-size:10px;color:var(--muted);letter-spacing:2px;text-transform:uppercase;margin-right:4px}
.mpill{padding:4px 11px;border-radius:20px;font-size:11px;font-family:'JetBrains Mono';font-weight:600}
.mpill.bull{background:rgba(0,255,136,.12);color:var(--green);border:1px solid rgba(0,255,136,.25)}
.mpill.bear{background:rgba(255,71,87,.12);color:var(--red);border:1px solid rgba(255,71,87,.25)}
.mpill.none{background:rgba(255,255,255,.04);color:var(--muted2);border:1px solid var(--bd)}
.sg{display:grid;grid-template-columns:repeat(auto-fill,minmax(335px,1fr));gap:13px}
.sc{background:var(--s2);border:1px solid var(--bd);border-radius:12px;overflow:hidden;animation:fr .3s ease both;transition:border-color .2s}
.sc:hover{border-color:var(--bd2)}
.sc.bul{border-left:3px solid var(--green)}.sc.ber{border-left:3px solid var(--red)}.sc.non{border-left:3px solid var(--muted2);opacity:.55}
.sch{padding:13px 15px 9px;display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid var(--bd)}
.sctf{font-family:'Bebas Neue';font-size:21px;letter-spacing:1px}
.bul .sctf{color:var(--green)}.ber .sctf{color:var(--red)}.non .sctf{color:var(--muted)}
.scdir{font-size:10px;font-weight:700;padding:3px 9px;border-radius:20px;letter-spacing:.5px}
.bul .scdir{background:rgba(0,255,136,.12);color:var(--green)}.ber .scdir{background:rgba(255,71,87,.12);color:var(--red)}.non .scdir{background:var(--bd);color:var(--muted)}
.scst{font-size:10px;color:var(--muted);margin-top:2px}.scst.act{color:var(--yellow);font-weight:600}
.scb{padding:13px 15px}
.pl{display:flex;flex-direction:column;gap:1px;margin-bottom:12px}
.plr{display:flex;justify-content:space-between;align-items:center;padding:6px 9px;border-radius:5px;font-size:12px}
.plr:nth-child(odd){background:rgba(255,255,255,.02)}
.pll{color:var(--muted);font-size:11px;min-width:80px}
.plv{font-family:'JetBrains Mono';font-weight:600;font-size:13px}
.plp{font-family:'JetBrains Mono';font-size:10px;padding:2px 6px;border-radius:4px;margin-left:5px}
.pe .plv{color:var(--yellow)}.pe .plp{background:rgba(255,165,2,.1);color:var(--yellow)}
.ps .plv{color:var(--red)}.ps .plp{background:rgba(255,71,87,.1);color:var(--red)}
.p1 .plv{color:#4ade80}.p1 .plp{background:rgba(74,222,128,.1);color:#4ade80}
.p2 .plv{color:var(--green)}.p2 .plp{background:rgba(0,255,136,.1);color:var(--green)}
.p3 .plv{color:#a7f3d0}.p3 .plp{background:rgba(167,243,208,.08);color:#a7f3d0}
.pcur .plv{color:var(--blue)}
.rrbar{margin-top:10px}
.rrlbl{font-size:10px;color:var(--muted);letter-spacing:2px;text-transform:uppercase;margin-bottom:4px;display:flex;justify-content:space-between}
.rrtrack{background:var(--bd);border-radius:3px;height:7px;display:flex;overflow:hidden}
.rrl{background:var(--red);height:100%}.rrp{background:var(--green);height:100%}
.mrow{display:grid;grid-template-columns:1fr 1fr;gap:7px;margin-top:10px}
.mbox{background:var(--s1);border:1px solid var(--bd);border-radius:7px;padding:9px 11px}
.mlbl{font-size:10px;color:var(--muted);letter-spacing:1px;text-transform:uppercase;margin-bottom:3px}
.mval{font-family:'JetBrains Mono';font-size:13px;font-weight:700}
.mval.g{color:var(--green)}.mval.r{color:var(--red)}.mval.y{color:var(--yellow)}.mval.b{color:var(--blue)}
.msub{font-size:10px;color:var(--muted);margin-top:2px}
.conds{display:flex;flex-wrap:wrap;gap:4px;margin-top:10px}
.cond{font-size:10px;padding:3px 8px;border-radius:12px;font-weight:600}
.cok{background:rgba(0,255,136,.1);color:var(--green);border:1px solid rgba(0,255,136,.2)}
.cwrn{background:rgba(255,165,2,.1);color:var(--yellow);border:1px solid rgba(255,165,2,.2)}
.cno{background:rgba(255,71,87,.1);color:var(--red);border:1px solid rgba(255,71,87,.2)}
.warn-box{margin-top:14px;padding:11px 14px;background:rgba(255,165,2,.06);border:1px solid rgba(255,165,2,.2);border-radius:8px;font-size:11px;color:var(--muted);line-height:1.8}

/* guide */
.gw{display:grid;grid-template-columns:1fr 1fr;gap:13px}
@media(max-width:860px){.gw{grid-template-columns:1fr}}
.gc2{background:var(--s2);border:1px solid var(--bd);border-radius:10px;padding:19px;animation:fr .3s ease both}
.gc2.full{grid-column:1/-1}
.gct{font-family:'Bebas Neue';font-size:18px;letter-spacing:1px;margin-bottom:11px;display:flex;align-items:center;gap:7px}
.cbull .gct{color:var(--green)}.cbear .gct{color:var(--red)}.crule .gct{color:var(--yellow)}.cmtf .gct{color:var(--blue)}.crisk .gct{color:var(--purple)}.cflow .gct{color:var(--green)}
.cdemo{width:100%;height:105px;margin:7px 0;background:var(--s1);border-radius:7px;border:1px solid var(--bd)}
.steps{display:flex;flex-direction:column;gap:8px;margin-top:3px}
.step{display:flex;gap:9px;align-items:flex-start}
.sn{min-width:21px;height:21px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:700;font-family:'JetBrains Mono';flex-shrink:0}
.sg2{background:rgba(0,255,136,.15);color:var(--green);border:1px solid rgba(0,255,136,.3)}
.sr{background:rgba(255,71,87,.15);color:var(--red);border:1px solid rgba(255,71,87,.3)}
.sb{background:rgba(88,166,255,.15);color:var(--blue);border:1px solid rgba(88,166,255,.3)}
.sy{background:rgba(255,165,2,.15);color:var(--yellow);border:1px solid rgba(255,165,2,.3)}
.st{font-size:12px;font-weight:600;margin-bottom:2px}.sd{font-size:11px;color:var(--muted);line-height:1.6}
.sd strong{color:var(--txt)}
.tags{display:flex;flex-wrap:wrap;gap:4px;margin-top:7px}
.tg{padding:3px 8px;border-radius:20px;font-size:10px;font-weight:600}
.tgg{background:rgba(0,255,136,.1);color:var(--green);border:1px solid rgba(0,255,136,.2)}
.tgr{background:rgba(255,71,87,.1);color:var(--red);border:1px solid rgba(255,71,87,.2)}
.tgb{background:rgba(88,166,255,.1);color:var(--blue);border:1px solid rgba(88,166,255,.2)}
.tgy{background:rgba(255,165,2,.1);color:var(--yellow);border:1px solid rgba(255,165,2,.2)}
.mt{width:100%;border-collapse:collapse;font-size:12px;margin-top:7px}
.mt th{padding:6px 9px;background:var(--s1);color:var(--muted);font-size:10px;letter-spacing:1px;text-transform:uppercase;text-align:left;border:1px solid var(--bd)}
.mt td{padding:7px 9px;border:1px solid var(--bd);vertical-align:top;line-height:1.5}
.mt tr:nth-child(even){background:rgba(255,255,255,.02)}
.mt .tf{font-family:'JetBrains Mono';font-size:11px;color:var(--yellow)}
.mt .role{color:var(--blue);font-weight:600}
.rg{display:grid;grid-template-columns:1fr 1fr;gap:7px;margin-top:7px}
.ri{background:var(--s1);border:1px solid var(--bd);border-radius:7px;padding:9px}
.rl{font-size:10px;color:var(--muted);letter-spacing:2px;text-transform:uppercase;margin-bottom:3px}
.rv{font-family:'JetBrains Mono';font-size:13px;font-weight:600}
.rv.g{color:var(--green)}.rv.r{color:var(--red)}.rv.y{color:var(--yellow)}.rv.b{color:var(--blue)}
.rd{font-size:10px;color:var(--muted);margin-top:3px;line-height:1.5}
.wb{background:rgba(255,165,2,.07);border:1px solid rgba(255,165,2,.25);border-radius:7px;padding:11px 13px;font-size:11px;color:var(--muted);line-height:1.7;margin-top:7px}
.wb strong{color:var(--yellow)}
.flow{display:flex;flex-wrap:wrap;gap:0;margin-top:9px;align-items:center}
.fs2{background:var(--s1);border:1px solid var(--bd);border-radius:7px;padding:8px 11px;font-size:10px;text-align:center;min-width:85px}
.fsn{font-family:'JetBrains Mono';font-size:9px;color:var(--muted);margin-bottom:2px}
.fst{font-weight:600;color:var(--txt)}.fss{font-size:9px;color:var(--muted);margin-top:1px}
.fa{color:var(--muted);font-size:15px;padding:0 3px}
</style>
</head>
<body>
<div class="wrap">

<header>
  <div>
    <div class="logo"><span class="kr">KR</span><span class="sc">SCAN</span></div>
    <div style="font-size:10px;color:var(--muted);letter-spacing:2px;margin-top:2px">KOREAN STOCK SCREENER v4</div>
  </div>
  <div class="hright">
    급등 · 전고점 · FVG · 매매법 · 타점제안<br>
    <span class="now" id="clock"></span>
    <br><a href="/logout" style="font-size:10px;color:#484f58;text-decoration:none;padding:2px 8px;border:1px solid #21262d;border-radius:4px;margin-top:4px;display:inline-block;transition:all .2s" onmouseover="this.style.color='#ff4757'" onmouseout="this.style.color='#484f58'">⏻ 로그아웃</a>
    <a href="/alerts" style="font-size:10px;color:#484f58;text-decoration:none;padding:2px 8px;border:1px solid #21262d;border-radius:4px;margin-top:4px;margin-left:4px;display:inline-block" onmouseover="this.style.color='#a78bfa'" onmouseout="this.style.color='#484f58'">🔔 알림조건</a>
    <a href="/settings/email" style="font-size:10px;color:#484f58;text-decoration:none;padding:2px 8px;border:1px solid #21262d;border-radius:4px;margin-top:4px;margin-left:4px;display:inline-block" onmouseover="this.style.color='#ffa502'" onmouseout="this.style.color='#484f58'">📧 알림설정</a>
    <a href="/status" style="font-size:10px;color:#484f58;text-decoration:none;padding:2px 8px;border:1px solid #21262d;border-radius:4px;margin-top:4px;margin-left:4px;display:inline-block" onmouseover="this.style.color='#06b6d4'" onmouseout="this.style.color='#484f58'">📡 상태</a>
    <a id="admin-link" href="/admin" style="font-size:10px;color:#484f58;text-decoration:none;padding:2px 8px;border:1px solid #21262d;border-radius:4px;margin-top:4px;margin-left:4px;display:none" onmouseover="this.style.color='#58a6ff'" onmouseout="this.style.color='#484f58'">👑 관리자</a>
    <a id="admin-link" href="/admin" style="font-size:10px;color:#484f58;text-decoration:none;padding:2px 8px;border:1px solid #21262d;border-radius:4px;margin-top:4px;margin-left:4px;display:none;transition:all .2s" onmouseover="this.style.color='#58a6ff'" onmouseout="this.style.color='#484f58'">👑 관리자</a>
  </div>
</header>

<div class="tabs-main">
  <div class="mtab active" id="mg-scanner" onclick="swGroup('scanner')">📊 스캐너</div>
  <div class="mtab" id="mg-setup"   onclick="swGroup('setup')">🎯 타점 분석</div>
  <div class="mtab" id="mg-market"  onclick="swGroup('market')">📡 시장 분석</div>
  <div class="mtab" id="mg-guide"   onclick="swGroup('guide')">📖 매매법</div>
  <div class="mtab" id="mg-bt"      onclick="swGroup('bt')">🧪 백테스트</div>
</div>
<div class="tabs-sub" id="sub-scanner">
  <div class="stab active" onclick="sw('surge')">🚀 급등 종목</div>
  <div class="stab" onclick="sw('high')">🏆 전고점 돌파</div>
  <div class="stab" onclick="sw('volscan')">📈 매수거래량 증가</div>
  <div class="stab" onclick="sw('cross')">🔀 교집합 스캔</div>
  <div class="stab" onclick="sw('fvgscan')">🧲 FVG 재진입</div>
  <div class="stab" onclick="sw('breakout')">🌅 상승 준비</div>
  <div class="stab" onclick="sw('highprob')">🎯 고승률 자리</div>
  <div class="stab" onclick="sw('fav')">⭐ 즐겨찾기</div>
</div>
<div class="tabs-sub" id="sub-setup" style="display:none">
  <div class="stab" onclick="sw('combo')">🔥 FVG+추세+VWAP</div>
  <div class="stab" onclick="sw('fvg')">⚡ FVG 분석</div>
  <div class="stab" onclick="sw('emafvg')">🎯 EMA+FVG</div>
  <div class="stab" onclick="sw('krma')">🇰🇷 거래량+MA</div>
  <div class="stab" onclick="sw('retr')">📐 25% 되돌림</div>
  <div class="stab" onclick="sw('setup')">💰 타점+수량</div>
</div>
<div class="tabs-sub" id="sub-market" style="display:none">
  <div class="stab" onclick="sw('trend')">📡 추세 분석</div>
  <div class="stab" onclick="sw('vwap')">📊 VWAP</div>
</div>
<div class="tabs-sub" id="sub-guide" style="display:none">
  <div class="stab" onclick="sw('guide')">📖 FVG 매매법</div>
</div>
<div class="tabs-sub" id="sub-bt" style="display:none">
  <div class="stab" onclick="sw('backtest')">🧪 전략 백테스트</div>
</div>


<!-- ══════ CROSS SCAN TAB ══════ -->
<div id="tab-cross" class="panel" style="display:none">
  <div class="pr">
    <div style="margin-bottom:4px;font-size:11px;color:var(--muted)">분석 방법 선택 (2개 이상 선택 → 교집합 추출)</div>
    <div class="cross-opts">
      <div class="cross-opt sel" id="cx-surge" onclick="cxToggle('surge',this)">
        <span class="cross-opt-check" id="cx-surge-chk">✓</span>
        <span class="cross-opt-icon">🚀</span>
        <div class="cross-opt-name">급등 종목</div>
        <div class="cross-opt-desc">설정 기간 내 최소 상승률 이상 종목</div>
      </div>
      <div class="cross-opt sel" id="cx-high" onclick="cxToggle('high',this)">
        <span class="cross-opt-check" id="cx-high-chk">✓</span>
        <span class="cross-opt-icon">🏆</span>
        <div class="cross-opt-name">전고점 돌파</div>
        <div class="cross-opt-desc">최근 기간 내 역사적 고점 돌파 종목</div>
      </div>
      <div class="cross-opt" id="cx-vol" onclick="cxToggle('vol',this)">
        <span class="cross-opt-check" id="cx-vol-chk"></span>
        <span class="cross-opt-icon">📈</span>
        <div class="cross-opt-name">매수거래량 증가</div>
        <div class="cross-opt-desc">거래량이 직전 동기 대비 크게 증가한 종목</div>
      </div>
    </div>

    <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:10px;margin-bottom:10px">
      <div class="ctrl"><label>시장</label>
        <select id="cx-mkt"><option value="ALL">전체</option><option value="KOSPI">코스피</option><option value="KOSDAQ">코스닥</option></select>
      </div>
      <div class="ctrl"><label>기간 (급등/전고점)</label>
        <select id="cx-period"><option value="14">2주</option><option value="30" selected>1개월</option><option value="60">2개월</option></select>
      </div>
      <div class="ctrl"><label>최소 상승률 (%)</label>
        <input type="number" id="cx-minpct" value="20" min="5" max="200" step="5" style="width:80px">
      </div>
      <div class="ctrl"><label>거래량 증가 기간</label>
        <select id="cx-voldays"><option value="1">1일</option><option value="3" selected>3일</option><option value="5">5일</option></select>
      </div>
      <div class="ctrl"><label>최소 거래량 증가율 (%)</label>
        <input type="number" id="cx-volratio" value="30" min="10" max="500" step="10" style="width:80px">
      </div>
    </div>
    <button class="btn" id="btn-cross" style="background:linear-gradient(135deg,#06b6d4,#a78bfa);color:#000;font-weight:700;font-size:13px" onclick="doCross()">🔀 교집합 스캔</button>
  </div>
  <div class="hint" style="margin-top:8px">
    선택한 <strong style="color:#06b6d4">2~3개 조건을 모두 만족하는 종목</strong>을 추출합니다.
    3개 조건 동시 충족 = <strong style="color:#a78bfa">최강 신호</strong> · 2개 조건 충족 = 유망 후보
  </div>
</div>

<!-- ══════ 고승률 자리 스캐너 ══════ -->
<div id="tab-highprob" class="panel" style="display:none">
  <div style="margin-bottom:8px;font-size:12px;color:var(--muted)">
    3가지 복합 조건 중 하나를 선택하세요 — 조건이 많을수록 종목 수는 줄지만 <strong style="color:var(--green)">승률은 올라갑니다</strong>
  </div>
  <div class="hp-combo-cards">
    <div class="hp-card sel sel-a" id="hp-card-a" onclick="hpSelect('a')">
      <div class="hp-winrate">75~85%</div>
      <div class="hp-card-icon">🚀</div>
      <div class="hp-card-title">콤보 A — 모멘텀 급등</div>
      <div class="hp-card-conds">
        <div class="hp-cond-item">급등 종목 (기간 내 상승)</div>
        <div class="hp-cond-item">FVG 재진입 (지지 구간)</div>
        <div class="hp-cond-item">매수거래량 증가</div>
      </div>
    </div>
    <div class="hp-card" id="hp-card-b" onclick="hpSelect('b')">
      <div class="hp-winrate">80~90%</div>
      <div class="hp-card-icon">🌅</div>
      <div class="hp-card-title">콤보 B — 상승 준비 완성</div>
      <div class="hp-card-conds">
        <div class="hp-cond-item">상승준비 4점 이상 (5가지 조건)</div>
        <div class="hp-cond-item">외국인 매수우세</div>
        <div class="hp-cond-item">FVG 구간 근접</div>
      </div>
    </div>
    <div class="hp-card" id="hp-card-c" onclick="hpSelect('c')">
      <div class="hp-winrate">85~90%</div>
      <div class="hp-card-icon">📐</div>
      <div class="hp-card-title">콤보 C — 25% 눌림목</div>
      <div class="hp-card-conds">
        <div class="hp-cond-item">22이평 이격 2.5%+ (되돌림 자리)</div>
        <div class="hp-cond-item">거래량 감소 (눌림목 완성)</div>
        <div class="hp-cond-item">저점 안정화 (하락 멈춤)</div>
      </div>
    </div>
  </div>

  <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:10px;margin-bottom:12px">
    <div class="ctrl"><label>시장</label>
      <select id="hp-mkt"><option value="ALL">전체</option><option value="KOSPI">코스피</option><option value="KOSDAQ">코스닥</option></select>
    </div>
    <div class="ctrl"><label>결과 수</label>
      <select id="hp-lim"><option value="30">30개</option><option value="50" selected>50개</option></select>
    </div>
  </div>
  <button class="btn" id="btn-highprob" style="background:linear-gradient(135deg,#00ff88,#a78bfa);color:#000;font-weight:700;font-size:13px" onclick="doHighProb()">🎯 고승률 자리 스캔</button>
  <div class="hint" style="margin-top:8px">
    거래량 상위 종목만 분석 (1~3분 소요) ·
    <strong style="color:var(--green)">콤보 A</strong>: 상승 모멘텀 확인 후 지지선 진입 ·
    <strong style="color:#a78bfa">콤보 B</strong>: 바닥 다지기 완성 진입 ·
    <strong style="color:#fb923c">콤보 C</strong>: 25% 되돌림 단타 최적 자리
  </div>
</div>

<!-- ══════ 상승 준비 구간 스캐너 ══════ -->
<div id="tab-breakout" class="panel" style="display:none">
  <div class="pr">
    <div class="ctrl"><label>시장</label>
      <select id="bo-mkt"><option value="ALL">전체</option><option value="KOSPI">코스피</option><option value="KOSDAQ">코스닥</option></select>
    </div>
    <div class="ctrl"><label>타임프레임</label>
      <select id="bo-tf"><option value="1d" selected>일봉</option><option value="4h">4시간봉</option><option value="1h">1시간봉</option></select>
    </div>
    <div class="ctrl"><label>분석 기간 (캔들 수)</label>
      <input type="number" id="bo-lookback" value="60" min="30" max="200" step="10" style="width:80px">
    </div>
    <div class="ctrl"><label>최소 점수 (5점 만점)</label>
      <select id="bo-minscore">
        <option value="3">3점 이상</option>
        <option value="4" selected>4점 이상</option>
        <option value="5">5점 만점만</option>
      </select>
    </div>
    <div class="ctrl"><label>결과 수</label>
      <select id="bo-lim"><option value="30">30개</option><option value="50" selected>50개</option><option value="100">100개</option></select>
    </div>
    <button class="btn" id="btn-breakout" style="background:linear-gradient(135deg,#ffa502,#00ff88);color:#000;font-weight:700" onclick="doBreakout()">🌅 상승 준비 스캔</button>
  </div>
  <div class="hint" style="margin-top:8px">
    <strong style="color:#ffa502">5가지 조건</strong>으로 상승 직전 구간 탐색:<br>
    ① <strong style="color:var(--green)">FVG 2개 이상</strong> 누적 (강한 수요 구간) &nbsp;
    ② <strong style="color:var(--green)">RSI 30~55</strong> (과매도 회복) &nbsp;
    ③ <strong style="color:var(--green)">저점 안정화</strong> (신저점 갱신 없음) &nbsp;
    ④ <strong style="color:var(--green)">EMA 수렴</strong> (가격 ≈ 이평선) &nbsp;
    ⑤ <strong style="color:var(--green)">거래량 감소</strong> (눌림목 완성)
  </div>
</div>

<!-- ══════ 고확신 신호 스캐너 ══════ -->
<div id="tab-hcs" class="panel" style="display:none">
  <div class="pr">
    <div class="ctrl"><label>시장</label>
      <select id="hcs-mkt"><option value="ALL">전체</option><option value="KOSPI">코스피</option><option value="KOSDAQ">코스닥</option></select>
    </div>
    <div class="ctrl"><label>타임프레임</label>
      <select id="hcs-tf"><option value="1d" selected>일봉</option><option value="4h">4시간봉</option></select>
    </div>
    <div class="ctrl"><label>분석 기간 (캔들)</label>
      <input type="number" id="hcs-lookback" value="60" min="20" max="120" step="10" style="width:80px">
    </div>
    <div class="ctrl"><label>결과 수</label>
      <select id="hcs-lim"><option value="20">20개</option><option value="30" selected>30개</option><option value="50">50개</option></select>
    </div>
    <button class="btn" id="btn-hcs" style="background:linear-gradient(135deg,#fb923c,#a78bfa,#06b6d4);color:#000;font-weight:800" onclick="doHcs()">⚡ 고확신 신호 스캔</button>
  </div>
  <div class="hint" style="margin-top:8px">
    <strong style="color:#fb923c">3가지 복합 전략</strong> 중 조건을 만족하는 종목만 추출 — 예상 승률 70~80%<br>
    🔶 <strong style="color:#fb923c">전략A</strong>: 급등+FVG재진입+매수거래량 동시 충족 &nbsp;
    🟣 <strong style="color:#a78bfa">전략B</strong>: 상승준비 4점↑+RSI회복 &nbsp;
    🔵 <strong style="color:#06b6d4">전략C</strong>: 25%이격+거래량감소 (눌림목 완성)
  </div>
</div>

<!-- ══════ FVG 재진입 스캐너 ══════ -->
<div id="tab-fvgscan" class="panel" style="display:none">
  <div class="pr">
    <div class="ctrl"><label>시장</label>
      <select id="fs-mkt"><option value="ALL">전체</option><option value="KOSPI">코스피</option><option value="KOSDAQ">코스닥</option></select>
    </div>
    <div class="ctrl"><label>타임프레임</label>
      <select id="fs-tf">
        <option value="1d" selected>일봉</option>
        <option value="4h">4시간봉</option>
        <option value="1h">1시간봉</option>
      </select>
    </div>
    <div class="ctrl"><label>FVG 감지 기간 (캔들 수)</label>
      <input type="number" id="fs-lookback" value="60" min="20" max="200" step="10" style="width:80px">
    </div>
    <div class="ctrl"><label>FVG 최소 크기 (%)</label>
      <input type="number" id="fs-minsize" value="0.5" min="0.1" max="5" step="0.1" style="width:80px">
      <span style="font-size:10px;color:var(--muted)">갭 크기 필터</span>
    </div>
    <div class="ctrl"><label>재진입 허용 범위 (%)</label>
      <input type="number" id="fs-tolerance" value="10" min="1" max="50" step="1" style="width:80px">
      <span style="font-size:10px;color:var(--muted)">FVG 내 진입 깊이</span>
    </div>
    <div class="ctrl"><label>결과 수</label>
      <select id="fs-lim"><option value="30">30개</option><option value="50" selected>50개</option><option value="100">100개</option></select>
    </div>
    <button class="btn" id="btn-fvgscan" style="background:linear-gradient(135deg,#06b6d4,#00ff88);color:#000;font-weight:700" onclick="doFvgScan()">🧲 FVG 재진입 스캔</button>
  </div>
  <div class="hint" style="margin-top:8px">
    <strong style="color:#06b6d4">FVG(공정가치 갭)</strong>가 형성된 후 가격이 그 구간으로 되돌아온 종목을 탐색합니다.
    FVG = 3개 봉 구조에서 첫 번째 봉 고점과 세 번째 봉 저점 사이 갭 (또는 반대).
    <strong style="color:var(--green)">상승 FVG</strong> 재진입 = 지지 구간 진입 기회 ·
    <strong style="color:var(--red)">하락 FVG</strong> 재진입 = 저항 구간 주의
  </div>
</div>

<!-- ══════ 즐겨찾기 TAB ══════ -->
<div id="tab-fav" class="panel" style="display:none">
  <div style="display:flex;align-items:center;gap:10px;margin-bottom:12px;flex-wrap:wrap">
    <div class="hist-tabs">
      <button class="hist-tab active" id="fav-tab-list" onclick="switchFavView('list')">⭐ 즐겨찾기 목록</button>
      <button class="hist-tab" id="fav-tab-hist" onclick="switchFavView('hist')">📅 가격 히스토리</button>
    </div>
    <span id="fav-count-badge" style="font-size:12px;color:var(--muted)"></span>
  </div>
  <div id="fav-res"></div>
  <div id="fav-hist-res" style="display:none"></div>
</div>

<!-- ══════ VOLSCAN TAB ══════ -->
<div id="tab-volscan" class="panel" style="display:none">
  <div class="pr">
    <div class="ctrl">
      <label>기간 선택</label>
      <div class="at-btns">
        <button class="at-btn" id="vs-d1" onclick="vsDay(1)">최근 1일</button>
        <button class="at-btn on-stock" id="vs-d3" onclick="vsDay(3)">최근 3일</button>
        <button class="at-btn" id="vs-d5" onclick="vsDay(5)">최근 5일</button>
      </div>
    </div>
    <div class="ctrl">
      <label>시장</label>
      <select id="vs-mkt"><option value="ALL">전체</option><option value="KOSPI">코스피</option><option value="KOSDAQ">코스닥</option></select>
    </div>
    <div class="ctrl">
      <label>최소 거래량 증가율 (%)</label>
      <input type="number" id="vs-minratio" value="50" min="10" max="500" step="10" style="width:90px">
    </div>
    <div class="ctrl">
      <label>결과 수</label>
      <select id="vs-lim"><option value="30">30개</option><option value="50" selected>50개</option><option value="100">100개</option></select>
    </div>
    <button class="btn" style="background:linear-gradient(135deg,#fb923c,#f43f5e);color:#000;font-weight:700" id="btn-volscan" onclick="doVolScan()">📈 스캔 시작</button>
  </div>
  <div class="hint" style="margin-top:8px">
    선택 기간 동안 <strong style="color:#fb923c">매수 거래량(양봉일 거래량)</strong>이 직전 동일 기간 대비 크게 증가한 종목을 자동 탐색합니다.
    거래량 급증 + 양봉 비율 높음 = <strong style="color:var(--txt)">세력/기관 매집 가능성</strong> 신호.
  </div>
</div>

<!-- ══════ VOLSCAN TAB ══════ -->
<div id="tab-volscan" class="panel" style="display:none">
  <div class="pr">
    <div class="ctrl">
      <label>기간 선택</label>
      <div class="at-btns">
        <button class="at-btn" id="vs-d1" onclick="vsDay(1)">최근 1일</button>
        <button class="at-btn on-stock" id="vs-d3" onclick="vsDay(3)">최근 3일</button>
        <button class="at-btn" id="vs-d5" onclick="vsDay(5)">최근 5일</button>
      </div>
    </div>
    <div class="ctrl"><label>시장</label>
      <select id="vs-mkt"><option value="ALL">전체</option><option value="KOSPI">코스피</option><option value="KOSDAQ">코스닥</option></select>
    </div>
    <div class="ctrl"><label>최소 거래량 증가율 (%)</label>
      <input type="number" id="vs-minratio" value="50" min="10" max="500" step="10" style="width:90px">
    </div>
    <div class="ctrl"><label>결과 수</label>
      <select id="vs-lim"><option value="30">30개</option><option value="50" selected>50개</option><option value="100">100개</option></select>
    </div>
    <button class="btn" style="background:linear-gradient(135deg,#fb923c,#f43f5e);color:#000;font-weight:700" id="btn-volscan" onclick="doVolScan()">📈 스캔 시작</button>
  </div>
  <div class="hint" style="margin-top:8px">
    선택 기간 동안 <strong style="color:#fb923c">매수 거래량(양봉일 거래량)</strong>이 직전 동일 기간 대비 크게 증가한 종목을 자동 탐색합니다.
    거래량 급증 + 양봉 비율 높음 = <strong style="color:var(--txt)">세력/기관 매집 가능성</strong>
  </div>
</div>

<!-- SURGE -->
<div id="tab-surge" class="panel">
  <div class="pr">
    <div class="ctrl"><label>시장</label><select id="s-mkt"><option value="ALL">전체</option><option value="KOSPI">코스피</option><option value="KOSDAQ">코스닥</option></select></div>
    <div class="ctrl"><label>기간</label><select id="s-per"><option value="7">1주일</option><option value="14">2주일</option><option value="30" selected>1개월</option><option value="60">2개월</option></select></div>
    <div class="ctrl"><label>최소 상승률(%)</label><input type="number" id="s-pct" value="30" min="5" max="300"></div>
    <div class="ctrl"><label>결과 수</label><select id="s-lim"><option value="50">50개</option><option value="100" selected>100개</option><option value="200">200개</option></select></div>
    <button class="btn btn-g" id="btn-surge" onclick="doScan('surge')">▶ 스캔 시작</button>
  </div>
</div>

<!-- HIGH -->
<div id="tab-high" class="panel" style="display:none">
  <div class="pr">
    <div class="ctrl"><label>시장</label><select id="h-mkt"><option value="ALL">전체</option><option value="KOSPI">코스피</option><option value="KOSDAQ">코스닥</option></select></div>
    <div class="ctrl"><label>전고점 기준</label><select id="h-per"><option value="90">3개월</option><option value="180">6개월</option><option value="365" selected>1년</option></select></div>
    <div class="ctrl"><label>돌파 확인 구간</label><select id="h-rec"><option value="14">2주</option><option value="30" selected>1개월</option><option value="60">2개월</option></select></div>
    <div class="ctrl"><label>결과 수</label><select id="h-lim"><option value="50">50개</option><option value="100" selected>100개</option><option value="200">200개</option></select></div>
    <button class="btn btn-g" id="btn-high" onclick="doScan('high')">▶ 스캔 시작</button>
  </div>
</div>

<!-- ══════ FVG TAB ══════ -->
<div id="tab-fvg" class="panel" style="display:none">

  <!-- 자산 유형 토글 -->
  <div style="margin-bottom:14px">
    <div class="at-wrap">
      <div class="at-label">자산 유형</div>
      <div class="at-btns">
        <button class="at-btn on-stock" id="fvg-as" onclick="fvgType('stock')">📈 주식</button>
        <button class="at-btn" id="fvg-ac" onclick="fvgType('crypto')">₿ 코인</button>
      </div>
    </div>
  </div>

  <!-- 주식 입력 -->
  <div id="fvg-s" class="pr">
    <div class="ctrl"><label>종목 코드</label><input type="text" id="fvg-code" placeholder="005930" maxlength="6"></div>
    <div class="ctrl"><label>시장</label><select id="fvg-mkt"><option value="KS">코스피</option><option value="KQ">코스닥</option></select></div>
    <button class="btn btn-b" onclick="doFVG()">⚡ FVG 분석</button>
  </div>
  <div id="fvg-sh" class="hint">삼성전자=005930(KS) · SK하이닉스=000660(KS) · 카카오=035720(KQ)</div>

  <!-- 코인 입력 (기본 숨김) -->
  <div id="fvg-c" class="pr" style="display:none">
    <div class="ctrl">
      <label>코인 선택</label>
      <div class="coin-row">
        <button class="cb sel-btc" id="fvg-btc" onclick="fvgCoin('BTC-USD','btc')">₿ BTC</button>
        <button class="cb" id="fvg-eth" onclick="fvgCoin('ETH-USD','eth')">Ξ ETH</button>
        <button class="cb" id="fvg-xrp" onclick="fvgCoin('XRP-USD','xrp')">✕ XRP</button>
      </div>
    </div>
    <button class="btn btn-b" onclick="doFVGcoin()">⚡ FVG 분석</button>
  </div>
  <div id="fvg-ch" class="hint" style="display:none">Bitcoin(BTC-USD) · Ethereum(ETH-USD) · XRP(XRP-USD) — yfinance 실시간 24/7</div>

</div>

<!-- GUIDE -->
<div id="tab-guide" class="panel" style="display:none;padding-bottom:26px">
  <div class="gw">
    <div class="gc2 full cflow">
      <div class="gct">📌 FVG란? (Fair Value Gap)</div>
      <div style="font-size:12px;color:var(--muted);line-height:1.8;margin-bottom:11px">3개 연속 캔들에서 <strong style="color:var(--txt)">1번 캔들 고/저가 ↔ 3번 캔들 저/고가 사이 공백</strong>. 스마트머니는 이 구간으로 가격 복귀 시 재진입합니다.</div>
      <div class="flow">
        <div class="fs2"><div class="fsn">01</div><div class="fst">갭 형성</div><div class="fss">3캔들</div></div><div class="fa">→</div>
        <div class="fs2"><div class="fsn">02</div><div class="fst">추세확인</div><div class="fss">MTF정렬</div></div><div class="fa">→</div>
        <div class="fs2"><div class="fsn">03</div><div class="fst">리테스트</div><div class="fss">FVG 50%</div></div><div class="fa">→</div>
        <div class="fs2"><div class="fsn">04</div><div class="fst">반응캔들</div><div class="fss">진입확인</div></div><div class="fa">→</div>
        <div class="fs2"><div class="fsn">05</div><div class="fst">진입+SL/TP</div><div class="fss">1:2 이상</div></div>
      </div>
    </div>
    <div class="gc2 cbull">
      <div class="gct">🟢 불리시 FVG 매수</div>
      <svg class="cdemo" viewBox="0 0 290 105" xmlns="http://www.w3.org/2000/svg">
        <rect width="290" height="105" fill="#0d1117" rx="6"/>
        <line x1="0" y1="26" x2="290" y2="26" stroke="#21262d" stroke-width="1"/><line x1="0" y1="53" x2="290" y2="53" stroke="#21262d" stroke-width="1"/><line x1="0" y1="79" x2="290" y2="79" stroke="#21262d" stroke-width="1"/>
        <line x1="52" y1="13" x2="52" y2="84" stroke="#ff4757" stroke-width="1.5"/><rect x="41" y="36" width="22" height="34" fill="#ff4757" rx="2"/>
        <line x1="106" y1="21" x2="106" y2="72" stroke="#00ff88" stroke-width="1.5"/><rect x="95" y="26" width="22" height="34" fill="#00ff88" rx="2"/>
        <rect x="117" y="47" width="48" height="18" fill="rgba(0,255,136,0.12)" rx="2"/>
        <line x1="117" y1="47" x2="165" y2="47" stroke="#00ff88" stroke-width="1" stroke-dasharray="3,2"/><line x1="117" y1="65" x2="165" y2="65" stroke="#00ff88" stroke-width="1" stroke-dasharray="3,2"/>
        <text x="130" y="60" fill="#00ff88" font-size="8" font-family="monospace">FVG</text>
        <line x1="158" y1="17" x2="158" y2="62" stroke="#00ff88" stroke-width="1.5"/><rect x="147" y="21" width="22" height="30" fill="#00ff88" rx="2"/>
        <path d="M205,30 C215,30 218,56 228,56" stroke="#ffa502" stroke-width="1.5" fill="none" stroke-dasharray="4,2"/>
        <line x1="244" y1="44" x2="244" y2="72" stroke="#00ff88" stroke-width="1.5"/><rect x="233" y="48" width="22" height="17" fill="#00ff88" rx="2"/>
        <text x="186" y="23" fill="#ffa502" font-size="8" font-family="monospace">리테스트</text>
        <text x="224" y="92" fill="#00ff88" font-size="9" font-family="monospace">▲ 매수</text>
      </svg>
      <div class="steps">
        <div class="step"><div class="sn sg2">1</div><div><div class="st">FVG 확인</div><div class="sd">캔들1고가 &lt; 캔들3저가 → <strong>위로 열린 갭</strong></div></div></div>
        <div class="step"><div class="sn sg2">2</div><div><div class="st">상위 추세</div><div class="sd">일봉/4H <strong>상승추세</strong>일 때만</div></div></div>
        <div class="step"><div class="sn sg2">3</div><div><div class="st">FVG 50% 리테스트</div><div class="sd">갭 중간 하락 후 <strong>반등 캔들</strong></div></div></div>
        <div class="step"><div class="sn sg2">4</div><div><div class="st">진입 + 손절</div><div class="sd">FVG 50% 매수 · SL: <strong>FVG 하단 아래</strong></div></div></div>
      </div>
    </div>
    <div class="gc2 cbear">
      <div class="gct">🔴 베어리시 FVG 매도</div>
      <svg class="cdemo" viewBox="0 0 290 105" xmlns="http://www.w3.org/2000/svg">
        <rect width="290" height="105" fill="#0d1117" rx="6"/>
        <line x1="0" y1="26" x2="290" y2="26" stroke="#21262d" stroke-width="1"/><line x1="0" y1="53" x2="290" y2="53" stroke="#21262d" stroke-width="1"/><line x1="0" y1="79" x2="290" y2="79" stroke="#21262d" stroke-width="1"/>
        <line x1="52" y1="21" x2="52" y2="87" stroke="#00ff88" stroke-width="1.5"/><rect x="41" y="34" width="22" height="34" fill="#00ff88" rx="2"/>
        <line x1="106" y1="17" x2="106" y2="77" stroke="#ff4757" stroke-width="1.5"/><rect x="95" y="40" width="22" height="34" fill="#ff4757" rx="2"/>
        <rect x="117" y="32" width="48" height="18" fill="rgba(255,71,87,0.12)" rx="2"/>
        <line x1="117" y1="32" x2="165" y2="32" stroke="#ff4757" stroke-width="1" stroke-dasharray="3,2"/><line x1="117" y1="50" x2="165" y2="50" stroke="#ff4757" stroke-width="1" stroke-dasharray="3,2"/>
        <text x="130" y="45" fill="#ff4757" font-size="8" font-family="monospace">FVG</text>
        <line x1="158" y1="44" x2="158" y2="94" stroke="#ff4757" stroke-width="1.5"/><rect x="147" y="54" width="22" height="30" fill="#ff4757" rx="2"/>
        <path d="M205,74 C215,74 218,44 228,44" stroke="#ffa502" stroke-width="1.5" fill="none" stroke-dasharray="4,2"/>
        <line x1="244" y1="32" x2="244" y2="62" stroke="#ff4757" stroke-width="1.5"/><rect x="233" y="36" width="22" height="19" fill="#ff4757" rx="2"/>
        <text x="186" y="89" fill="#ffa502" font-size="8" font-family="monospace">리테스트</text>
        <text x="224" y="14" fill="#ff4757" font-size="9" font-family="monospace">▼ 매도</text>
      </svg>
      <div class="steps">
        <div class="step"><div class="sn sr">1</div><div><div class="st">FVG 확인</div><div class="sd">캔들1저가 &gt; 캔들3고가 → <strong>아래로 열린 갭</strong></div></div></div>
        <div class="step"><div class="sn sr">2</div><div><div class="st">하락 추세</div><div class="sd">일봉/4H <strong>하락추세</strong>일 때만</div></div></div>
        <div class="step"><div class="sn sr">3</div><div><div class="st">FVG 50% 리테스트</div><div class="sd">갭 중간 반등 후 <strong>거부 캔들</strong></div></div></div>
        <div class="step"><div class="sn sr">4</div><div><div class="st">진입 + 손절</div><div class="sd">FVG 50% 매도 · SL: <strong>FVG 상단 위</strong></div></div></div>
      </div>
    </div>
    <div class="gc2 full cmtf">
      <div class="gct">🔭 멀티타임프레임 전략</div>
      <table class="mt">
        <thead><tr><th>타임프레임</th><th>역할</th><th>확인 내용</th></tr></thead>
        <tbody>
          <tr><td class="tf">1개월/1주일</td><td class="role">거시 추세</td><td>전체 방향 — 이 방향으로만 매매</td></tr>
          <tr><td class="tf">1일/4시간</td><td class="role">중기 추세</td><td>스윙 방향 + FVG 구조 파악</td></tr>
          <tr><td class="tf">1시간</td><td class="role">진입 TF</td><td>FVG 형성 + 리테스트 대기</td></tr>
          <tr><td class="tf">15분/5분</td><td class="role">트리거</td><td>정확한 진입 시점 + 반전 캔들</td></tr>
          <tr><td class="tf">1분</td><td class="role">정밀 진입</td><td>스캘핑 or 진입가 최적화</td></tr>
        </tbody>
      </table>
    </div>
    <div class="gc2 crisk">
      <div class="gct" style="color:var(--purple)">🛡️ 리스크 관리</div>
      <div class="rg">
        <div class="ri"><div class="rl">손절 위치</div><div class="rv r">FVG 구간 외부</div><div class="rd">갭 완전히 채워지면 즉시 청산</div></div>
        <div class="ri"><div class="rl">목표가</div><div class="rv g">1:2 ~ 1:3 RR</div><div class="rd">절반 익절 후 나머지 운영</div></div>
        <div class="ri"><div class="rl">진입 비중</div><div class="rv y">계좌 1~2%</div><div class="rd">손절폭 계산 후 수량 결정</div></div>
        <div class="ri"><div class="rl">무효화</div><div class="rv b">즉시 청산</div><div class="rd">상위 추세 전환 시</div></div>
      </div>
    </div>
    <div class="gc2 crule">
      <div class="gct" style="color:var(--yellow)">✅ 진입 체크리스트</div>
      <div class="steps">
        <div class="step"><div class="sn sb">①</div><div><div class="st">상위 TF 추세 방향?</div><div class="sd">일봉/4H <strong>같은 방향</strong>만</div></div></div>
        <div class="step"><div class="sn sb">②</div><div><div class="st">FVG 완전히 형성?</div><div class="sd">3번째 캔들 <strong>닫힌 후</strong> 확인</div></div></div>
        <div class="step"><div class="sn sb">③</div><div><div class="st">FVG 50% 리테스트?</div><div class="sd">너무 얕거나 깊으면 <strong>패스</strong></div></div></div>
        <div class="step"><div class="sn sb">④</div><div><div class="st">반응 캔들?</div><div class="sd">핀바 · 엔걸핑 등 <strong>반전 신호</strong></div></div></div>
        <div class="step"><div class="sn sy">⑤</div><div><div class="st">RR 1:2 이상?</div><div class="sd">안 되면 <strong>진입 안 함</strong></div></div></div>
      </div>
    </div>
    <div class="gc2 full" style="border-color:rgba(255,165,2,.2)">
      <div class="gct" style="color:var(--yellow)">⚠️ 주의사항</div>
      <div class="wb" style="background:transparent;border:none;padding:0;font-size:12px">
        <strong>1.</strong> FVG는 항상 채워지지 않습니다 — 강한 추세에서는 그대로 지나침<br>
        <strong>2.</strong> 뉴스/공시·크립토 이슈 구간은 FVG 무효<br>
        <strong>3.</strong> 거래량 없는 FVG는 신뢰도 낮음<br>
        <strong>4.</strong> 이 프로그램은 <strong>참고용</strong>입니다 — 모든 투자 판단·손익은 본인 책임
      </div>
    </div>
  </div>
</div>

<!-- ══════ SETUP TAB ══════ -->
<div id="tab-setup" class="panel" style="display:none">
  <!-- 자산 유형 토글 -->
  <div style="margin-bottom:14px">
    <div class="at-wrap">
      <div class="at-label">자산 유형</div>
      <div class="at-btns">
        <button class="at-btn on-stock" id="st-as" onclick="stType('stock')">📈 주식</button>
        <button class="at-btn" id="st-ac" onclick="stType('crypto')">₿ 코인</button>
      </div>
    </div>
  </div>

  <!-- 주식 입력 -->
  <div id="st-s" class="pr">
    <div class="ctrl"><label>종목 코드</label><input type="text" id="st-code" placeholder="005930" maxlength="6"></div>
    <div class="ctrl"><label>시장</label><select id="st-mkt"><option value="KS">코스피</option><option value="KQ">코스닥</option></select></div>
    <div class="ctrl"><label>계좌 금액 (원)</label><input type="number" id="st-acct" value="10000000" min="100000" step="1000000"></div>
    <div class="ctrl"><label>리스크 (%)</label><input type="number" id="st-risk" value="1" min="0.1" max="5" step="0.1"></div>
    <button class="btn btn-o" id="btn-st-stock" onclick="startSetupLive('stock')">🎯 타점 분석</button>
  </div>
  <div id="st-sh" class="hint">💡 FVG 기반 진입가·손절·수익라인을 5분봉~월봉 자동 계산. 계좌금액+리스크% 입력 시 적정 수량도 제안.</div>

  <!-- 코인 입력 -->
  <div id="st-c" class="pr" style="display:none">
    <div class="ctrl">
      <label>코인 선택</label>
      <div class="coin-row">
        <button class="cb sel-btc" id="st-btc" onclick="stCoin('BTC-USD','btc')">₿ BTC</button>
        <button class="cb" id="st-eth" onclick="stCoin('ETH-USD','eth')">Ξ ETH</button>
        <button class="cb" id="st-xrp" onclick="stCoin('XRP-USD','xrp')">✕ XRP</button>
      </div>
    </div>
    <div class="ctrl"><label>투자 금액 (USD)</label><input type="number" id="st-usd" value="10000" min="100" step="1000"></div>
    <div class="ctrl"><label>리스크 (%)</label><input type="number" id="st-rusd" value="1" min="0.1" max="5" step="0.1"></div>
    <button class="btn btn-o" id="btn-st-crypto" onclick="startSetupLive('crypto')">🎯 타점 분석</button>
  </div>
  <div id="st-ch" class="hint" style="display:none">💡 BTC·ETH·XRP FVG 타점. 투자금액(USD)+리스크% 입력 시 코인 수량·달러 손익 자동 계산.</div>

  <!-- 실시간 상태바 (분석 시작 후 표시) -->
  <div id="st-live-bar" style="display:none;margin-top:12px;padding:10px 14px;background:var(--s3);border:1px solid rgba(0,255,136,.3);border-radius:8px;display:none;align-items:center;gap:12px;flex-wrap:wrap">
    <span style="display:flex;align-items:center;gap:6px">
      <span id="st-live-dot" style="width:8px;height:8px;border-radius:50%;background:var(--green);animation:glow 1.5s ease-in-out infinite"></span>
      <span style="font-size:11px;color:var(--green);font-weight:700;font-family:'JetBrains Mono'">LIVE</span>
    </span>
    <span style="font-size:12px;color:var(--muted)" id="st-live-ticker">—</span>
    <span style="font-family:'JetBrains Mono';font-size:14px;color:var(--txt)" id="st-live-price">—</span>
    <span style="font-family:'JetBrains Mono';font-size:12px" id="st-live-chg">—</span>
    <span style="margin-left:auto;font-size:11px;color:var(--muted)">다음 갱신: <span id="st-live-countdown" style="color:var(--yellow);font-family:'JetBrains Mono'">30</span>초</span>
    <button onclick="stopSetupLive()" style="padding:4px 12px;border-radius:5px;background:rgba(255,71,87,.15);color:var(--red);border:1px solid rgba(255,71,87,.3);cursor:pointer;font-size:11px;font-weight:700">■ 중지</button>
    <button onclick="refreshSetupNow()" style="padding:4px 12px;border-radius:5px;background:rgba(0,255,136,.12);color:var(--green);border:1px solid rgba(0,255,136,.3);cursor:pointer;font-size:11px;font-weight:700">↺ 지금 갱신</button>
  </div>

</div>

<!-- ══════ VWAP TAB ══════ -->
<div id="tab-vwap" class="panel" style="display:none">
  <div class="pr">
    <div class="at-wrap">
      <div class="at-label">자산 유형</div>
      <div class="at-btns">
        <button class="at-btn on-crypto" id="vw-ac" onclick="vwType('crypto')">₿ 코인</button>
        <button class="at-btn" id="vw-as" onclick="vwType('stock')">📈 주식</button>
      </div>
    </div>
    <!-- 코인 -->
    <div id="vw-c" class="ctrl">
      <label>코인 (복수 선택)</label>
      <div class="coin-row">
        <button class="cb sel-btc" id="vw-btc" onclick="vwToggle('BTC-USD','btc')">₿ BTC</button>
        <button class="cb sel-eth" id="vw-eth" onclick="vwToggle('ETH-USD','eth')">Ξ ETH</button>
        <button class="cb sel-xrp" id="vw-xrp" onclick="vwToggle('XRP-USD','xrp')">✕ XRP</button>
      </div>
    </div>
    <!-- 주식 -->
    <div id="vw-s" class="ctrl" style="display:none">
      <label>종목 코드</label>
      <input type="text" id="vw-code" placeholder="005930" maxlength="6" style="width:140px">
    </div>
    <div id="vw-sm" class="ctrl" style="display:none">
      <label>시장</label>
      <select id="vw-mkt"><option value="KS">코스피</option><option value="KQ">코스닥</option></select>
    </div>
    <div class="ctrl">
      <label>분석 기간</label>
      <div class="at-btns">
        <button class="at-btn on-blue" id="vw-p1d" onclick="vwPeriod('1d')">1일</button>
        <button class="at-btn" id="vw-p7d" onclick="vwPeriod('7d')">7일</button>
        <button class="at-btn" id="vw-p1m" onclick="vwPeriod('1m')">1개월</button>
      </div>
    </div>
    <button class="btn" style="background:#06b6d4;color:#000;font-weight:700" onclick="doVwap()">📊 VWAP 분석</button>
  </div>
  <div class="hint" style="margin-top:8px">
    VWAP = 거래량 가중 평균가 —
    가격이 VWAP <strong style="color:var(--green)">위</strong>=매수 우위 /
    <strong style="color:var(--red)">아래</strong>=매도 우위 /
    <strong style="color:var(--yellow)">±0.3% 이내</strong>=중립(진입 구간)<br>
    <strong style="color:#06b6d4">1일</strong>: 5분·15분·1시간봉 /
    <strong style="color:#06b6d4">7일</strong>: 1시간·4시간봉 /
    <strong style="color:#06b6d4">1개월</strong>: 4시간·일봉
  </div>
</div>

<!-- ══════ EMA+FVG TAB ══════ -->
<div id="tab-emafvg" class="panel" style="display:none">
  <div class="pr">
    <div class="at-wrap">
      <div class="at-label">자산 유형</div>
      <div class="at-btns">
        <button class="at-btn on-crypto" id="ef-ac" onclick="efType('crypto')">₿ 코인</button>
        <button class="at-btn" id="ef-as" onclick="efType('stock')">📈 주식</button>
      </div>
    </div>
    <div id="ef-c" class="ctrl">
      <label>코인 선택</label>
      <div class="coin-row">
        <button class="cb sel-btc" id="ef-btc" onclick="efCoin('BTC-USD','btc')">₿ BTC</button>
        <button class="cb" id="ef-eth" onclick="efCoin('ETH-USD','eth')">Ξ ETH</button>
        <button class="cb" id="ef-xrp" onclick="efCoin('XRP-USD','xrp')">✕ XRP</button>
      </div>
    </div>
    <div id="ef-s" class="ctrl" style="display:none">
      <label>종목 코드</label>
      <input type="text" id="ef-code" placeholder="005930" maxlength="6" style="width:140px">
    </div>
    <div id="ef-sm" class="ctrl" style="display:none">
      <label>시장</label>
      <select id="ef-mkt"><option value="KS">코스피</option><option value="KQ">코스닥</option></select>
    </div>
    <div class="ctrl">
      <label>투자금액</label>
      <input type="number" id="ef-acct" value="10000" step="1000" style="width:120px">
      <span style="font-size:10px;color:var(--muted);margin-top:2px" id="ef-unit">USD</span>
    </div>
    <div class="ctrl">
      <label>리스크 (%)</label>
      <input type="number" id="ef-risk" value="1" min="0.1" max="5" step="0.1" style="width:80px">
    </div>
    <button class="btn" style="background:#34d399;color:#000;font-weight:700" onclick="doEmaFvg()">🎯 타점 분석</button>
  </div>
  <div class="hint" style="margin-top:8px">
    <strong style="color:#34d399">EMA + FVG 스마트머니 전략</strong> —
    1H · 4H · 1D 멀티타임프레임 EMA 정렬 확인 후 <strong style="color:var(--txt)">FVG 리테스트 진입 타점</strong>을 정밀 제안합니다.
    EMA와 FVG가 겹치는 <strong style="color:var(--yellow)">컨플루언스 구간</strong>일수록 신뢰도가 높습니다.
  </div>
</div>

<!-- ══════ KR MA+VOL TAB ══════ -->
<div id="tab-krma" class="panel" style="display:none">
  <div class="pr">
    <div class="ctrl">
      <label>종목 코드</label>
      <input type="text" id="kr-code" placeholder="005930" maxlength="6" style="width:150px">
    </div>
    <div class="ctrl">
      <label>시장</label>
      <select id="kr-mkt"><option value="KS">코스피</option><option value="KQ">코스닥</option></select>
    </div>
    <div class="ctrl">
      <label>계좌 금액 (원)</label>
      <input type="number" id="kr-acct" value="10000000" step="1000000" style="width:150px">
    </div>
    <div class="ctrl">
      <label>리스크 (%)</label>
      <input type="number" id="kr-risk" value="1" min="0.1" max="5" step="0.1" style="width:80px">
    </div>
    <button class="btn" style="background:#fb923c;color:#000;font-weight:700" onclick="doKrMA()">🇰🇷 타점 분석</button>
  </div>
  <div class="hint" style="margin-top:8px">
    <strong style="color:#fb923c">거래량 + 이동평균선 한국주식 전략</strong> —
    5일·20일·60일·120일선 정렬 확인 + <strong style="color:var(--txt)">거래량 급증(평균 2배+)</strong> 조합으로
    매수/매도 타점을 제안합니다. <strong style="color:var(--yellow)">20일선 돌파 + 거래량 = 핵심 진입 신호</strong>
  </div>
</div>

<!-- ══════ 25% 되돌림 TAB ══════ -->
<div id="tab-retr" class="panel" style="display:none">
  <div class="pr">
    <div class="at-wrap">
      <div class="at-label">자산 유형</div>
      <div class="at-btns">
        <button class="at-btn on-crypto" id="retr-ac" onclick="retrType('crypto')">₿ 코인</button>
        <button class="at-btn" id="retr-as" onclick="retrType('stock')">📈 주식</button>
      </div>
    </div>
    <div id="retr-c" class="ctrl">
      <label>코인 선택</label>
      <div class="coin-row">
        <button class="cb sel-btc" id="retr-btc" onclick="retrCoin('BTC-USD','btc')">₿ BTC</button>
        <button class="cb" id="retr-eth" onclick="retrCoin('ETH-USD','eth')">Ξ ETH</button>
        <button class="cb" id="retr-xrp" onclick="retrCoin('XRP-USD','xrp')">✕ XRP</button>
      </div>
    </div>
    <div id="retr-s" class="ctrl" style="display:none">
      <label>종목 코드</label>
      <input type="text" id="retr-code" placeholder="005930" maxlength="6" style="width:140px">
    </div>
    <div id="retr-sm" class="ctrl" style="display:none">
      <label>시장</label>
      <select id="retr-mkt"><option value="KS">코스피</option><option value="KQ">코스닥</option></select>
    </div>
    <div class="ctrl">
      <label>타임프레임</label>
      <select id="retr-tf">
        <option value="1m">1분봉 (나스닥 단타)</option>
        <option value="5m" selected>5분봉</option>
        <option value="15m">15분봉</option>
        <option value="1h">1시간봉</option>
      </select>
    </div>
    <div class="ctrl">
      <label>MA 기간 (기본 22)</label>
      <input type="number" id="retr-ma" value="22" min="5" max="200" style="width:80px">
    </div>
    <div class="ctrl">
      <label>투자금액</label>
      <input type="number" id="retr-acct" value="10000" step="1000" style="width:120px">
      <span id="retr-unit" style="font-size:10px;color:var(--muted)">USD</span>
    </div>
    <div class="ctrl">
      <label>리스크 (%)</label>
      <input type="number" id="retr-risk" value="1" min="0.1" max="5" step="0.1" style="width:80px">
    </div>
    <button class="btn" style="background:linear-gradient(135deg,#fb923c,#a78bfa);color:#000;font-weight:700" onclick="doRetr()">📐 되돌림 분석</button>
  </div>
  <div class="hint" style="margin-top:8px">
    <strong style="color:#fb923c">김직선 25% 되돌림 전략</strong> —
    22이평선 괴리율 측정 → <strong style="color:var(--txt)">전략 A: 25% 익절 (승률 90%)</strong> /
    <strong style="color:#a78bfa">전략 B: 75% 이상 반등 시 추세전환 스윙</strong><br>
    손실은 <strong style="color:var(--red)">1% 이내</strong>로 제한, 수익은 <strong style="color:var(--green)">25% 구간</strong>만 챙기는 고승률 단타법
  </div>
</div>

<!-- ══════ BACKTEST TAB ══════ -->
<div id="tab-backtest" class="panel" style="display:none">

  <!-- 전략 카드 선택 -->
  <div class="bts-grid">
    <div class="bts-btn sel" id="bts-fvg" onclick="btStrategy('fvg')">
      <span class="bts-icon">⚡</span>
      <span class="bts-name">FVG 단독</span>
      <span class="bts-desc">불리시/베어리시 FVG 진입</span>
    </div>
    <div class="bts-btn" id="bts-vwap" onclick="btStrategy('vwap')">
      <span class="bts-icon">📊</span>
      <span class="bts-name">VWAP 단독</span>
      <span class="bts-desc">VWAP 이격·크로스 진입</span>
    </div>
    <div class="bts-btn" id="bts-combo" onclick="btStrategy('combo')">
      <span class="bts-icon">🔥</span>
      <span class="bts-name">FVG+추세+VWAP</span>
      <span class="bts-desc">3개 지표 종합 신호</span>
    </div>
    <div class="bts-btn" id="bts-emafvg" onclick="btStrategy('emafvg')">
      <span class="bts-icon">🎯</span>
      <span class="bts-name">EMA+FVG</span>
      <span class="bts-desc">EMA 정렬 + FVG 방향 일치</span>
    </div>
    <div class="bts-btn" id="bts-krma" onclick="btStrategy('krma')">
      <span class="bts-icon">🇰🇷</span>
      <span class="bts-name">거래량+MA</span>
      <span class="bts-desc">MA 정렬 + 거래량 급증</span>
    </div>
  </div>

  <!-- 자산 입력 (전략에 따라 코인/주식 전환) -->
  <div class="pr" id="bt-coin-row">
    <div class="at-wrap">
      <div class="at-label">코인 선택</div>
      <div class="at-btns">
        <button class="at-btn on-crypto" id="bt-btc" onclick="btCoin('BTC-USD','btc')">₿ BTC</button>
        <button class="at-btn" id="bt-eth" onclick="btCoin('ETH-USD','eth')">Ξ ETH</button>
        <button class="at-btn" id="bt-xrp" onclick="btCoin('XRP-USD','xrp')">✕ XRP</button>
      </div>
    </div>
  </div>
  <div class="pr" id="bt-stock-row" style="display:none">
    <div class="ctrl">
      <label>종목 코드</label>
      <input type="text" id="bt-kr-code" placeholder="005930" maxlength="6" style="width:140px">
    </div>
    <div class="ctrl">
      <label>시장</label>
      <select id="bt-kr-mkt"><option value="KS">코스피</option><option value="KQ">코스닥</option></select>
    </div>
  </div>
  <div class="pr">
    <div class="ctrl">
      <label>기간</label>
      <select id="bt-period">
        <option value="1">최근 1개월</option>
        <option value="3" selected>최근 3개월</option>
        <option value="6">최근 6개월</option>
      </select>
    </div>
    <div class="ctrl" id="bt-score-wrap">
      <label>최소 신호 점수</label>
      <select id="bt-minscore">
        <option value="2">±2 (많은 신호)</option>
        <option value="3" selected>±3 (권장)</option>
        <option value="4">±4 (엄격)</option>
        <option value="5">±5 (매우 엄격)</option>
      </select>
    </div>
    <div class="ctrl" id="bt-vwap-thresh-wrap" style="display:none">
      <label>VWAP 이격 임계 (%)</label>
      <input type="number" id="bt-vwap-thresh" value="0.3" min="0.1" max="2" step="0.1" style="width:90px">
    </div>
    <div class="ctrl">
      <label id="bt-acct-lbl">투자금액 (USD)</label>
      <input type="number" id="bt-acct" value="10000" min="100" step="1000" style="width:130px">
    </div>
    <div class="ctrl">
      <label>리스크 (%)</label>
      <input type="number" id="bt-risk" value="1" min="0.1" max="5" step="0.1" style="width:80px">
    </div>
    <button class="btn" style="background:#e879f9;color:#000;font-weight:700" id="btn-backtest" onclick="doBacktest()">🧪 백테스트 실행</button>
  </div>

  <div id="bt-hint" class="hint" style="margin-top:8px">
    4시간봉 · <strong style="color:var(--txt)">FVG 단독</strong> 전략 —
    불리시/베어리시 FVG 감지 → FVG 50% 진입 · SL: FVG 엣지 · TP1: 1:2 / TP2: 1:3 · 타임아웃: 20캔들
  </div>

</div>

<!-- ══════ COMBO TAB ══════ -->
<div id="tab-combo" class="panel" style="display:none">
  <div class="pr">
    <div class="at-wrap">
      <div class="at-label">자산 유형</div>
      <div class="at-btns">
        <button class="at-btn on-crypto" id="cb-ac" onclick="cbType('crypto')">₿ 코인</button>
        <button class="at-btn" id="cb-as" onclick="cbType('stock')">📈 주식</button>
      </div>
    </div>
    <div id="cb-c" class="ctrl">
      <label>코인 선택</label>
      <div class="coin-row">
        <button class="cb sel-btc" id="cb-btc" onclick="cbCoin('BTC-USD','btc')">₿ BTC</button>
        <button class="cb" id="cb-eth" onclick="cbCoin('ETH-USD','eth')">Ξ ETH</button>
        <button class="cb" id="cb-xrp" onclick="cbCoin('XRP-USD','xrp')">✕ XRP</button>
      </div>
    </div>
    <div id="cb-s" class="ctrl" style="display:none">
      <label>종목 코드</label>
      <input type="text" id="cb-code" placeholder="005930" maxlength="6" style="width:140px">
    </div>
    <div id="cb-sm" class="ctrl" style="display:none">
      <label>시장</label>
      <select id="cb-mkt"><option value="KS">코스피</option><option value="KQ">코스닥</option></select>
    </div>
    <div class="ctrl">
      <label>계좌/투자금액</label>
      <input type="number" id="cb-acct" value="10000000" min="100000" step="1000000" style="width:150px">
    </div>
    <div class="ctrl">
      <label>리스크 (%)</label>
      <input type="number" id="cb-risk" value="1" min="0.1" max="5" step="0.1" style="width:90px">
    </div>
    <button class="btn" style="background:#f43f5e;color:#fff;font-weight:700;font-size:13px" onclick="doCombo()">🔥 종합 분석</button>
  </div>
  <div class="hint" style="margin-top:8px">
    FVG(4H) + 추세(EMA/RSI) + VWAP 신호를 종합해 <strong style="color:var(--txt)">4시간봉 기준</strong>
    진입 타점·손절·목표가를 한 번에 제안합니다.
    <strong style="color:#f43f5e">신뢰도 점수</strong>가 높을수록 조건이 더 충족된 신호입니다.
  </div>
</div>

<!-- ══════ TREND TAB ══════ -->
<div id="tab-trend" class="panel" style="display:none">
  <div class="trend-top">
    <div class="at-wrap">
      <div class="at-label">자산 유형</div>
      <div class="at-btns">
        <button class="at-btn on-crypto" id="tr-ac" onclick="trType('crypto')">₿ 코인</button>
        <button class="at-btn" id="tr-as" onclick="trType('stock')">📈 주식</button>
      </div>
    </div>
    <!-- 코인 선택 -->
    <div id="tr-c-row" class="ctrl">
      <label>코인 선택 (복수)</label>
      <div class="coin-row">
        <button class="cb sel-btc" id="tr-btc" onclick="trToggleCoin('BTC-USD','btc')">₿ BTC</button>
        <button class="cb sel-eth" id="tr-eth" onclick="trToggleCoin('ETH-USD','eth')">Ξ ETH</button>
        <button class="cb sel-xrp" id="tr-xrp" onclick="trToggleCoin('XRP-USD','xrp')">✕ XRP</button>
      </div>
    </div>
    <!-- 주식 입력 -->
    <div id="tr-s-row" class="ctrl" style="display:none">
      <label>종목 코드</label>
      <input type="text" id="tr-code" placeholder="005930" maxlength="6" style="width:140px">
    </div>
    <div id="tr-s-mkt" class="ctrl" style="display:none">
      <label>시장</label>
      <select id="tr-mkt"><option value="KS">코스피</option><option value="KQ">코스닥</option></select>
    </div>
    <div class="ctrl">
      <label>분석 기간</label>
      <div class="at-btns">
        <button class="at-btn on-purple" id="tr-p1d" onclick="trPeriod('1d')">1일</button>
        <button class="at-btn" id="tr-p7d" onclick="trPeriod('7d')">7일</button>
        <button class="at-btn" id="tr-p1m" onclick="trPeriod('1m')">1개월</button>
      </div>
    </div>
    <button class="btn" style="background:#a78bfa;color:#000;font-weight:700" onclick="doTrend()">📡 추세 분석</button>
  </div>
  <div class="hint" style="margin-top:8px">
    EMA20/50/200 · RSI · 고점/저점 구조 기반 추세 판단<br>
    <strong style="color:#a78bfa">1일</strong>: 15분·1시간·4시간 /
    <strong style="color:#a78bfa">7일</strong>: 1시간·4시간·일봉 /
    <strong style="color:#a78bfa">1개월</strong>: 4시간·일봉·주봉
  </div>
</div>

<!-- PROGRESS -->
<div class="prog-wrap" id="prog">
  <div class="prog-bg"><div class="prog-fill" id="pbar"></div></div>
  <div class="prog-txt" id="ptxt">준비 중...</div>
</div>

<!-- RESULTS -->
<div id="scan-res" style="display:none">
  <div class="rh"><div class="rc" id="rcnt">0<span>종목</span></div><div style="display:flex;gap:8px;align-items:center"><span id="scan-label" style="font-size:11px;color:var(--muted)"></span><button class="btn-csv" onclick="dlCSV()">⬇ CSV</button><button class="btn-csv" style="background:rgba(255,71,87,.12);color:var(--red);border-color:rgba(255,71,87,.3)" onclick="clearScanRes()">🗑 지우기</button></div></div>
  <div class="tw"><table><thead id="tblh"></thead><tbody id="tblb"></tbody></table></div>
</div>
<div id="fvg-res"   style="display:none"></div>
<div id="setup-res" style="display:none"></div>
<div id="trend-res" style="display:none"></div>
<div id="vwap-res"  style="display:none"></div>
<div id="combo-res" style="display:none"></div>
<div id="backtest-res" style="display:none"></div>
<div id="emafvg-res"  style="display:none"></div>
<div id="krma-res"    style="display:none"></div>
<div id="volscan-res" style="display:none"></div>
<div id="cross-res"   style="display:none"></div>
<div id="retr-res"    style="display:none"></div>
<div id="fvgscan-res" style="display:none"></div>
<div id="breakout-res" style="display:none"></div>
<div id="highprob-res" style="display:none"></div>
<div id="hcs-res"    style="display:none"></div>
<div id="fav-res"    style="display:none"></div>

</div><!-- /wrap -->

<script>
/* ── clock ── */
syncFavsToServer();
setInterval(()=>{const n=new Date();document.getElementById('clock').textContent=n.toLocaleDateString('ko-KR')+' '+n.toLocaleTimeString('ko-KR')},1000);
// 관리자 링크 표시
fetch('/api/me').then(r=>r.json()).then(d=>{ if(d.is_admin){ const a=document.getElementById('admin-link'); if(a) a.style.display='inline-block'; } }).catch(()=>{});

/* ── state ── */
let curTab='surge', curData=[], sCol=null, sDir=-1;
let fvgCoinTicker='BTC-USD', fvgCoinCls='btc';
let stCoinTicker='BTC-USD', stCoinCls='btc';
let liveTimer=null, liveCountdown=null, liveParams=null, liveIsCrypto=false;
const LIVE_INTERVAL=30;

/* ── tab switch ── */
function sw(t){
  curTab=t;
  if(t!=='setup'){if(liveCountdown){clearInterval(liveCountdown);liveCountdown=null}}
  const allTabs=['surge','high','fvg','guide','setup','trend','vwap','combo','backtest','emafvg','krma','volscan','cross','retr','fvgscan','breakout','highprob','fav'];
  allTabs.forEach(id=>{
    const el=document.getElementById('tab-'+id);
    if(el) el.style.display=id===t?'':'none';
  });
  // update sub-tab active state
  document.querySelectorAll('.stab').forEach(el=>{
    const onclick=el.getAttribute('onclick')||'';
    el.classList.toggle('active', onclick.includes("'"+t+"'"));
  });
  const isScanTab=(t==='surge'||t==='high');
  const isVolTab=(t==='volscan');
  ['fvg-res','setup-res','trend-res','vwap-res','combo-res','backtest-res','emafvg-res','krma-res','retr-res'].forEach(id=>document.getElementById(id).style.display='none');
  if(!isScanTab) document.getElementById('scan-res').style.display='none';
  else if(curData.length>0) document.getElementById('scan-res').style.display='block';
  const vsEl=document.getElementById('volscan-res');
  if(!isVolTab){ vsEl.style.display='none'; }
  else if(vsEl.innerHTML.trim()){ vsEl.style.display='block'; }
  const isCrossTab=(t==='cross');
  const crEl=document.getElementById('cross-res');
  if(!isCrossTab){ crEl.style.display='none'; }
  else if(crEl.innerHTML.trim()){ crEl.style.display='block'; }
  const isFvgScanTab=(t==='fvgscan');
  const fsEl=document.getElementById('fvgscan-res');
  if(!isFvgScanTab){ fsEl.style.display='none'; }
  else if(fsEl.innerHTML.trim()){ fsEl.style.display='block'; }
  const isBreakoutTab=(t==='breakout');
  const boEl=document.getElementById('breakout-res');
  if(!isBreakoutTab){ boEl.style.display='none'; }
  else if(boEl.innerHTML.trim()){ boEl.style.display='block'; }
  const isHcsTab=(t==='hcs');
  const hcsEl=document.getElementById('hcs-res');
  if(!isHcsTab){ hcsEl.style.display='none'; }
  else if(hcsEl.innerHTML.trim()){ hcsEl.style.display='block'; }
  const isHPTab=(t==='highprob');
  const hpEl=document.getElementById('highprob-res');
  if(!isHPTab){ hpEl.style.display='none'; }
  else if(hpEl.innerHTML.trim()){ hpEl.style.display='block'; }
  const isFavTab=(t==='fav');
  const favEl=document.getElementById('fav-res');
  if(!isFavTab){ favEl.style.display='none'; }
  else{ favEl.style.display='block'; renderFavList(); }
  document.getElementById('prog').style.display='none';
  if(t==='setup'&&liveParams){
    document.getElementById('setup-res').style.display='block';
    document.getElementById('st-live-bar').style.display='flex';
    startCountdown();
  }
}

/* ── Group switcher ── */
const groupMap={
  scanner:['surge','high','volscan','cross','fvgscan','breakout','highprob','fav'],
  setup:['combo','fvg','emafvg','krma','retr','setup'],
  market:['trend','vwap'],
  guide:['guide'],
  bt:['backtest']
};
const groupDefault={scanner:'surge',setup:'combo',market:'trend',guide:'guide',bt:'backtest'};

function swGroup(g){
  // update main tab highlight
  document.querySelectorAll('.mtab').forEach(el=>{
    el.classList.toggle('active', el.id==='mg-'+g);
  });
  // show correct sub-tab bar
  ['scanner','setup','market','guide','bt'].forEach(id=>{
    const el=document.getElementById('sub-'+id);
    if(el) el.style.display=id===g?'flex':'none';
  });
  // 스캐너 그룹 벗어날 때 scan-res 강제 숨김
  if(g!=='scanner'){
    document.getElementById('scan-res').style.display='none';
  }
  // switch to default tab of group
  sw(groupDefault[g]||'surge');
}

/* ── FVG asset toggle ── */
function fvgType(t){
  const isStock=t==='stock';
  document.getElementById('fvg-as').className='at-btn'+(isStock?' on-stock':'');
  document.getElementById('fvg-ac').className='at-btn'+(!isStock?' on-crypto':'');
  document.getElementById('fvg-s').style.display=isStock?'flex':'none';
  document.getElementById('fvg-c').style.display=isStock?'none':'flex';
  document.getElementById('fvg-sh').style.display=isStock?'':'none';
  document.getElementById('fvg-ch').style.display=isStock?'none':'';
}
function fvgCoin(tk,cls){
  fvgCoinTicker=tk; fvgCoinCls=cls;
  ['btc','eth','xrp'].forEach(c=>{
    const b=document.getElementById('fvg-'+c); if(!b)return;
    b.className='cb'+(c===cls?' sel-'+c:'');
  });
}
function doFVGcoin(){
  hideAll();
  document.getElementById('fvg-res').style.display='block';
  document.getElementById('fvg-res').innerHTML=loading(fvgCoinTicker+' FVG 분석 중...');
  fetch('/fvg',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ticker:fvgCoinTicker,is_crypto:true})})
    .then(r=>r.json()).then(d=>renderFVG(d,true)).catch(e=>err('fvg-res',e));
}

/* ── Setup asset toggle ── */
function stType(t){
  const isStock=t==='stock';
  document.getElementById('st-as').className='at-btn'+(isStock?' on-stock':'');
  document.getElementById('st-ac').className='at-btn'+(!isStock?' on-crypto':'');
  document.getElementById('st-s').style.display=isStock?'flex':'none';
  document.getElementById('st-c').style.display=isStock?'none':'flex';
  document.getElementById('st-sh').style.display=isStock?'':'none';
  document.getElementById('st-ch').style.display=isStock?'none':'';
}
function stCoin(tk,cls){
  stCoinTicker=tk; stCoinCls=cls;
  ['btc','eth','xrp'].forEach(c=>{
    const b=document.getElementById('st-'+c); if(!b)return;
    b.className='cb'+(c===cls?' sel-'+c:'');
  });
}
/* ══════════════════════════════════════
   LIVE SETUP — 실시간 자동 갱신
══════════════════════════════════════ */
function startSetupLive(type){
  // 파라미터 수집
  let params;
  if(type==='stock'){
    const code=document.getElementById('st-code').value.trim();
    const mkt=document.getElementById('st-mkt').value;
    const acct=parseInt(document.getElementById('st-acct').value)||10000000;
    const risk=parseFloat(document.getElementById('st-risk').value)||1;
    if(!code||code.length<5){alert('종목 코드 입력');return}
    params={ticker:code.padStart(6,'0')+'.'+mkt,is_crypto:false,account:acct,risk_pct:risk};
    liveIsCrypto=false;
  } else {
    const acct=parseFloat(document.getElementById('st-usd').value)||10000;
    const risk=parseFloat(document.getElementById('st-rusd').value)||1;
    params={ticker:stCoinTicker,is_crypto:true,account:acct,risk_pct:risk};
    liveIsCrypto=true;
  }
  liveParams=params;

  // 기존 타이머 정리
  stopSetupLive(false);

  // 라이브바 표시
  const bar=document.getElementById('st-live-bar');
  bar.style.display='flex';
  document.getElementById('st-live-ticker').textContent=params.ticker;

  // 즉시 첫 분석
  fetchSetupLive();

  // 30초마다 자동 갱신
  liveTimer=setInterval(()=>fetchSetupLive(), LIVE_INTERVAL*1000);
  startCountdown();
}

function fetchSetupLive(){
  if(!liveParams) return;
  // 로딩 표시 (카드 유지하면서 상단에만 표시)
  const res=document.getElementById('setup-res');
  res.style.display='block';
  if(!res.innerHTML||res.innerHTML.includes('loading')){
    res.innerHTML=loading(liveParams.ticker+' 타점 분석 중...');
  } else {
    // 기존 카드 유지하고 업데이트 중 표시
    const existing=res.querySelector('.setup-updating');
    if(!existing){
      const upd=document.createElement('div');
      upd.className='setup-updating';
      upd.style.cssText='position:fixed;top:70px;right:20px;background:rgba(0,255,136,.12);border:1px solid rgba(0,255,136,.4);border-radius:8px;padding:8px 14px;font-size:12px;color:var(--green);font-family:JetBrains Mono,monospace;z-index:999;animation:glow 1s ease-in-out infinite';
      upd.textContent='⟳ 실시간 갱신 중...';
      document.body.appendChild(upd);
      setTimeout(()=>upd.remove(),8000);
    }
  }

  fetch('/setup',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(liveParams)})
    .then(r=>r.json())
    .then(d=>{
      renderSetup(d,liveIsCrypto);
      // 라이브바 가격 업데이트
      updateLiveBar(d);
      // 카운트다운 리셋
      startCountdown();
      // 업데이트 중 표시 제거
      document.querySelectorAll('.setup-updating').forEach(el=>el.remove());
    })
    .catch(e=>{
      document.querySelectorAll('.setup-updating').forEach(el=>el.remove());
      console.error('live update error:',e);
    });
}

function updateLiveBar(d){
  const fmt=v=>d.is_crypto?(v>=1?'$'+v.toLocaleString(undefined,{maximumFractionDigits:2}):'$'+v.toFixed(4)):v.toLocaleString()+'원';
  const chg=d.change||0;
  document.getElementById('st-live-ticker').textContent=d.name||d.ticker;
  document.getElementById('st-live-price').textContent=fmt(d.price||0);
  const chgEl=document.getElementById('st-live-chg');
  chgEl.textContent=(chg>=0?'+':'')+chg.toFixed(2)+'%';
  chgEl.style.color=chg>=0?'var(--green)':'var(--red)';
}

function startCountdown(){
  if(liveCountdown) clearInterval(liveCountdown);
  let sec=LIVE_INTERVAL;
  document.getElementById('st-live-countdown').textContent=sec;
  liveCountdown=setInterval(()=>{
    sec--;
    const el=document.getElementById('st-live-countdown');
    if(el) el.textContent=sec;
    if(sec<=0){ clearInterval(liveCountdown); liveCountdown=null; }
  },1000);
}

function stopSetupLive(showMsg=true){
  if(liveTimer){clearInterval(liveTimer);liveTimer=null}
  if(liveCountdown){clearInterval(liveCountdown);liveCountdown=null}
  document.querySelectorAll('.setup-updating').forEach(el=>el.remove());
  if(showMsg){
    const bar=document.getElementById('st-live-bar');
    if(bar) bar.style.display='none';
    liveParams=null;
  }
}

function refreshSetupNow(){
  if(liveCountdown) clearInterval(liveCountdown);
  fetchSetupLive();
}

/* ── 기존 단순 함수 (하위호환) ── */
function doSetupCoin(){startSetupLive('crypto')}
function doSetup(){startSetupLive('stock')}

/* ── Scan result persist + clear ── */
function clearScanRes(){
  curData=[];
  document.getElementById('scan-res').style.display='none';
  document.getElementById('tblh').innerHTML='';
  document.getElementById('tblb').innerHTML='';
  document.getElementById('rcnt').innerHTML='0<span>종목</span>';
  const lbl=document.getElementById('scan-label'); if(lbl) lbl.textContent='';
}
function clearVolRes(){
  document.getElementById('volscan-res').style.display='none';
  document.getElementById('volscan-res').innerHTML='';
}

/* ── Scan ── */
function doScan(type){
  const btn=document.getElementById('btn-'+type); btn.disabled=true;
  showProg(true); hideAll(); animP();
  const p=type==='surge'
    ?{type,market:g('s-mkt'),period:g('s-per'),minpct:g('s-pct'),limit:g('s-lim')}
    :{type,market:g('h-mkt'),period:g('h-per'),recent:g('h-rec'),limit:g('h-lim')};
  const ctrl=new AbortController();
  const timer=setTimeout(()=>ctrl.abort(),120000); // 2분 타임아웃
  fetch('/scan',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(p), signal:ctrl.signal})
    .then(r=>{
      clearTimeout(timer);
      if(r.status===401||r.status===302){location.href='/login';return null;}
      if(!r.ok) throw new Error('서버 오류: '+r.status);
      return r.json();
    })
    .then(d=>{
      if(!d) return;
      sp(100);
      setTimeout(()=>{
        btn.disabled=false; showProg(false);
        if(d.error){document.getElementById('scan-res').style.display='block';
          document.getElementById('tblb').innerHTML=`<tr><td colspan="8" style="text-align:center;color:var(--red);padding:20px">❌ ${d.error}</td></tr>`;return;}
        renderScan(d.results||[],type);
      },300);
    })
    .catch(e=>{
      clearTimeout(timer);
      btn.disabled=false; showProg(false);
      const msg=e.name==='AbortError'?'⏱ 스캔 시간 초과 (2분). 조건을 줄여보세요.':'스캔 실패: '+e;
      alert(msg);
    });
}

/* ── FVG (stock) ── */
function doFVG(){
  const code=document.getElementById('fvg-code').value.trim();
  const mkt=document.getElementById('fvg-mkt').value;
  if(!code||code.length<5){alert('종목 코드 입력');return}
  hideAll();
  document.getElementById('fvg-res').style.display='block';
  document.getElementById('fvg-res').innerHTML=loading('FVG 분석 중...');
  fetch('/fvg',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ticker:code.padStart(6,'0')+'.'+mkt,is_crypto:false})})
    .then(r=>r.json()).then(d=>renderFVG(d,false)).catch(e=>err('fvg-res',e));
}

/* ── Render scan ── */
function renderScan(data,type){
  curData=data;
  document.getElementById('scan-res').style.display='block';
  document.getElementById('rcnt').innerHTML=data.length+'<span>종목</span>';
  const lbl=document.getElementById('scan-label');
  if(lbl) lbl.textContent=(type==='surge'?'🚀 급등 종목':'🏆 전고점 돌파')+' · '+(new Date().toLocaleTimeString('ko-KR'));
  const iS=type==='surge';
  const cols=iS?['⭐','순위','코드','종목명','시장','등락률','시작가','현재가']:['⭐','순위','코드','종목명','시장','돌파율','전고점','돌파고점','현재가'];
  document.getElementById('tblh').innerHTML='<tr>'+cols.map((c,i)=>`<th>${c}</th>`).join('')+'</tr>';
  renderBody(data,iS);
}
function renderBody(data,iS){
  const b=document.getElementById('tblb');
  if(!data.length){b.innerHTML='<tr><td colspan="9" class="empty">조건에 맞는 종목 없음</td></tr>';return}
  b.innerHTML=data.map((r,i)=>{
    const badge=r.시장==='코스피'?'<span class="badge bkp">KOSPI</span>':'<span class="badge bkq">KOSDAQ</span>';
    const p=iS?r.등락률:r.돌파율; const pc=p>=50?'up':p>=30?'mid':'dn';
    const st=makeStar(r.코드,r.종목명,r.시장||'',r.현재가||0,r.등락률||0,iS?'surge':'high');
    return iS
      ?`<tr style="animation-delay:${Math.min(i,25)*12}ms"><td style="text-align:center">${st}</td><td class="r">${i+1}</td><td class="c">${r.코드}</td><td>${r.종목명}</td><td>${badge}</td><td class="pct ${pc}">+${p.toFixed(1)}%</td><td class="sub">${(r.시작가||0).toLocaleString()}원</td><td class="pr2">${(r.현재가||0).toLocaleString()}원</td></tr>`
      :`<tr style="animation-delay:${Math.min(i,25)*12}ms"><td style="text-align:center">${st}</td><td class="r">${i+1}</td><td class="c">${r.코드}</td><td>${r.종목명}</td><td>${badge}</td><td class="pct ${pc}">+${p.toFixed(1)}%</td><td class="sub">${(r.전고점||0).toLocaleString()}원</td><td class="sub">${(r.돌파고점||0).toLocaleString()}원</td><td class="pr2">${(r.현재가||0).toLocaleString()}원</td></tr>`;
  }).join('');
}
function sortBy(ci,cn){
  const iS=curTab==='surge';
  const km={'등락률':'등락률','돌파율':'돌파율','현재가':'현재가','시작가':'시작가','전고점':'전고점','돌파고점':'돌파고점'};
  const k=km[cn]; if(!k)return;
  if(sCol===k)sDir*=-1; else{sCol=k;sDir=-1}
  const s=[...curData].sort((a,b)=>((a[k]||0)-(b[k]||0))*sDir);
  document.querySelectorAll('thead th').forEach((th,i)=>{th.classList.remove('asc','desc');if(i===ci)th.classList.add(sDir===1?'asc':'desc')});
  renderBody(s,iS);
}

/* ── Render FVG ── */
function renderFVG(d,isCrypto){
  const wrap=document.getElementById('fvg-res');
  if(d.error){wrap.innerHTML=`<div class="empty">❌ ${d.error}</div>`;return}
  const tfL={'1mo':'1개월','1wk':'1주일','1d':'1일','4h':'4시간','1h':'1시간','15m':'15분','5m':'5분','1m':'1분'};
  const tfO=['1mo','1wk','1d','4h','1h','15m','5m','1m'];
  const chg=d.change||0;
  const fmtPrice=v=>isCrypto?(v>=1?'$'+v.toLocaleString(undefined,{maximumFractionDigits:2}):'$'+v.toFixed(5)):v.toLocaleString()+'원';
  const coinCls=d.ticker&&d.ticker.startsWith('BTC')?'cb-btc':d.ticker&&d.ticker.startsWith('ETH')?'cb-eth':'cb-xrp';
  const coinIco=d.ticker&&d.ticker.startsWith('BTC')?'₿':d.ticker&&d.ticker.startsWith('ETH')?'Ξ':'✕';

  let hdrHtml=isCrypto
    ?`<span class="cbadge ${coinCls}" style="font-size:16px;padding:5px 13px">${coinIco} ${d.ticker.split('-')[0]}</span><div><div class="fname">${d.name}</div><div class="fsub">${d.ticker} · 24/7</div></div>`
    :`<div class="fticker">${d.ticker.split('.')[0]}</div><div><div class="fname">${d.name}</div><div class="fsub">${d.ticker}</div></div>`;

  let html=`<div class="fvg-hdr">${hdrHtml}
    <div class="fprice" style="margin-left:auto">${fmtPrice(d.price||0)}
      <span style="font-family:'JetBrains Mono';font-size:12px;margin-left:5px;color:${chg>=0?'var(--green)':'var(--red)'}">${chg>=0?'+':''}${chg.toFixed(2)}%</span>
    </div>
  </div>
  <div class="legend"><span><div class="dot db"></div>불리시</span><span><div class="dot dr"></div>베어리시</span><span><div class="dot dn2"></div>없음</span></div>
  <div class="tf-grid">`;

  tfO.forEach((tf,idx)=>{
    const info=d.timeframes&&d.timeframes[tf];
    if(!info){html+=`<div class="tfc none" style="animation-delay:${idx*50}ms"><div class="tfl">${tfL[tf]}</div><div class="tfs none">N/A</div></div>`;return}
    const cls=info.signal==='BULL'?'bull':info.signal==='BEAR'?'bear':'none';
    const st=info.signal==='BULL'?'BULLISH FVG':info.signal==='BEAR'?'BEARISH FVG':'NO FVG';
    const fmtG=v=>isCrypto?(v>=1?'$'+v.toLocaleString(undefined,{maximumFractionDigits:2}):'$'+v.toFixed(5)):v.toLocaleString();
    html+=`<div class="tfc ${cls}" style="animation-delay:${idx*50}ms"><div class="tfl">${tfL[tf]}</div><div class="tfs ${cls}">${st}</div><div class="tfd">${info.signal!=='NONE'?`<div>수: <span class="gc">${info.count}</span>개</div><div>상단: <span class="gh">${fmtG(info.gap_high||0)}</span></div><div>하단: <span class="gl">${fmtG(info.gap_low||0)}</span></div>`:'<div style="color:var(--muted2)">미감지</div>'}</div></div>`;
  });
  html+='</div>'; wrap.innerHTML=html;
}

/* ── Render Setup ── */
function renderSetup(d,isCrypto){
  const wrap=document.getElementById('setup-res');
  if(d.error){wrap.innerHTML=`<div class="empty">❌ ${d.error}</div>`;return}
  const tfNames={'5m':'5분봉 스캘핑','15m':'15분봉 단타','1h':'1시간 단타','4h':'4시간 스윙','1d':'일봉 스윙','1wk':'주봉 포지션','1mo':'월봉 장기'};
  const tfOrder=['5m','15m','1h','4h','1d','1wk','1mo'];
  const setups=d.setups||{}; const chg=d.change||0;
  const fmt=v=>isCrypto?(v>=1?'$'+v.toLocaleString(undefined,{maximumFractionDigits:2}):'$'+v.toFixed(5)):v.toLocaleString()+'원';
  const fmtAmt=v=>isCrypto?'$'+v.toLocaleString(undefined,{maximumFractionDigits:2}):Math.round(v/10000).toLocaleString()+'만원';
  const unit=isCrypto?d.ticker.split('-')[0]:'주';

  const coinCls=isCrypto?(d.ticker.startsWith('BTC')?'cb-btc':d.ticker.startsWith('ETH')?'cb-eth':'cb-xrp'):'';
  const coinIco=isCrypto?(d.ticker.startsWith('BTC')?'₿':d.ticker.startsWith('ETH')?'Ξ':'✕'):'';

  const bullC=Object.values(setups).filter(s=>s&&s.signal==='BULL').length;
  const bearC=Object.values(setups).filter(s=>s&&s.signal==='BEAR').length;
  const actC=Object.values(setups).filter(s=>s&&s.status&&s.status.includes('✅')).length;

  const tfShort=['1mo','1wk','1d','4h','1h','15m','5m'];
  const pills=tfShort.map(tf=>{
    const s=setups[tf];
    if(!s||s.signal==='NONE') return `<span class="mpill none">${tf}</span>`;
    return `<span class="mpill ${s.signal==='BULL'?'bull':'bear'}">${s.signal==='BULL'?'🟢':'🔴'} ${tf}</span>`;
  }).join('');

  const heroCode=isCrypto?`<span class="cbadge ${coinCls}" style="font-size:18px;padding:7px 14px">${coinIco} ${d.ticker.split('-')[0]}</span>`
    :`<div class="hcode">${d.ticker.split('.')[0]}</div>`;

  let html=`
  <div class="hero">
    ${heroCode}
    <div><div class="hname">${d.name}</div><div class="hmkt">${d.ticker}${isCrypto?' · 24/7':''}</div></div>
    <div class="hprice" style="margin-left:auto">${fmt(d.price||0)}
      <span class="hchg" style="color:${chg>=0?'var(--green)':'var(--red)'}">${chg>=0?'+':''}${chg.toFixed(2)}%</span>
    </div>
  </div>
  <div class="sum-bar">
    <div class="sbox"><div class="slbl">현재가</div><div class="sval b" style="font-size:${isCrypto?'16':'22'}px">${fmt(d.price||0)}</div></div>
    <div class="sbox"><div class="slbl">불리시 TF</div><div class="sval g">${bullC}개</div></div>
    <div class="sbox"><div class="slbl">베어리시 TF</div><div class="sval r">${bearC}개</div></div>
    <div class="sbox"><div class="slbl">진입 가능</div><div class="sval o">${actC}개</div></div>
    <div class="sbox"><div class="slbl">투자금액</div><div class="sval y">${fmtAmt(d.account||0)}</div></div>
    <div class="sbox"><div class="slbl">리스크</div><div class="sval y">${d.risk_pct}%</div></div>
  </div>
  <div class="mtf-bar"><span class="mtf-lbl">MTF 정렬</span>${pills}</div>
  <div class="sg">`;

  tfOrder.forEach((tf,idx)=>{
    const s=setups[tf]; const tfName=tfNames[tf]||tf;
    if(!s||s.signal==='NONE'){
      html+=`<div class="sc non" style="animation-delay:${idx*65}ms"><div class="sch"><div><div class="sctf">${tfName}</div><div class="scst">FVG 없음</div></div><span class="scdir">NO FVG</span></div><div class="scb"><div style="color:var(--muted2);font-size:12px;padding:10px 0">이 타임프레임에 FVG가 감지되지 않았습니다.</div></div></div>`;
      return;
    }
    const isBull=s.signal==='BULL'; const isAct=s.status&&s.status.includes('✅');
    const coinQty=isCrypto&&s.entry>0?Math.floor(s.risk_amt/Math.abs(s.entry-s.sl)*10000)/10000:s.shares;

    html+=`<div class="sc ${isBull?'bul':'ber'}" style="animation-delay:${idx*65}ms">
      <div class="sch">
        <div><div class="sctf">${tfName}</div><div class="${isAct?'scst act':'scst'}">${s.status}</div></div>
        <span class="scdir">${isBull?'▲ 매수 LONG':'▼ 매도 SHORT'}</span>
      </div>
      <div class="scb">
        <div class="pl">
          <div class="plr pcur"><span class="pll">📍 현재가</span><span class="plv">${fmt(s.current||0)}</span></div>
          <div class="plr pe"><span class="pll">🎯 진입가</span><span class="plv">${fmt(s.entry||0)}</span></div>
          <div class="plr ps"><span class="pll">🛑 손절가</span><span><span class="plv">${fmt(s.sl||0)}</span><span class="plp">-${s.sl_pct}%</span></span></div>
          <div class="plr p1"><span class="pll">✅ 1차 (1:2)</span><span><span class="plv">${fmt(s.tp1||0)}</span><span class="plp">+${s.tp1_pct}%</span></span></div>
          <div class="plr p2"><span class="pll">🚀 2차 (1:3)</span><span><span class="plv">${fmt(s.tp2||0)}</span><span class="plp">+${s.tp2_pct}%</span></span></div>
          <div class="plr p3"><span class="pll">💎 3차 (1:5)</span><span><span class="plv">${fmt(s.tp3||0)}</span><span class="plp">+${s.tp3_pct}%</span></span></div>
        </div>
        <div class="rrbar">
          <div class="rrlbl"><span>Risk : Reward</span><span style="color:var(--green)">1 : ${s.rr||2}</span></div>
          <div class="rrtrack"><div class="rrl" style="width:33%"></div><div class="rrp" style="width:${Math.min(67,33*(s.rr||2))}%"></div></div>
        </div>
        <div class="mrow">
          <div class="mbox"><div class="mlbl">${isCrypto?'매수 수량':'적정 수량'}</div><div class="mval y">${isCrypto?(coinQty+' '+unit):(coinQty.toLocaleString()+' 주')}</div><div class="msub">리스크 ${d.risk_pct}% 기준</div></div>
          <div class="mbox"><div class="mlbl">투자 금액</div><div class="mval b">${fmtAmt(isCrypto?coinQty*(s.entry||0):s.invest_amt||0)}</div></div>
          <div class="mbox"><div class="mlbl">최대 손실</div><div class="mval r">${fmtAmt(s.risk_amt||0)}</div><div class="msub">계좌의 ${d.risk_pct}%</div></div>
          <div class="mbox"><div class="mlbl">1차 수익</div><div class="mval g">${fmtAmt((s.risk_amt||0)*2)}</div></div>
        </div>
        <div class="conds">
          <span class="cond ${isAct?'cok':'cwrn'}">${isAct?'✓ 진입 가능':'⏳ 리테스트 대기'}</span>
          <span class="cond ${isBull?'cok':'cno'}">${isBull?'↑ 불리시':'↓ 베어리시'}</span>
          <span class="cond cok">FVG: ${fmt(s.gap_low||0)}~${fmt(s.gap_high||0)}</span>
        </div>
      </div>
    </div>`;
  });
  html+=`</div><div class="warn-box">⚠️ <strong style="color:var(--yellow)">주의:</strong> 자동 계산 타점입니다. 실제 진입 전 반드시 반응 캔들·추세 정렬·거래량을 확인하세요. 모든 투자 판단과 손익은 <strong style="color:var(--txt)">본인 책임</strong>입니다.</div>`;
  wrap.innerHTML=html;
}

/* ── utils ── */
// 인증 만료 체크 — 401 응답 시 로그인 페이지로 이동
async function apiFetch(url, body){
  try {
    const r = await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    if(r.status===401){ location.href='/login'; return null; }
    if(!r.ok) throw new Error('서버 오류 '+r.status);
    const d = await r.json();
    if(d && d.redirect){ location.href=d.redirect; return null; }
    return d;
  } catch(e) {
    if(e.name!=='AbortError') throw e;
    return null;
  }
}
function g(id){return document.getElementById(id).value}

// 인증 만료 체크 래퍼
function hideAll(){['fvg-res','setup-res','trend-res','vwap-res','combo-res','backtest-res','emafvg-res','krma-res','volscan-res','cross-res','retr-res','fvgscan-res','breakout-res','highprob-res','fav-res'].forEach(id=>document.getElementById(id).style.display='none')}
function showProg(v){document.getElementById('prog').style.display=v?'block':'none'}
function sp(p){document.getElementById('pbar').style.width=p+'%'}
function loading(msg){return`<div style="text-align:center;padding:55px"><div class="spin"></div><br><span style="color:var(--muted);font-size:13px">${msg}<br><span style="font-size:11px;color:var(--muted2)">(10~30초 소요)</span></span></div>`}
function err(id,e){document.getElementById(id).innerHTML=`<div class="empty">❌ ${e}</div>`}
function animP(){
  let p=5; const msgs=['데이터 로딩...','종목 스캔...','분석 중...','필터링...','정렬...'];
  const iv=setInterval(()=>{p=Math.min(p+Math.random()*7,88);sp(p);document.getElementById('ptxt').textContent=msgs[Math.floor(p/20)]||'처리 중...';if(p>=88)clearInterval(iv)},700);
}
function dlCSV(){
  if(!curData.length)return;
  const keys=Object.keys(curData[0]);
  const rows=[keys.join(','),...curData.map(r=>keys.map(k=>r[k]).join(','))];
  const blob=new Blob(['\uFEFF'+rows.join('\n')],{type:'text/csv;charset=utf-8'});
  const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=`krscan_${new Date().toISOString().slice(0,10)}.csv`;a.click();
}
/* ── TREND STATE ── */
let trAsset='crypto';
let trCoins={'BTC-USD':'btc','ETH-USD':'eth','XRP-USD':'xrp'};
let trSelectedCoins=new Set(['BTC-USD','ETH-USD','XRP-USD']);
let trPeriodSel='1d';
function trPeriod(p){
  trPeriodSel=p;
  ['1d','7d','1m'].forEach(x=>{
    const b=document.getElementById('tr-p'+x); if(!b)return;
    b.className='at-btn'+(x===p?' on-purple':'');
  });
}


function trType(t){
  trAsset=t;
  const isC=t==='crypto';
  document.getElementById('tr-ac').className='at-btn'+(isC?' on-crypto':'');
  document.getElementById('tr-as').className='at-btn'+(!isC?' on-stock':'');
  document.getElementById('tr-c-row').style.display=isC?'':'none';
  document.getElementById('tr-s-row').style.display=isC?'none':'';
  document.getElementById('tr-s-mkt').style.display=isC?'none':'';
}
function trToggleCoin(tk,cls){
  if(trSelectedCoins.has(tk)) trSelectedCoins.delete(tk);
  else trSelectedCoins.add(tk);
  const b=document.getElementById('tr-'+cls);
  b.className='cb'+(trSelectedCoins.has(tk)?' sel-'+cls:'');
}
function doTrend(){
  const btn=document.querySelector('#tab-trend .btn');
  btn.disabled=true;
  hideAll();
  document.getElementById('trend-res').style.display='block';
  document.getElementById('trend-res').innerHTML=loading('추세 분석 중... (10~20초)');

  let tickers=[];
  if(trAsset==='crypto'){
    tickers=[...trSelectedCoins].map(tk=>({ticker:tk,is_crypto:true,cls:trCoins[tk]}));
  } else {
    const code=document.getElementById('tr-code').value.trim().padStart(6,'0');
    const mkt=document.getElementById('tr-mkt').value;
    tickers=[{ticker:code+'.'+mkt,is_crypto:false,cls:'stock'}];
  }
  if(!tickers.length){btn.disabled=false;document.getElementById('trend-res').innerHTML='<div class="empty">분석할 코인을 선택하세요</div>';return}

  // 순차 요청 (바이낸스 레이트리밋 방지 — 동시 요청 시 BTC/XRP 누락)
  const delay=ms=>new Promise(r=>setTimeout(r,ms));
  (async()=>{
    const all=[];
    for(const t of tickers){
      try{
        const r=await fetch('/trend',{method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({ticker:t.ticker,is_crypto:t.is_crypto,period:trPeriodSel})});
        const d=await r.json();
        all.push(d.error?{error:d.error,ticker:t.ticker,cls:t.cls}:{...d,cls:t.cls});
      }catch(e){
        all.push({error:'네트워크 오류: '+e,ticker:t.ticker,cls:t.cls});
      }
      if(tickers.length>1) await delay(400); // 400ms 간격으로 레이트리밋 방지
    }
    btn.disabled=false;
    renderTrend(all);
  })().catch(e=>{btn.disabled=false;document.getElementById('trend-res').innerHTML='<div class="empty">❌ '+e+'</div>';});
}

function renderTrend(all){
  const wrap=document.getElementById('trend-res');
  // tf_keys/tf_labels는 첫 번째 유효한 응답에서 가져옴 (forEach 바깥이므로 d 미정의 방지)
  const firstValid=all.find(x=>!x.error&&x.tf_keys)||{};
  const periodTfDefault={
    '1d':['1h','4h','1d'],'7d':['1h','4h','1d'],'1m':['4h','1d','1w']
  };
  const tfOrder=(firstValid.tf_keys)||periodTfDefault[trPeriodSel||'1d']||['1h','4h','1d'];
  const tfNamesAll={'15m':'15분','1h':'1시간','4h':'4시간','1d':'일봉','1w':'주봉'};
  const rawLabels=(firstValid.tf_labels)||{'1h':'1시간','4h':'4시간','1d':'일봉'};
  const tfNames=Object.fromEntries(tfOrder.map(k=>[k,rawLabels[k]||tfNamesAll[k]||k]));

  const periodLabel={'1d':'1일 (15분·1시간·4시간)','7d':'7일 (1시간·4시간·일봉)','1m':'1개월 (4시간·일봉·주봉)'};
  let html='<div class="trend-grid">';
  all.forEach((d,di)=>{
    if(d.error){html+=`<div class="asset-block"><div class="ab-head"><div class="ab-icon ${d.cls||'stock'}">?</div><div><div class="ab-name">${d.ticker}</div></div></div><div style="padding:16px;color:var(--muted);font-size:12px">❌ ${d.error}</div></div>`;return}

    const fmt=v=>d.is_crypto?(v>=1?'$'+v.toLocaleString(undefined,{maximumFractionDigits:2}):'$'+v.toFixed(5)):v.toLocaleString()+'원';
    const chg=d.chg24||0;
    const iconMap={'BTC-USD':'₿','ETH-USD':'Ξ','XRP-USD':'✕'};
    const icon=iconMap[d.ticker]||'📈';

    // summary bar
    let summaryHtml='<div class="trend-summary-bar">';
    tfOrder.forEach(tf=>{
      const r=d.results&&d.results[tf];
      if(!r){summaryHtml+=`<div class="tsb-item"><div class="tsb-tf">${tfNames[tf]}</div><div class="tsb-arrow">—</div><div class="tsb-txt side">N/A</div></div>`;return}
      const {arrow,cls2}=trendMeta(r);
      summaryHtml+=`<div class="tsb-item"><div class="tsb-tf">${tfNames[tf]}</div><div class="tsb-arrow">${arrow}</div><div class="tsb-txt ${cls2}">${r.trend_str}</div></div>`;
    });
    summaryHtml+='</div>';

    // detail rows
    let rowsHtml='<div class="tf-rows">';
    tfOrder.forEach(tf=>{
      const r=d.results&&d.results[tf];
      if(!r){rowsHtml+=`<div class="tf-row side"><div class="tf-lbl">${tfNames[tf]}</div><div class="tf-trend-txt">N/A</div></div>`;return}
      const {arrow,rowCls,rsiColor}=trendMeta(r);
      const str=r.strength||0;
      const dots=[-3,-2,-1,0,1,2,3].map(v=>{
        if(str>0&&v>0&&v<=str) return '<div class="sdot aup"></div>';
        if(str<0&&v<0&&v>=str) return '<div class="sdot adn"></div>';
        return '<div class="sdot"></div>';
      }).join('');
      const fmtE=v=>d.is_crypto?(v>=1?'$'+v.toFixed(0):'$'+v.toFixed(4)):v.toLocaleString();
      rowsHtml+=`<div class="tf-row ${rowCls}">
        <div class="tf-lbl" style="color:var(--muted)">${tfNames[tf]}</div>
        <div class="tf-arrow">${arrow}</div>
        <div class="tf-trend-txt">${r.trend_str}</div>
        <div class="strength-dots">${dots}</div>
        <div class="tf-meta">
          <div>${r.reason||''}</div>
          <div class="tf-emas">E20:${fmtE(r.ema20)} E50:${fmtE(r.ema50)}</div>
        </div>
        <div class="tf-rsi-wrap">
          <div class="tf-rsi-val" style="color:${rsiColor}">RSI ${r.rsi}</div>
          <div class="tf-rsi-bar"><div class="tf-rsi-fill" style="width:${r.rsi}%;background:${rsiColor}"></div></div>
        </div>
      </div>`;
    });
    rowsHtml+='</div>';

    const pLabel=periodLabel[d.period||'1d']||'';
    html+=`<div class="asset-block" style="animation-delay:${di*80}ms">
      <div class="ab-head">
        <div class="ab-icon ${d.cls}">${icon}</div>
        <div><div class="ab-name">${d.name}</div><div class="ab-ticker">${d.ticker}</div></div>
        <div style="margin-left:auto;text-align:right">
          <div class="ab-price">${fmt(d.price||0)}</div>
          <div class="ab-chg" style="color:${chg>=0?'var(--green)':'var(--red)'}">${chg>=0?'+':''}${chg}% (24H)</div>
        </div>
      </div>
      ${summaryHtml}
      ${rowsHtml}
    </div>`;
  });
  html+='</div>';
  wrap.innerHTML=html;
}

function trendMeta(r){
  const t=r.trend; const s=r.strength||0;
  let arrow='→', rowCls='side', rsiColor='var(--yellow)';
  if(t==='UP'){
    rowCls=s>=4?'up-strong':s>=2?'up':'up-weak';
    arrow=s>=4?'⬆':'↑'; rsiColor='var(--green)';
  } else if(t==='DOWN'){
    rowCls=s<=-4?'down-strong':s<=-2?'down':'down-weak';
    arrow=s<=-4?'⬇':'↓'; rsiColor='var(--red)';
  }
  const cls2=t==='UP'?'up':t==='DOWN'?'down':'side';
  return {arrow,rowCls,rsiColor,cls2};
}

/* ══════════════════════════════════════
   VWAP
══════════════════════════════════════ */
let vwAsset='crypto';
let vwSelected=new Set(['BTC-USD','ETH-USD','XRP-USD']);
const vwCoinMap={'BTC-USD':'btc','ETH-USD':'eth','XRP-USD':'xrp'};
let vwPeriodSel='1d';
function vwPeriod(p){
  vwPeriodSel=p;
  ['1d','7d','1m'].forEach(x=>{
    const b=document.getElementById('vw-p'+x); if(!b)return;
    b.className='at-btn'+(x===p?' on-blue':'');
  });
}


function vwType(t){
  vwAsset=t; const isC=t==='crypto';
  document.getElementById('vw-ac').className='at-btn'+(isC?' on-crypto':'');
  document.getElementById('vw-as').className='at-btn'+(!isC?' on-stock':'');
  document.getElementById('vw-c').style.display=isC?'':'none';
  document.getElementById('vw-s').style.display=isC?'none':'';
  document.getElementById('vw-sm').style.display=isC?'none':'';
}
function vwToggle(tk,cls){
  if(vwSelected.has(tk)) vwSelected.delete(tk);
  else vwSelected.add(tk);
  const b=document.getElementById('vw-'+cls);
  b.className='cb'+(vwSelected.has(tk)?' sel-'+cls:'');
}
function doVwap(){
  hideAll();
  document.getElementById('vwap-res').style.display='block';
  document.getElementById('vwap-res').innerHTML=loading('VWAP 분석 중...');
  let tickers=[];
  if(vwAsset==='crypto'){
    tickers=[...vwSelected].map(tk=>({ticker:tk,is_crypto:true,cls:vwCoinMap[tk]}));
  } else {
    const code=document.getElementById('vw-code').value.trim().padStart(6,'0');
    const mkt=document.getElementById('vw-mkt').value;
    tickers=[{ticker:code+'.'+mkt,is_crypto:false,cls:'stock'}];
  }
  if(!tickers.length){document.getElementById('vwap-res').innerHTML='<div class="empty">코인을 선택하세요</div>';return}
  // 순차 요청 (바이낸스 레이트리밋 방지)
  const vwDelay=ms=>new Promise(r=>setTimeout(r,ms));
  (async()=>{
    const all=[];
    for(const t of tickers){
      try{
        const r=await fetch('/vwap',{method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({ticker:t.ticker,is_crypto:t.is_crypto,period:vwPeriodSel})});
        const d=await r.json();
        all.push({...d,cls:t.cls});
      }catch(e){
        all.push({error:String(e),ticker:t.ticker,cls:t.cls});
      }
      if(tickers.length>1) await vwDelay(400);
    }
    renderVwap(all);
  })();
}

function renderVwap(all){
  const wrap=document.getElementById('vwap-res');
  const vwPeriodTfMap={
    '1d':  {names:{'5m':'5분','15m':'15분','1h':'1시간'},   order:['5m','15m','1h']},
    '7d':  {names:{'1h':'1시간','4h':'4시간'},               order:['1h','4h']},
    '1m':  {names:{'4h':'4시간','1d':'일봉'},                order:['4h','1d']},
  };
  const vwCfg=vwPeriodTfMap[vwPeriodSel||'1d']||vwPeriodTfMap['1d'];
  const tfNames=vwCfg.names;
  const iconMap={'BTC-USD':'₿','ETH-USD':'Ξ','XRP-USD':'✕'};

  const vwPeriodLabel={'1d':'1일 (5분·15분·1시간)','7d':'7일 (1시간·4시간)','1m':'1개월 (4시간·일봉)'};
  let html='<div class="vwap-grid">';
  all.forEach((d,di)=>{
    if(d.error){html+=`<div class="vcard"><div class="vc-head"><div class="vc-icon ${d.cls||'stock'}">?</div><div><div class="vc-name">${d.ticker}</div></div></div><div style="padding:14px;color:var(--muted);font-size:12px">❌ ${d.error}</div></div>`;return}
    const fmt=v=>d.is_crypto?(v>=1?'$'+v.toLocaleString(undefined,{maximumFractionDigits:2}):'$'+v.toFixed(5)):v.toLocaleString()+'원';
    const chg=d.chg24||0; const icon=iconMap[d.ticker]||'📈';
    const tfOrder=vwCfg.order;

    // summary bar
    let sumHtml='<div class="vwap-summary">';
    tfOrder.forEach(tf=>{
      const r=d.timeframes&&d.timeframes[tf];
      if(!r){sumHtml+=`<div class="vs-item"><div class="vs-tf">${tfNames[tf]}</div><div class="vs-sig">—</div><div class="vs-txt near">N/A</div></div>`;return}
      const {emoji,cls2,txt}=vwapMeta(r.position);
      sumHtml+=`<div class="vs-item"><div class="vs-tf">${tfNames[tf]}</div><div class="vs-sig">${emoji}</div><div class="vs-txt ${cls2}">${txt}</div></div>`;
    });
    sumHtml+='</div>';

    // detail rows
    let rowsHtml='';
    tfOrder.forEach(tf=>{
      const r=d.timeframes&&d.timeframes[tf];
      if(!r){rowsHtml+=`<div class="vwap-row"><div class="vr-head"><div class="vr-tf">${tfNames[tf]}</div><span class="vr-signal sig-near">N/A</span></div></div>`;return}
      const {sigCls,txt,emoji}=vwapMeta(r.position);
      const diffPct=r.diff_pct||0;
      const vwap=r.vwap||0; const price=r.price||0;
      const vbandU=r.vband_upper||vwap; const vbandL=r.vband_lower||vwap;
      const range=vbandU-vbandL||1;
      const pricePct=Math.min(100,Math.max(0,((price-vbandL)/range)*100));
      const vwapPct=50;

      rowsHtml+=`<div class="vwap-row">
        <div class="vr-head">
          <div class="vr-tf">${tfNames[tf]}</div>
          <span class="vr-signal ${sigCls}">${emoji} ${txt}</span>
        </div>
        <div class="vr-bars">
          <div class="vr-bar-row">
            <span class="vr-bar-label">현재가</span>
            <span class="vr-bar-val" style="color:var(--blue)">${fmt(price)}</span>
            <div class="vr-bar-track">
              <div class="vr-bar-fill" style="width:${pricePct}%;background:var(--blue)"></div>
              <div class="vr-bar-marker" style="left:${vwapPct}%"></div>
            </div>
          </div>
          <div class="vr-bar-row">
            <span class="vr-bar-label">VWAP</span>
            <span class="vr-bar-val" style="color:#06b6d4">${fmt(vwap)}</span>
            <div class="vr-bar-track">
              <div class="vr-bar-fill" style="width:50%;background:#06b6d4"></div>
            </div>
          </div>
          <div class="vr-bar-row">
            <span class="vr-bar-label">VWAP +1σ</span>
            <span class="vr-bar-val" style="color:rgba(0,255,136,.6)">${fmt(vbandU)}</span>
          </div>
          <div class="vr-bar-row">
            <span class="vr-bar-label">VWAP -1σ</span>
            <span class="vr-bar-val" style="color:rgba(255,71,87,.6)">${fmt(vbandL)}</span>
          </div>
        </div>
        <div class="vr-meta">
          <div class="vm-box">
            <div class="vm-lbl">VWAP 이격</div>
            <div class="vm-val ${diffPct>0?'g':'r'}">${diffPct>0?'+':''}${diffPct.toFixed(2)}%</div>
          </div>
          <div class="vm-box">
            <div class="vm-lbl">매매 신호</div>
            <div class="vm-val ${r.position==='ABOVE'?'g':r.position==='BELOW'?'r':'y'}">${txt}</div>
          </div>
          <div class="vm-box">
            <div class="vm-lbl">거래량 추세</div>
            <div class="vm-val c">${r.vol_trend||'—'}</div>
          </div>
          <div class="vm-box">
            <div class="vm-lbl">VWAP 기울기</div>
            <div class="vm-val ${r.slope_dir==='UP'?'g':r.slope_dir==='DOWN'?'r':'y'}">${r.slope_dir==='UP'?'↑ 상승':r.slope_dir==='DOWN'?'↓ 하락':'→ 횡보'}</div>
          </div>
        </div>
      </div>`;
    });

    html+=`<div class="vcard" style="animation-delay:${di*80}ms">
      <div class="vc-head">
        <div class="vc-icon ${d.cls}">${icon}</div>
        <div><div class="vc-name">${d.name}</div><div class="vc-sub">${d.ticker}${d.is_crypto?' · Binance':''}</div></div>
        <div class="vc-price-wrap">
          <div class="vc-price">${fmt(d.price||0)}</div>
          <div class="vc-chg" style="color:${chg>=0?'var(--green)':'var(--red)'}">${chg>=0?'+':''}${chg}%</div>
        </div>
      </div>
      <div class="vc-body">
        ${sumHtml}
        ${rowsHtml}
        <div style="font-size:10px;color:var(--muted2);margin-top:4px;padding:0 2px">
          📌 VWAP 위 = 매수 우위 &nbsp;|&nbsp; VWAP 아래 = 매도 우위 &nbsp;|&nbsp; ±0.3% 이내 = 중립 (진입 구간)
        </div>
      </div>
    </div>`;
  });
  html+='</div>';
  wrap.innerHTML=html;
}

function vwapMeta(pos){
  if(pos==='ABOVE') return {emoji:'🟢',cls2:'above',txt:'매수 우위',sigCls:'sig-above'};
  if(pos==='BELOW') return {emoji:'🔴',cls2:'below',txt:'매도 우위',sigCls:'sig-below'};
  return {emoji:'🟡',cls2:'near',txt:'중립 진입구간',sigCls:'sig-near'};
}
/* ══════════════════════════════════════
   COMBO — 종합 타점 분석
══════════════════════════════════════ */
let cbAsset='crypto', cbTicker='BTC-USD', cbCls2='btc';

function cbType(t){
  cbAsset=t; const isC=t==='crypto';
  document.getElementById('cb-ac').className='at-btn'+(isC?' on-crypto':'');
  document.getElementById('cb-as').className='at-btn'+(!isC?' on-stock':'');
  document.getElementById('cb-c').style.display=isC?'':'none';
  document.getElementById('cb-s').style.display=isC?'none':'';
  document.getElementById('cb-sm').style.display=isC?'none':'';
}
function cbCoin(tk,cls){
  cbTicker=tk; cbCls2=cls;
  ['btc','eth','xrp'].forEach(c=>{
    const b=document.getElementById('cb-'+c); if(!b)return;
    b.className='cb'+(c===cls?' sel-'+c:'');
  });
}
function doCombo(){
  let ticker, isCrypto;
  const acct=parseFloat(document.getElementById('cb-acct').value)||10000000;
  const risk=parseFloat(document.getElementById('cb-risk').value)||1;
  if(cbAsset==='crypto'){
    ticker=cbTicker; isCrypto=true;
  } else {
    const code=document.getElementById('cb-code').value.trim().padStart(6,'0');
    const mkt=document.getElementById('cb-mkt').value;
    ticker=code+'.'+mkt; isCrypto=false;
  }
  hideAll();
  document.getElementById('combo-res').style.display='block';
  document.getElementById('combo-res').innerHTML=loading('FVG + 추세 + VWAP 종합 분석 중...<br><span style="font-size:11px;color:var(--muted2)">(20~40초 소요)</span>');
  fetch('/combo',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({ticker,is_crypto:isCrypto,account:acct,risk_pct:risk})})
    .then(r=>r.json()).then(d=>renderCombo(d))
    .catch(e=>{document.getElementById('combo-res').innerHTML=`<div class="empty">❌ ${e}</div>`});
}

function renderCombo(d){
  const wrap=document.getElementById('combo-res');
  if(d.error){wrap.innerHTML=`<div class="empty">❌ ${d.error}</div>`;return}

  const fmt=v=>d.is_crypto?(v>=1?'$'+v.toLocaleString(undefined,{maximumFractionDigits:2}):'$'+v.toFixed(5)):v.toLocaleString()+'원';
  const fmtAmt=v=>d.is_crypto?'$'+v.toLocaleString(undefined,{maximumFractionDigits:2}):Math.round(v/10000).toLocaleString()+'만원';
  const iconMap={'BTC-USD':'₿','ETH-USD':'Ξ','XRP-USD':'✕'};
  const icon=iconMap[d.ticker]||'📈';
  const chg=d.change||0; const score=d.score||0; const maxScore=9;
  const scorePct=Math.round((score+maxScore)/(maxScore*2)*100);

  // verdict
  const {vClass,vIcon,vTitle,vSub,vSide}=verdictInfo(score,d);

  // score circle color
  const scColor=score>=3?'#00ff88':score<=-3?'#ff4757':score>0?'#4ade80':score<0?'#f87171':'#ffa502';

  // confidence bar
  const confPct=Math.round(Math.abs(score)/maxScore*100);
  const confColor=confPct>=60?'#00ff88':confPct>=35?'#ffa502':'#ff4757';

  // signal cards
  const fvg=d.signals?.fvg||{}; const trend=d.signals?.trend||{}; const vwap=d.signals?.vwap||{};

  const fvgCls=fvg.score>0?'bull':fvg.score<0?'bear':'neut';
  const trendCls=trend.score>0?'bull':trend.score<0?'bear':'neut';
  const vwapCls=vwap.score>0?'bull':vwap.score<0?'bear':'neut';

  // entry box
  const entry=d.entry||{};
  const hasEntry=entry.entry_price>0;

  let html=`
  <div class="combo-hero">
    <div class="ch-icon ${d.cls||'stock'}">${icon}</div>
    <div>
      <div class="ch-name">${d.name||d.ticker}</div>
      <div class="ch-sub">${d.ticker}${d.is_crypto?' · Binance 4H 기준':' · 4시간봉 기준'}</div>
    </div>
    <div style="margin-left:auto;text-align:right">
      <div class="ch-price">${fmt(d.price||0)}</div>
      <div class="ch-chg" style="color:${chg>=0?'var(--green)':'var(--red)'}">${chg>=0?'+':''}${chg.toFixed(2)}%</div>
    </div>
    <div class="score-circle" style="background:${scColor}22;border:2px solid ${scColor}">
      <div class="sc-num" style="color:${scColor}">${score>0?'+':''}${score}</div>
      <div class="sc-max" style="color:${scColor}88">/ ${maxScore}</div>
    </div>
  </div>

  <div class="verdict ${vClass}">
    <div class="verdict-icon">${vIcon}</div>
    <div class="verdict-txt">
      <div class="verdict-title">${vTitle}</div>
      <div class="verdict-sub">${vSub}</div>
    </div>
    <div class="verdict-side">${vSide}</div>
  </div>

  <div class="signal-grid">
    <div class="sig-card" style="animation-delay:50ms">
      <div class="sig-card-title"><div class="sig-score-dot ${fvgCls}"></div> ⚡ FVG (4시간)</div>
      <div class="sig-card-sig ${fvgCls}">${fvg.label||'N/A'}</div>
      <div class="sig-card-detail">${fvg.detail||'—'}</div>
    </div>
    <div class="sig-card" style="animation-delay:100ms">
      <div class="sig-card-title"><div class="sig-score-dot ${trendCls}"></div> 📡 추세 (EMA/RSI)</div>
      <div class="sig-card-sig ${trendCls}">${trend.label||'N/A'}</div>
      <div class="sig-card-detail">${trend.detail||'—'}</div>
    </div>
    <div class="sig-card" style="animation-delay:150ms">
      <div class="sig-card-title"><div class="sig-score-dot ${vwapCls}"></div> 📊 VWAP (1시간)</div>
      <div class="sig-card-sig ${vwapCls}">${vwap.label||'N/A'}</div>
      <div class="sig-card-detail">${vwap.detail||'—'}</div>
    </div>
  </div>

  <div class="conf-bar">
    <div class="cb-label"><span>종합 신뢰도</span><span style="color:${confColor};font-family:'JetBrains Mono'">${confPct}%</span></div>
    <div class="cb-track"><div class="cb-fill" style="width:${confPct}%;background:${confColor}"></div></div>
    <div class="cond-list">
      ${(d.conditions||[]).map(c=>`<div class="cl-item"><span>${c.ok?'✅':'❌'}</span><span class="${c.ok?'cl-ok':'cl-no'}">${c.text}</span></div>`).join('')}
    </div>
  </div>`;

  if(hasEntry){
    const isBull=entry.direction==='LONG';
    html+=`<div class="entry-box" style="margin-top:14px">
      <div class="eb-head">
        <div class="eb-tf">4H 진입 타점</div>
        <div class="eb-label">4시간봉 기준 FVG 50% 진입</div>
        <span class="eb-dir ${isBull?'long':entry.direction==='SHORT'?'short':'wait'}">${isBull?'▲ LONG':entry.direction==='SHORT'?'▼ SHORT':'⏳ 대기'}</span>
      </div>
      <div class="eb-body">
        <div class="ep-item"><div class="ep-lbl">📍 현재가</div><div class="ep-val cur">${fmt(entry.current||0)}</div></div>
        <div class="ep-item"><div class="ep-lbl">🎯 진입가 (FVG 50%)</div><div class="ep-val entry">${fmt(entry.entry_price||0)}</div><div class="ep-pct ${entry.entry_diff>=0?'pos':'neg'}">${entry.entry_diff>=0?'+':''}${(entry.entry_diff||0).toFixed(2)}% 현재가 대비</div></div>
        <div class="ep-item"><div class="ep-lbl">🛑 손절가</div><div class="ep-val sl">${fmt(entry.sl||0)}</div><div class="ep-pct neg">-${(entry.sl_pct||0).toFixed(2)}%</div></div>
        <div class="ep-item"><div class="ep-lbl">✅ 1차 목표 (1:2)</div><div class="ep-val tp1">${fmt(entry.tp1||0)}</div><div class="ep-pct pos">+${(entry.tp1_pct||0).toFixed(2)}%</div></div>
        <div class="ep-item"><div class="ep-lbl">🚀 2차 목표 (1:3)</div><div class="ep-val tp2">${fmt(entry.tp2||0)}</div><div class="ep-pct pos">+${(entry.tp2_pct||0).toFixed(2)}%</div></div>
        <div class="ep-item"><div class="ep-lbl">💎 3차 목표 (1:5)</div><div class="ep-val tp3">${fmt(entry.tp3||0)}</div><div class="ep-pct pos">+${(entry.tp3_pct||0).toFixed(2)}%</div></div>
      </div>
      <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px;padding:0 16px 14px">
        <div class="ep-item"><div class="ep-lbl">수량</div><div class="ep-val entry">${d.is_crypto?(entry.shares||0).toFixed(4)+' '+d.ticker.split('-')[0]:(entry.shares||0).toLocaleString()+'주'}</div></div>
        <div class="ep-item"><div class="ep-lbl">투자금액</div><div class="ep-val" style="color:var(--blue)">${fmtAmt(entry.invest_amt||0)}</div></div>
        <div class="ep-item"><div class="ep-lbl">최대 손실</div><div class="ep-val sl">${fmtAmt(entry.risk_amt||0)}</div></div>
        <div class="ep-item"><div class="ep-lbl">R:R 비율</div><div class="ep-val tp1">1 : ${(entry.rr||2).toFixed(1)}</div></div>
      </div>
    </div>`;
  } else {
    html+=`<div style="margin-top:14px;padding:16px;background:rgba(255,165,2,.06);border:1px solid rgba(255,165,2,.2);border-radius:10px;font-size:13px;color:var(--yellow);text-align:center">
      ⏳ 현재 4시간봉 기준 유효한 FVG가 없습니다.<br>
      <span style="color:var(--muted);font-size:11px;margin-top:4px;display:block">가격이 FVG 구간으로 진입하면 타점이 나타납니다.</span>
    </div>`;
  }

  html+=`<div style="margin-top:12px;padding:11px 14px;background:rgba(255,165,2,.05);border:1px solid rgba(255,165,2,.15);border-radius:8px;font-size:11px;color:var(--muted);line-height:1.8">
    ⚠️ <strong style="color:var(--yellow)">주의:</strong> 종합 분석 자동 계산값입니다. 실제 진입 전 반응 캔들·거래량을 반드시 확인하세요. 모든 투자 손익은 <strong style="color:var(--txt)">본인 책임</strong>입니다.
  </div>`;

  wrap.innerHTML=html;
}

function verdictInfo(score, d){
  if(score>=6) return {vClass:'strong-long',vIcon:'🚀',vTitle:'강한 롱 진입 신호',vSub:'FVG·추세·VWAP 모두 상승 정렬 — 적극 진입 고려',vSide:'STRONG\nLONG'};
  if(score>=3) return {vClass:'long',vIcon:'📈',vTitle:'롱 진입 신호',vSub:'대부분의 조건이 상승 방향 — 리테스트 후 진입',vSide:'LONG'};
  if(score>=1) return {vClass:'weak-long',vIcon:'↗️',vTitle:'약한 롱 신호',vSub:'일부 조건 충족 — 추가 확인 후 진입',vSide:'WEAK\nLONG'};
  if(score<=-6) return {vClass:'strong-short',vIcon:'🩸',vTitle:'강한 숏 진입 신호',vSub:'FVG·추세·VWAP 모두 하락 정렬 — 적극 숏 고려',vSide:'STRONG\nSHORT'};
  if(score<=-3) return {vClass:'short',vIcon:'📉',vTitle:'숏 진입 신호',vSub:'대부분의 조건이 하락 방향 — 리테스트 후 숏',vSide:'SHORT'};
  if(score<=-1) return {vClass:'weak-short',vIcon:'↘️',vTitle:'약한 숏 신호',vSub:'일부 조건 하락 — 추가 확인 후 숏',vSide:'WEAK\nSHORT'};
  return {vClass:'neutral',vIcon:'⚖️',vTitle:'중립 — 관망',vSub:'신호가 혼재 — 명확한 방향 형성까지 대기',vSide:'WAIT'};
}
/* ══════════════════════════════════════
   BACKTEST
══════════════════════════════════════ */
let btTicker='BTC-USD', btCls3='btc';
let btAllTrades=[], btFilter='all';
let btStrat='fvg';

const btHints={
  combo:'4시간봉 · <strong style="color:var(--txt)">FVG + 추세(EMA/RSI) + VWAP</strong> 종합 신호 — 모든 지표 정렬 시 진입 · 점수 ≥ 설정값',
  fvg:  '4시간봉 · <strong style="color:var(--txt)">FVG 단독</strong> 전략 — 불리시/베어리시 FVG 감지 → FVG 50% 진입 · SL: FVG 엣지 · TP: 1:2/1:3',
  vwap: '4시간봉 · <strong style="color:var(--txt)">VWAP 단독</strong> 전략 — 가격이 VWAP에서 이격 후 돌아올 때 진입 · VWAP 크로스 기반',
  emafvg:'4시간봉 · <strong style="color:var(--txt)">EMA + FVG</strong> 전략 — EMA20/50 상승정렬 + 불리시FVG 일치 시 진입 (스마트머니 개념)',
  krma: '4시간봉 · <strong style="color:var(--txt)">거래량 + MA</strong> 전략 — MA20 위 + MA5>MA20 + 거래량 1.5배+ 조건 충족 시 진입 (한국주식 특화)',
};

function btStrategy(s){
  btStrat=s;
  ['combo','fvg','vwap','emafvg','krma'].forEach(x=>{
    const b=document.getElementById('bts-'+x); if(!b)return;
    b.className='bts-btn'+(x===s?' sel':'');
  });
  document.getElementById('bt-score-wrap').style.display=s==='vwap'?'none':'';
  document.getElementById('bt-vwap-thresh-wrap').style.display=s==='vwap'?'':'none';
  document.getElementById('bt-hint').innerHTML=btHints[s]||btHints['fvg'];

  // KR MA 전략 = 한국 주식, 나머지 = 코인
  const isKR = s==='krma';
  document.getElementById('bt-coin-row').style.display  = isKR?'none':'flex';
  document.getElementById('bt-stock-row').style.display = isKR?'flex':'none';
  const lbl=document.getElementById('bt-acct-lbl');
  if(lbl) lbl.textContent = isKR?'투자금액 (원)':'투자금액 (USD)';
  const acctEl=document.getElementById('bt-acct');
  if(acctEl){
    if(isKR){acctEl.value='10000000';acctEl.step='1000000';}
    else{acctEl.value='10000';acctEl.step='1000';}
  }
}

function btCoin(tk,cls){
  btTicker=tk; btCls3=cls;
  ['btc','eth','xrp'].forEach(c=>{
    const b=document.getElementById('bt-'+c); if(!b)return;
    b.className='at-btn'+(c===cls?' on-crypto':'');
  });
}

function doBacktest(){
  const period=parseInt(document.getElementById('bt-period').value)||3;
  const minScore=parseInt(document.getElementById('bt-minscore').value)||3;
  const vwapThresh=parseFloat(document.getElementById('bt-vwap-thresh').value)||0.3;
  const acct=parseFloat(document.getElementById('bt-acct').value)||10000;
  const risk=parseFloat(document.getElementById('bt-risk').value)||1;
  const btn=document.getElementById('btn-backtest'); btn.disabled=true;

  const stratLabels={combo:'FVG+추세+VWAP 종합',fvg:'FVG 단독',vwap:'VWAP 단독',emafvg:'EMA+FVG',krma:'거래량+MA'};
  const stratColors={combo:'#f43f5e',fvg:'#58a6ff',vwap:'#06b6d4',emafvg:'#34d399',krma:'#fb923c'};
  const sc=stratColors[btStrat]||'#e879f9';

  hideAll();
  document.getElementById('backtest-res').style.display='block';
  document.getElementById('backtest-res').innerHTML=`
    <div class="bt-progress">
      <div class="spin" style="border-top-color:${sc}"></div><br>
      <span style="color:var(--muted);font-size:13px">
        ${btTicker} · ${stratLabels[btStrat]} 백테스트 실행 중...<br>
        <span style="font-size:11px;color:var(--muted2)">최근 ${period}개월 · 4시간봉 · (30~60초)</span>
      </span>
      <div class="bt-prog-bar"><div class="bt-prog-fill" id="bt-pbar" style="width:5%;background:linear-gradient(90deg,${sc},#f43f5e)"></div></div>
      <div id="bt-prog-txt" style="font-size:11px;color:var(--muted);font-family:'JetBrains Mono'">데이터 수집 중...</div>
    </div>`;

  let p=5;
  const msgs={
    fvg:  ['바이낸스 캔들 수집...','FVG 패턴 스캔...','불리시/베어리시 감지...','진입 시뮬레이션...','손익 계산...'],
    vwap: ['바이낸스 캔들 수집...','VWAP 계산 중...','이격 분석...','진입 시뮬레이션...','손익 계산...'],
    combo:['바이낸스 캔들 수집...','FVG 스캔...','추세 분석...','VWAP 계산...','종합 시뮬레이션...'],
  };
  const mArr=msgs[btStrat]||msgs.combo;
  const iv=setInterval(()=>{
    p=Math.min(p+Math.random()*4,90);
    const el=document.getElementById('bt-pbar'); if(el)el.style.width=p+'%';
    const t=document.getElementById('bt-prog-txt'); if(t)t.textContent=mArr[Math.floor(p/20)]||'처리 중...';
    if(p>=90)clearInterval(iv);
  },800);

  const isKrma=(btStrat==='krma');
  const krCode=isKrma?document.getElementById('bt-kr-code').value.trim().padStart(6,'0'):'';
  const krMkt=isKrma?(document.getElementById('bt-kr-mkt').value||'KS'):'KS';
  const finalTicker=isKrma?(krCode+'.'+krMkt):btTicker;
  const loadingTicker=isKrma?(krCode||'KR종목'):btTicker;

  document.getElementById('backtest-res').innerHTML=document.getElementById('backtest-res').innerHTML.replace(btTicker,loadingTicker);

  fetch('/backtest',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({
      ticker:finalTicker, months:period, strategy:btStrat,
      is_kr_stock:isKrma, kr_market:krMkt,
      min_score:minScore, vwap_thresh:vwapThresh,
      account:acct, risk_pct:risk
    })})
    .then(r=>r.json())
    .then(d=>{clearInterval(iv);btn.disabled=false;renderBacktest(d,sc);})
    .catch(e=>{clearInterval(iv);btn.disabled=false;
      document.getElementById('backtest-res').innerHTML=`<div class="empty">❌ ${e}</div>`;});
}

function renderBacktest(d, sc='#e879f9'){
  if(d.error){document.getElementById('backtest-res').innerHTML=`<div class="empty">❌ ${d.error}</div>`;return}
  btAllTrades=d.trades||[];
  const s=d.stats||{};
  const isKR=d.is_kr_stock||false;
  const cur=isKR?'원':'$';
  const fmtMoney=v=>isKR?Math.round(v/10000).toLocaleString()+'만원':(cur+Math.abs(v).toLocaleString(undefined,{maximumFractionDigits:2}));
  const stratLabels={combo:'FVG+추세+VWAP',fvg:'FVG 단독',vwap:'VWAP 단독',emafvg:'EMA+FVG',krma:'거래량+MA (한국주식 일봉)'};
  const stratLabel=stratLabels[d.strategy]||d.strategy;
  const tf=isKR?'일봉':'4시간봉';

  const winRate=s.win_rate||0;
  const wrColor=winRate>=55?'var(--green)':winRate>=45?'var(--yellow)':'var(--red)';
  const pnlTotal=s.total_pnl_pct||0;
  const pnlColor=pnlTotal>=0?'var(--green)':'var(--red)';
  const eqSvg=drawEquityCurve(d.equity_curve||[], s.initial_capital||(isKR?10000000:10000), sc);

  let html=`
  <div style="display:flex;align-items:center;gap:12px;margin-bottom:14px;padding:14px 18px;background:var(--s2);border:1px solid ${sc}44;border-radius:12px;flex-wrap:wrap">
    <div style="font-family:'Bebas Neue';font-size:30px;color:${sc}">${d.ticker} · ${stratLabel}</div>
    <div><div style="font-size:11px;color:var(--muted)">최근 ${d.months}개월 · ${tf} · ${d.period_start} ~ ${d.period_end}</div>
    ${isKR?'<div style="font-size:10px;color:#fb923c;margin-top:2px">🇰🇷 한국주식 일봉 · 원화(KRW) 기준</div>':''}</div>
    <div style="margin-left:auto;font-size:11px;color:var(--muted);font-family:'JetBrains Mono'">${d.strategy==='vwap'?'이격 임계: '+d.vwap_thresh+'%':'점수 ≥ ±'+d.min_score}</div>
  </div>

  <div class="bt-summary">
    <div class="bt-box"><div class="bt-lbl">총 거래수</div><div class="bt-val w">${s.total_trades||0}</div><div class="bt-sub">신호 발생 횟수</div></div>
    <div class="bt-box"><div class="bt-lbl">승률</div><div class="bt-val" style="color:${wrColor}">${winRate.toFixed(1)}%</div><div class="bt-sub">${s.wins||0}승 ${s.losses||0}패 ${s.timeouts||0}시간초과</div></div>
    <div class="bt-box"><div class="bt-lbl">총 손익</div><div class="bt-val" style="color:${pnlColor}">${pnlTotal>=0?'+':''}${pnlTotal.toFixed(2)}%</div><div class="bt-sub">${fmtMoney(s.total_pnl_usd||0)}</div></div>
    <div class="bt-box"><div class="bt-lbl">평균 수익</div><div class="bt-val g">+${(s.avg_win_pct||0).toFixed(2)}%</div><div class="bt-sub">승리 거래 평균</div></div>
    <div class="bt-box"><div class="bt-lbl">평균 손실</div><div class="bt-val r">${(s.avg_loss_pct||0).toFixed(2)}%</div><div class="bt-sub">패배 거래 평균</div></div>
    <div class="bt-box"><div class="bt-lbl">손익비(PF)</div><div class="bt-val y">${(s.profit_factor||0).toFixed(2)}</div><div class="bt-sub">총수익/총손실</div></div>
    <div class="bt-box"><div class="bt-lbl">최대 낙폭</div><div class="bt-val r">-${(s.max_drawdown_pct||0).toFixed(2)}%</div><div class="bt-sub">Max Drawdown</div></div>
    <div class="bt-box"><div class="bt-lbl">기대값(R)</div><div class="bt-val p">${(s.expectancy||0).toFixed(3)}</div><div class="bt-sub">거래당 기대 R</div></div>
  </div>

  <div class="equity-wrap">
    <div class="equity-title">📈 자본 곡선 · 초기: ${fmtMoney(s.initial_capital||0)} → 최종: ${fmtMoney(s.final_capital||0)}</div>
    ${eqSvg}
  </div>

  <div class="trade-list">
    <div class="bt-filter-row">
      <span class="bt-filter-lbl">필터:</span>
      <button class="bt-filter-btn active" onclick="btSetFilter('all',this)">전체 (${btAllTrades.length})</button>
      <button class="bt-filter-btn" onclick="btSetFilter('win',this)">🟢 승리 (${s.wins||0})</button>
      <button class="bt-filter-btn" onclick="btSetFilter('loss',this)">🔴 패배 (${s.losses||0})</button>
      <button class="bt-filter-btn" onclick="btSetFilter('long',this)">▲ 롱</button>
      <button class="bt-filter-btn" onclick="btSetFilter('short',this)">▼ 숏</button>
      <button class="bt-filter-btn" onclick="btSetFilter('tp1',this)">TP1</button>
      <button class="bt-filter-btn" onclick="btSetFilter('tp2',this)">TP2</button>
      <button class="bt-filter-btn" onclick="btSetFilter('sl',this)">SL</button>
    </div>
    <div class="tl-head">
      <span>#</span><span>방향</span><span>진입가</span><span>청산가</span><span>손익($)</span><span>손익%</span><span>결과</span><span>신호</span>
    </div>
    <div id="bt-trade-list">${renderTradeRows(btAllTrades.slice(0,50),d.strategy,isKR)}</div>
    ${btAllTrades.length>50?`<div style="text-align:center;padding:10px;font-size:11px;color:var(--muted)">처음 50개 표시 (총 ${btAllTrades.length}개)</div>`:''}
  </div>

  <div style="margin-top:10px;padding:11px 14px;background:rgba(232,121,249,.05);border:1px solid rgba(232,121,249,.2);border-radius:8px;font-size:11px;color:var(--muted);line-height:1.8">
    ⚠️ 백테스트는 과거 성과이며 미래 수익을 보장하지 않습니다. 슬리피지·거래비용 미반영. 참고용으로만 사용하세요.
  </div>`;

  document.getElementById('backtest-res').innerHTML=html;
}

function btSetFilter(f,btn){
  btFilter=f;
  document.querySelectorAll('.bt-filter-btn').forEach(b=>b.classList.remove('active'));
  if(btn) btn.classList.add('active');
  let filtered=btAllTrades;
  if(f==='win')   filtered=btAllTrades.filter(t=>t.pnl_pct>0);
  if(f==='loss')  filtered=btAllTrades.filter(t=>t.pnl_pct<=0);
  if(f==='long')  filtered=btAllTrades.filter(t=>t.direction==='LONG');
  if(f==='short') filtered=btAllTrades.filter(t=>t.direction==='SHORT');
  if(f==='tp1')   filtered=btAllTrades.filter(t=>t.outcome==='tp1');
  if(f==='tp2')   filtered=btAllTrades.filter(t=>t.outcome==='tp2');
  if(f==='sl')    filtered=btAllTrades.filter(t=>t.outcome==='sl');
  document.getElementById('bt-trade-list').innerHTML=renderTradeRows(filtered.slice(0,50),btStrat,btAllTrades[0]&&btAllTrades[0].is_kr);
}

function renderTradeRows(trades, strategy='fvg'){
  if(!trades.length) return '<div class="empty" style="padding:20px">해당 거래 없음</div>';
  const outMap={tp1:'TP1',tp2:'TP2',sl:'SL',timeout:'시간초과',cross:'크로스'};
  const outCls={tp1:'tp1',tp2:'tp2',sl:'sl',timeout:'to',cross:'tp1'};
  return trades.map((t,i)=>{
    const rowCls=t.outcome==='timeout'?'timeout':t.pnl_pct>0?'win':'loss';
    const dirCls=t.direction==='LONG'?'tl-long':'tl-short';
    const fmt=v=>v>=1?'$'+v.toLocaleString(undefined,{maximumFractionDigits:2}):'$'+v.toFixed(4);
    // 신호 표시: strategy별
    const sigTxt=strategy==='fvg'?(t.fvg_sig||'FVG'):strategy==='vwap'?`${(t.diff_pct||0).toFixed(2)}%`:`${t.score>=0?'+':''}${t.score||0}`;
    const sigColor=strategy==='vwap'?(t.diff_pct>0?'var(--green)':'var(--red)'):(t.score>=3?'var(--green)':t.score<=-3?'var(--red)':'var(--yellow)');
    return `<div class="tl-row ${rowCls}" style="animation-delay:${Math.min(i,20)*15}ms">
      <span class="tl-num">${t.idx||i+1}</span>
      <span class="tl-dir ${dirCls}">${t.direction}</span>
      <span class="tl-price">${fmt(t.entry)}</span>
      <span class="tl-price">${fmt(t.exit)}</span>
      <span class="tl-pnl ${t.pnl_usd>=0?'pos':'neg'}">${t.pnl_usd>=0?'+':''}$${t.pnl_usd.toFixed(2)}</span>
      <span class="tl-pnl ${t.pnl_pct>=0?'pos':'neg'}">${t.pnl_pct>=0?'+':''}${t.pnl_pct.toFixed(2)}%</span>
      <span class="tl-outcome ${outCls[t.outcome]||'to'}">${outMap[t.outcome]||t.outcome}</span>
      <span class="tl-score" style="color:${sigColor}">${sigTxt}</span>
    </div>`;
  }).join('');
}

function drawEquityCurve(curve, initial, lineColor='#e879f9'){
  if(!curve||curve.length<2) return '<div style="text-align:center;padding:30px;color:var(--muted)">데이터 부족</div>';
  const W=800, H=160, pad=30;
  const vals=curve.map(p=>p.equity);
  const minV=Math.min(...vals); const maxV=Math.max(...vals); const range=maxV-minV||1;
  const scaleX=i=>(i/(curve.length-1))*(W-pad*2)+pad;
  const scaleY=v=>H-pad-((v-minV)/range)*(H-pad*2);
  const ptArr=curve.map((p,i)=>({x:scaleX(i),y:scaleY(p.equity)}));
  const linePath='M'+ptArr.map(p=>`${p.x.toFixed(1)},${p.y.toFixed(1)}`).join('L');
  const baseY=scaleY(initial);
  const areaPath=`M${ptArr[0].x},${baseY}L`+ptArr.map(p=>`${p.x.toFixed(1)},${p.y.toFixed(1)}`).join('L')+`L${ptArr[ptArr.length-1].x},${baseY}Z`;
  const lastVal=vals[vals.length-1]; const fillAlpha=lastVal>=initial?'22':'22';
  const fillC=lastVal>=initial?lineColor:lineColor;
  const fmtV=v=>'$'+Math.round(v).toLocaleString();
  // draw trade markers
  const markers=(curve.filter(p=>p.outcome)||[]).map(p=>{
    const xi=curve.indexOf(p); const x=scaleX(xi); const y=scaleY(p.equity);
    const c=p.outcome==='sl'?'#ff4757':p.outcome==='timeout'?'#7d8590':'#00ff88';
    return `<circle cx="${x}" cy="${y}" r="3" fill="${c}" opacity=".8"/>`;
  }).join('');
  return `<svg class="equity-svg" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" xmlns="http://www.w3.org/2000/svg">
    <rect width="${W}" height="${H}" fill="#0d1117" rx="6"/>
    <line x1="${pad}" y1="${baseY.toFixed(1)}" x2="${W-pad}" y2="${baseY.toFixed(1)}" stroke="rgba(255,255,255,.12)" stroke-width="1" stroke-dasharray="4,3"/>
    <path d="${areaPath}" fill="${fillC}${fillAlpha}"/>
    <path d="${linePath}" fill="none" stroke="${lineColor}" stroke-width="2.5"/>
    ${markers}
    <circle cx="${ptArr[ptArr.length-1].x}" cy="${ptArr[ptArr.length-1].y}" r="5" fill="${lineColor}"/>
    <text x="${pad+4}" y="${H-10}" fill="rgba(255,255,255,.35)" font-size="10" font-family="monospace">${fmtV(minV)}</text>
    <text x="${pad+4}" y="${pad+6}" fill="rgba(255,255,255,.35)" font-size="10" font-family="monospace">${fmtV(maxV)}</text>
    <text x="${W-pad-5}" y="${ptArr[ptArr.length-1].y<20?ptArr[ptArr.length-1].y+14:ptArr[ptArr.length-1].y-6}" fill="${lineColor}" font-size="11" font-family="monospace" font-weight="bold" text-anchor="end">${fmtV(lastVal)}</text>
  </svg>`;
}

/* ══════════════════════════════════════
   EMA + FVG 정밀 타점
══════════════════════════════════════ */
let efAsset='crypto', efTicker='BTC-USD', efCls4='btc';

function efType(t){
  efAsset=t; const isC=t==='crypto';
  document.getElementById('ef-ac').className='at-btn'+(isC?' on-crypto':'');
  document.getElementById('ef-as').className='at-btn'+(!isC?' on-stock':'');
  document.getElementById('ef-c').style.display=isC?'':'none';
  document.getElementById('ef-s').style.display=isC?'none':'';
  document.getElementById('ef-sm').style.display=isC?'none':'';
  document.getElementById('ef-unit').textContent=isC?'USD':'원';
  document.getElementById('ef-acct').value=isC?'10000':'10000000';
}
function efCoin(tk,cls){
  efTicker=tk; efCls4=cls;
  ['btc','eth','xrp'].forEach(c=>{
    const b=document.getElementById('ef-'+c); if(!b)return;
    b.className='cb'+(c===cls?' sel-'+c:'');
  });
}
function doEmaFvg(){
  let ticker, isCrypto;
  const acct=parseFloat(document.getElementById('ef-acct').value)||10000;
  const risk=parseFloat(document.getElementById('ef-risk').value)||1;
  if(efAsset==='crypto'){
    ticker=efTicker; isCrypto=true;
  } else {
    const code=document.getElementById('ef-code').value.trim().padStart(6,'0');
    const mkt=document.getElementById('ef-mkt').value;
    ticker=code+'.'+mkt; isCrypto=false;
  }
  hideAll();
  document.getElementById('emafvg-res').style.display='block';
  document.getElementById('emafvg-res').innerHTML=loading('EMA + FVG 멀티타임프레임 분석 중...<br><span style="font-size:11px;color:var(--muted2)">1H · 4H · 1D 동시 분석 (15~30초)</span>');
  fetch('/emafvg',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({ticker,is_crypto:isCrypto,account:acct,risk_pct:risk})})
    .then(r=>r.json()).then(d=>renderEmaFvg(d))
    .catch(e=>{document.getElementById('emafvg-res').innerHTML=`<div class="empty">❌ ${e}</div>`});
}

function renderEmaFvg(d){
  const wrap=document.getElementById('emafvg-res');
  if(d.error){wrap.innerHTML=`<div class="empty">❌ ${d.error}</div>`;return}
  const fmt=v=>d.is_crypto?(v>=1?'$'+v.toLocaleString(undefined,{maximumFractionDigits:2}):'$'+v.toFixed(5)):v.toLocaleString()+'원';
  const fmtAmt=v=>d.is_crypto?'$'+v.toLocaleString(undefined,{maximumFractionDigits:2}):Math.round(v/10000).toLocaleString()+'만원';
  const chg=d.change||0;
  const iconMap={'BTC-USD':'₿','ETH-USD':'Ξ','XRP-USD':'✕'};
  const icon=iconMap[d.ticker]||'📈';
  const tfs=d.timeframes||{};
  const tfOrder=['1h','4h','1d'];
  const tfNames={'1h':'1시간','4h':'4시간','1d':'1일'};

  // EMA alignment summary
  let emaHtml='<div class="ema-align-grid">';
  tfOrder.forEach((tf,idx)=>{
    const t=tfs[tf]||{};
    const ema=t.ema||{};
    const rows=['EMA20','EMA50','EMA200'].map(name=>{
      const val=ema[name.toLowerCase().replace('ema','e')]||0;
      const diff=val>0?((t.price-val)/val*100):0;
      const cls=diff>0.5?'above':diff<-0.5?'below':'near';
      const lbl=diff>0.5?'위':diff<-0.5?'아래':'근접';
      return `<div class="ema-row ${cls}">
        <span class="ema-name">${name}</span>
        <span class="ema-val ${diff>0?'g':diff<0?'r':'y'}">${fmt(val)}</span>
        <span class="ema-pos ${cls}">${lbl} ${Math.abs(diff).toFixed(1)}%</span>
      </div>`;
    }).join('');
    const aligned=t.ema_aligned;
    const badgeCls=aligned==='FULL'?'full':aligned==='PARTIAL'?'partial':'none';
    const badgeTxt=aligned==='FULL'?'✅ 완전 정렬':aligned==='PARTIAL'?'⚡ 부분 정렬':'❌ 미정렬';
    emaHtml+=`<div class="ema-tf-card" style="animation-delay:${idx*60}ms">
      <div class="ema-tf-label">${tfNames[tf]} EMA 상태</div>
      <div class="ema-stack">${rows}</div>
      <div class="ema-align-badge ${badgeCls}">${badgeTxt}</div>
    </div>`;
  });
  emaHtml+='</div>';

  // Confluence cards
  let cfHtml='<div style="font-size:11px;color:var(--muted);letter-spacing:2px;text-transform:uppercase;margin-bottom:10px">⚡ FVG + EMA 컨플루언스 타점</div><div class="confluence-grid">';

  const setups=d.setups||[];
  if(!setups.length){
    cfHtml+=`<div style="padding:20px;color:var(--muted);font-size:13px;grid-column:1/-1">현재 유효한 EMA+FVG 컨플루언스 구간이 없습니다. 가격이 FVG 구간으로 접근하면 타점이 생성됩니다.</div>`;
  } else {
    setups.forEach((s,idx)=>{
      const qCls=s.quality==='HIGH'?'high':s.quality==='MID'?'mid':'low';
      const qTxt=s.quality==='HIGH'?'⭐ 최우선':s.quality==='MID'?'▶ 진입 가능':'◎ 참고';
      const isBull=s.direction==='LONG';

      // Price visual (simplified bar)
      const minP=Math.min(s.sl,s.tp3,s.current)*0.998;
      const maxP=Math.max(s.sl,s.tp3,s.current)*1.002;
      const rng=maxP-minP||1;
      const pct=v=>((v-minP)/rng*100).toFixed(1);
      const entryPct=pct(s.entry); const slPct=pct(s.sl);
      const tp1Pct=pct(s.tp1); const tp2Pct=pct(s.tp2);
      const curPct=pct(s.current);
      const fvgTopPct=pct(s.fvg_high); const fvgBotPct=pct(s.fvg_low);
      const fvgH=Math.abs(parseFloat(fvgTopPct)-parseFloat(fvgBotPct));

      cfHtml+=`<div class="cf-card ${qCls}" style="animation-delay:${idx*70}ms">
        <div class="cf-head">
          <div class="cf-tf">${tfNames[s.tf]||s.tf} FVG</div>
          <div style="display:flex;gap:7px;align-items:center">
            <span class="cf-quality ${qCls}">${qTxt}</span>
            <span style="font-size:11px;font-weight:700;color:${isBull?'var(--green)':'var(--red)'}">${isBull?'▲ LONG':'▼ SHORT'}</span>
          </div>
        </div>
        <div class="cf-body">

          <!-- 가격 시각화 바 -->
          <div class="cf-section">
            <div class="cf-sec-lbl">가격 레벨 시각화</div>
            <div class="price-visual">
              <!-- FVG zone -->
              <div class="pv-zone" style="bottom:${Math.min(fvgBotPct,fvgTopPct)}%;height:${fvgH}%;min-height:4px;opacity:.8"></div>
              <!-- SL line -->
              <div style="position:absolute;bottom:${slPct}%;left:0;right:0;height:1px;background:rgba(255,71,87,.6);"></div>
              <div style="position:absolute;bottom:${slPct}%;left:6px;font-size:8px;color:rgba(255,71,87,.8);font-family:monospace">SL</div>
              <!-- TP1 line -->
              <div style="position:absolute;bottom:${tp1Pct}%;left:0;right:0;height:1px;background:rgba(74,222,128,.5);"></div>
              <div style="position:absolute;bottom:${tp1Pct}%;left:6px;font-size:8px;color:rgba(74,222,128,.8);font-family:monospace">TP1</div>
              <!-- TP2 line -->
              <div style="position:absolute;bottom:${tp2Pct}%;left:0;right:0;height:1px;background:rgba(0,255,136,.5);"></div>
              <div style="position:absolute;bottom:${tp2Pct}%;left:6px;font-size:8px;color:rgba(0,255,136,.8);font-family:monospace">TP2</div>
              <!-- Current price -->
              <div style="position:absolute;bottom:${curPct}%;left:8px;right:30px;height:2px;background:var(--blue);border-radius:1px;"></div>
              <div style="position:absolute;bottom:${curPct}%;right:4px;font-size:8px;color:var(--blue);font-family:monospace">◀ 현재</div>
              <!-- Entry -->
              <div style="position:absolute;bottom:${entryPct}%;left:0;right:0;height:1.5px;background:var(--yellow);border-radius:1px;"></div>
              <div style="position:absolute;bottom:${entryPct}%;right:4px;font-size:8px;color:var(--yellow);font-family:monospace">진입</div>
            </div>
          </div>

          <!-- 가격 레벨 -->
          <div class="cf-section">
            <div class="cf-sec-lbl">진입 타점 상세</div>
            <div class="ef-levels">
              <div class="ef-lv"><span class="ef-lv-name">📍 현재가</span><span class="ef-lv-val ef-cur-val">${fmt(s.current)}</span></div>
              <div class="ef-lv"><span class="ef-lv-name">🎯 진입가(FVG50%)</span><span><span class="ef-lv-val ef-entry-val">${fmt(s.entry)}</span><span class="ef-lv-pct ef-entry-pct">${s.entry_diff>=0?'+':''}${(s.entry_diff||0).toFixed(2)}%</span></span></div>
              <div class="ef-lv"><span class="ef-lv-name">🛑 손절 (FVG엣지)</span><span><span class="ef-lv-val ef-sl-val">${fmt(s.sl)}</span><span class="ef-lv-pct ef-sl-pct">-${(s.sl_pct||0).toFixed(2)}%</span></span></div>
              <div class="ef-lv"><span class="ef-lv-name">✅ 1차목표 (1:2)</span><span><span class="ef-lv-val ef-tp1-val">${fmt(s.tp1)}</span><span class="ef-lv-pct ef-tp1-pct">+${(s.tp1_pct||0).toFixed(2)}%</span></span></div>
              <div class="ef-lv"><span class="ef-lv-name">🚀 2차목표 (1:3)</span><span><span class="ef-lv-val ef-tp2-val">${fmt(s.tp2)}</span><span class="ef-lv-pct ef-tp2-pct">+${(s.tp2_pct||0).toFixed(2)}%</span></span></div>
              <div class="ef-lv"><span class="ef-lv-name">💎 3차목표 (1:5)</span><span><span class="ef-lv-val ef-tp3-val">${fmt(s.tp3)}</span><span class="ef-lv-pct ef-tp2-pct">+${(s.tp3_pct||0).toFixed(2)}%</span></span></div>
            </div>
          </div>

          <!-- 컨플루언스 태그 -->
          <div class="cf-score-row">
            ${(s.confluence_tags||[]).map(t=>`<span class="cf-tag ${t.ok?'ok':t.warn?'warn':'no'}">${t.text}</span>`).join('')}
          </div>

          <!-- 이유 -->
          <div class="reason-box">${s.reason||''}</div>

          <!-- 금액 정보 -->
          <div class="ef-money">
            <div class="ef-mbox"><div class="ef-mlbl">수량</div><div class="ef-mval y">${d.is_crypto?((s.qty||0).toFixed(4)+' '+d.ticker.split('-')[0]):(s.qty||0).toLocaleString()+'주'}</div></div>
            <div class="ef-mbox"><div class="ef-mlbl">투자금액</div><div class="ef-mval b">${fmtAmt(s.invest_amt||0)}</div></div>
            <div class="ef-mbox"><div class="ef-mlbl">최대손실</div><div class="ef-mval r">${fmtAmt(s.risk_amt||0)}</div></div>
            <div class="ef-mbox"><div class="ef-mlbl">R:R</div><div class="ef-mval g">1 : ${(s.rr||2).toFixed(1)}</div></div>
          </div>

        </div>
      </div>`;
    });
  }
  cfHtml+='</div>';

  let heroHtml=`<div class="ef-hero">
    <div class="ef-code">${icon} ${d.ticker.split('-')[0]}</div>
    <div><div class="ef-name">${d.name}</div><div class="ef-sub">${d.ticker}${d.is_crypto?' · Binance':''} · EMA+FVG 분석</div></div>
    <div style="margin-left:auto"><div class="ef-price">${fmt(d.price||0)}<span class="ef-chg" style="color:${chg>=0?'var(--green)':'var(--red)'}">${chg>=0?'+':''}${chg.toFixed(2)}%</span></div></div>
  </div>`;

  wrap.innerHTML = heroHtml + emaHtml + cfHtml +
    `<div style="margin-top:10px;padding:11px 14px;background:rgba(52,211,153,.05);border:1px solid rgba(52,211,153,.2);border-radius:8px;font-size:11px;color:var(--muted);line-height:1.8">
      ⭐ <strong style="color:#34d399">최우선 진입:</strong> EMA 완전정렬 + FVG가 EMA에 인접 + FVG 50% 리테스트 대기 중 →
      반응 캔들(핀바·엔걸핑) 확인 후 진입 · 손절: FVG 엣지 이탈 시 즉시<br>
      ⚠️ 이 분석은 참고용입니다. 모든 투자 판단과 손익은 본인 책임입니다.
    </div>`;
}
/* ══════════════════════════════════════
   KR MA + VOLUME 타점
══════════════════════════════════════ */
function doKrMA(){
  const code=document.getElementById('kr-code').value.trim().padStart(6,'0');
  const mkt=document.getElementById('kr-mkt').value;
  const acct=parseInt(document.getElementById('kr-acct').value)||10000000;
  const risk=parseFloat(document.getElementById('kr-risk').value)||1;
  if(!code||code.length<5){alert('종목 코드를 입력하세요');return}
  hideAll();
  document.getElementById('krma-res').style.display='block';
  document.getElementById('krma-res').innerHTML=loading(code+' 거래량+이동평균 분석 중...<br><span style="font-size:11px;color:var(--muted2)">(10~20초)</span>');
  fetch('/krma',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({code,market:mkt,account:acct,risk_pct:risk})})
    .then(r=>r.json()).then(d=>renderKrMA(d))
    .catch(e=>{document.getElementById('krma-res').innerHTML=`<div class="empty">❌ ${e}</div>`});
}

function renderKrMA(d){
  const wrap=document.getElementById('krma-res');
  if(d.error){wrap.innerHTML=`<div class="empty">❌ ${d.error}</div>`;return}
  const fmt=v=>v.toLocaleString()+'원';
  const fmtM=v=>Math.round(v/10000).toLocaleString()+'만원';
  const chg=d.change||0;
  const mas=d.ma||{}; const vol=d.volume||{}; const signals=d.signals||[];

  // MA status boxes
  let maHtml='<div class="ma-status-grid">';
  [['5일선',mas.ma5],['20일선',mas.ma20],['60일선',mas.ma60],['120일선',mas.ma120]].forEach(([name,val])=>{
    if(!val){maHtml+=`<div class="ma-box"><div class="ma-name">${name}</div><div class="ma-val y">N/A</div></div>`;return}
    const diff=(d.price-val)/val*100;
    const cls=diff>0.5?'above':diff<-0.5?'below':'near';
    const lbl=diff>0.5?'위':diff<-0.5?'아래':'근접';
    const valCls=diff>0?'g':diff<0?'r':'y';
    maHtml+=`<div class="ma-box ${cls}">
      <div class="ma-name">${name}</div>
      <div class="ma-val ${valCls}">${fmt(val)}</div>
      <div class="ma-diff" style="color:${diff>0?'var(--green)':diff<0?'var(--red)':'var(--yellow)'}">${diff>=0?'+':''}${diff.toFixed(1)}%</div>
      <div class="ma-pos-badge ${cls}">${lbl}</div>
    </div>`;
  });
  maHtml+='</div>';

  // MA alignment overall
  const bullMAs=['ma5','ma20','ma60','ma120'].filter(k=>mas[k]&&d.price>mas[k]).length;
  const alignCls=bullMAs>=3?'bull':bullMAs<=1?'bear':'mixed';
  const alignTxt=bullMAs>=3?`✅ 상승 정렬 (${bullMAs}/4 이평선 위)`:bullMAs<=1?`❌ 하락 정렬 (${bullMAs}/4 이평선 위)`:
    `⚡ 혼조 (${bullMAs}/4 이평선 위)`;
  const m5above20=mas.ma5&&mas.ma20&&mas.ma5>mas.ma20;
  const m20above60=mas.ma20&&mas.ma60&&mas.ma20>mas.ma60;
  const m60above120=mas.ma60&&mas.ma120&&mas.ma60>mas.ma120;

  let alignTrack=`<div class="ma-align-track">
    ${['5>20','20>60','60>120'].map((lbl,i)=>{
      const ok=[m5above20,m20above60,m60above120][i];
      return `<span class="ma-align-pill ${ok?'above':'below'}">${ok?'✅':'❌'} ${lbl}</span>${i<2?'<span class="ma-align-arr">→</span>':''}`;
    }).join('')}
  </div>
  <div class="ma-overall ${alignCls}">${alignTxt}</div>`;

  let maAlignHtml=`<div class="ma-align-wrap">
    <div class="ma-align-title">이동평균선 정렬 상태</div>
    ${alignTrack}
  </div>`;

  // Volume bar
  const maxVol=Math.max(vol.today||0,vol.avg5||0,vol.avg20||0)*1.1||1;
  const volSigCls=(vol.ratio||0)>=2?'surge':(vol.ratio||0)>=1.2?'normal':'low';
  const volSigTxt=(vol.ratio||0)>=2?`🔥 거래량 폭발! 평균의 ${(vol.ratio||0).toFixed(1)}배 — 세력 진입 가능성`
    :(vol.ratio||0)>=1.2?`📈 거래량 보통 (평균의 ${(vol.ratio||0).toFixed(1)}배)`
    :`⚠️ 거래량 부족 (평균의 ${(vol.ratio||0).toFixed(1)}배) — 진입 주의`;
  const volHtml=`<div class="vol-bar-wrap">
    <div class="vol-bar-title">거래량 분석</div>
    ${[['오늘',vol.today,'#fb923c'],['5일 평균',vol.avg5,'var(--blue)'],['20일 평균',vol.avg20,'var(--muted)']].map(([lbl,v,c])=>{
      if(!v)return '';
      const pct=Math.min(100,(v/maxVol*100));
      return `<div class="vol-row">
        <span class="vol-row-lbl">${lbl}</span>
        <div class="vol-row-bar"><div class="vol-row-fill" style="width:${pct}%;background:${c}"></div></div>
        <span class="vol-row-val" style="color:${c}">${Math.round(v/1000).toLocaleString()}천주</span>
      </div>`;
    }).join('')}
    <div class="vol-signal ${volSigCls}">${volSigTxt}</div>
  </div>`;

  // Signal cards
  let sigHtml='<div class="kr-signal-grid">';
  if(!signals.length){
    sigHtml+=`<div style="padding:20px;color:var(--muted);font-size:13px;grid-column:1/-1">현재 명확한 매매 신호가 없습니다. 이평선 정렬 또는 거래량 조건이 충족되면 타점이 나타납니다.</div>`;
  }
  signals.forEach((s,idx)=>{
    const cardCls=s.type==='BUY'?'buy':s.type==='SELL'?'sell':'watch';
    const badgeTxt=s.type==='BUY'?'매수 신호':s.type==='SELL'?'매도 신호':'관망';
    sigHtml+=`<div class="krs-card ${cardCls}" style="animation-delay:${idx*60}ms">
      <div class="krs-head">
        <div class="krs-title">${s.strategy}</div>
        <span class="krs-badge">${badgeTxt}</span>
      </div>
      <div class="krs-body">
        <div class="krs-levels">
          <div class="krs-lv"><span class="krs-lv-lbl">📍 현재가</span><span class="krs-lv-val krs-cur-v">${fmt(d.price)}</span></div>
          <div class="krs-lv"><span class="krs-lv-lbl">🎯 진입가</span><span><span class="krs-lv-val krs-entry-v">${fmt(s.entry)}</span><span class="krs-lv-pct krs-entry-p">${s.entry_diff>=0?'+':''}${(s.entry_diff||0).toFixed(1)}%</span></span></div>
          <div class="krs-lv"><span class="krs-lv-lbl">🛑 손절 (${s.sl_basis})</span><span><span class="krs-lv-val krs-sl-v">${fmt(s.sl)}</span><span class="krs-lv-pct krs-sl-p">-${(s.sl_pct||0).toFixed(1)}%</span></span></div>
          <div class="krs-lv"><span class="krs-lv-lbl">✅ 1차 목표 (1:2)</span><span><span class="krs-lv-val krs-tp1-v">${fmt(s.tp1)}</span><span class="krs-lv-pct krs-tp1-p">+${(s.tp1_pct||0).toFixed(1)}%</span></span></div>
          <div class="krs-lv"><span class="krs-lv-lbl">🚀 2차 목표 (1:3)</span><span><span class="krs-lv-val krs-tp2-v">${fmt(s.tp2)}</span><span class="krs-lv-pct krs-tp2-p">+${(s.tp2_pct||0).toFixed(1)}%</span></span></div>
          <div class="krs-lv"><span class="krs-lv-lbl">💎 3차 목표 (1:5)</span><span><span class="krs-lv-val krs-tp3-v">${fmt(s.tp3)}</span><span class="krs-lv-pct krs-tp3-p">+${(s.tp3_pct||0).toFixed(1)}%</span></span></div>
        </div>
        <div class="krs-tags">${(s.tags||[]).map(t=>`<span class="krs-tag ${t.ok?'ok':t.warn?'warn':'no'}">${t.text}</span>`).join('')}</div>
        <div class="krs-reason">${s.reason}</div>
        <div class="krs-money">
          <div class="krs-mbox"><div class="krs-mlbl">수량</div><div class="krs-mval y">${(s.shares||0).toLocaleString()}주</div></div>
          <div class="krs-mbox"><div class="krs-mlbl">투자금액</div><div class="krs-mval b">${fmtM(s.invest_amt||0)}</div></div>
          <div class="krs-mbox"><div class="krs-mlbl">최대손실</div><div class="krs-mval r">${fmtM(s.risk_amt||0)}</div></div>
          <div class="krs-mbox"><div class="krs-mlbl">R:R</div><div class="krs-mval g">1:${(s.rr||2).toFixed(1)}</div></div>
        </div>
      </div>
    </div>`;
  });
  sigHtml+='</div>';

  // ── 외국인/기관 수급 렌더링 ──
  const inv = d.investor||{};
  function renderInvCard(data, title, colorCls){
    if(!data) return `<div class="inv-card">
      <div class="inv-head"><div class="inv-title ${colorCls}">${title}</div></div>
      <div class="inv-no-data">수급 데이터 없음<br><span style="font-size:10px">FinanceDataReader 미지원 종목</span></div>
    </div>`;
    const todayV=data.today||0; const d5=data['5d']||0; const d20=data['20d']||0;
    const trend=data.trend||'중립';
    const badgeCls=trend==='매수우세'?'buy':trend==='매도우세'?'sell':'neutral';
    const badgeTxt=trend==='매수우세'?'📈 매수우세':trend==='매도우세'?'📉 매도우세':'⚖️ 중립';
    const fmtN=v=>v===0?'—':(v>0?'+':'')+Math.round(v/1000).toLocaleString()+'천주';
    const fmtC=v=>v>0?'pos':v<0?'neg':'neu';

    // 미니 바 차트 (최근 20일)
    const series=data.series||[];
    let miniChart='';
    if(series.length>0){
      const absMax=Math.max(...series.map(Math.abs),1);
      miniChart=`<div class="inv-bar-lbl">20일 순매수 추이</div>
      <div class="inv-mini-chart">
        ${series.map(v=>{
          const h=Math.max(2,Math.abs(v)/absMax*36);
          return `<div class="inv-bar ${v>=0?'pos':'neg'}" style="height:${h}px" title="${fmtN(v)}"></div>`;
        }).join('')}
      </div>`;
    }

    return `<div class="inv-card">
      <div class="inv-head">
        <div class="inv-title ${colorCls}">${title}</div>
        <span class="inv-badge ${badgeCls}">${badgeTxt}</span>
      </div>
      <div class="inv-body">
        <div class="inv-row"><span class="inv-lbl">오늘 순매수</span><span class="inv-val ${fmtC(todayV)}">${fmtN(todayV)}</span></div>
        <div class="inv-row"><span class="inv-lbl">5일 누적</span><span class="inv-val ${fmtC(d5)}">${fmtN(d5)}</span></div>
        <div class="inv-row"><span class="inv-lbl">20일 누적</span><span class="inv-val ${fmtC(d20)}">${fmtN(d20)}</span></div>
        ${series.length>0?`<div class="inv-bar-section">${miniChart}</div>`:''}
      </div>
    </div>`;
  }

  const invHtml=`<div style="font-size:11px;color:var(--muted);letter-spacing:2px;text-transform:uppercase;margin-bottom:8px">📊 외국인 · 기관 수급 (최근 20일)</div>
  <div class="inv-grid">
    ${renderInvCard(inv.foreign,'🌏 외국인','forg')}
    ${renderInvCard(inv.institution,'🏦 기관','inst')}
  </div>`;

  wrap.innerHTML=`<div class="kr-hero">
    <div class="kr-code">${d.code}</div>
    <div><div class="kr-name">${d.name}</div><div class="kr-sub">${d.ticker} · ${d.market==='KS'?'코스피':'코스닥'} · 거래량+MA 분석</div></div>
    <div style="margin-left:auto"><div class="kr-price">${fmt(d.price)}<span class="kr-chg" style="color:${chg>=0?'var(--green)':'var(--red)'}">${chg>=0?'+':''}${chg.toFixed(2)}%</span></div></div>
  </div>
  ${maHtml}${maAlignHtml}${invHtml}${volHtml}${sigHtml}
  <div style="margin-top:10px;padding:11px 14px;background:rgba(251,146,60,.05);border:1px solid rgba(251,146,60,.2);border-radius:8px;font-size:11px;color:var(--muted);line-height:1.8">
    ⚠️ 이 분석은 참고용입니다. 실제 진입 전 기업 공시·뉴스·시장 상황을 반드시 확인하세요. 모든 투자 판단과 손익은 <strong style="color:var(--txt)">본인 책임</strong>입니다.
  </div>`;
}
/* ══════════════════════════════════════
   매수거래량 증가 스캐너
══════════════════════════════════════ */
let vsDays=3;
function vsDay(d){
  vsDays=d;
  [1,3,5].forEach(x=>{
    const b=document.getElementById('vs-d'+x); if(!b)return;
    b.className='at-btn'+(x===d?' on-stock':'');
  });
}

function doVolScan(){
  const mkt=document.getElementById('vs-mkt').value;
  const minRatio=parseFloat(document.getElementById('vs-minratio').value)||50;
  const limit=parseInt(document.getElementById('vs-lim').value)||50;
  const btn=document.getElementById('btn-volscan'); btn.disabled=true;
  hideAll();
  document.getElementById('volscan-res').style.display='block';
  document.getElementById('volscan-res').innerHTML=loading(
    `매수거래량 증가 종목 스캔 중...<br><span style="font-size:11px;color:var(--muted2)">최근 ${vsDays}일 기준 · ${mkt==='ALL'?'전체':mkt} (2~5분 소요)</span>`);
  fetch('/vol_scan',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({days:vsDays,market:mkt,min_ratio:minRatio,limit})})
    .then(r=>r.json())
    .then(d=>{btn.disabled=false; renderVolScan(d);})
    .catch(e=>{btn.disabled=false;
      document.getElementById('volscan-res').innerHTML=`<div class="empty">❌ ${e}</div>`;});
}

function renderVolScan(d){
  const wrap=document.getElementById('volscan-res');
  if(d.error){wrap.innerHTML=`<div class="empty">❌ ${d.error}</div>`;return}
  const results=d.results||[];
  if(!results.length){
    wrap.innerHTML='<div class="empty">조건에 맞는 종목이 없습니다. 최소 증가율을 낮춰 다시 시도해 보세요.</div>';return;
  }

  const maxRatio=Math.max(...results.map(r=>r.vol_ratio));

  let html=`
  <div class="vs-summary">
    <span><span class="vs-sum-lbl">발견 종목</span><span class="vs-sum-val" style="color:#fb923c">${results.length}개</span></span>
    <span><span class="vs-sum-lbl">기간</span><span class="vs-sum-val">최근 ${d.days}일 vs 직전 ${d.days}일</span></span>
    <span><span class="vs-sum-lbl">시장</span><span class="vs-sum-val">${d.market==='ALL'?'전체':d.market}</span></span>
    <span><span class="vs-sum-lbl">최소증가율</span><span class="vs-sum-val">+${d.min_ratio}%</span></span>
    <span style="margin-left:auto;display:flex;align-items:center;gap:8px">
      <span style="font-size:11px;color:var(--muted)">💡 거래량 증가율 높은 순 정렬</span>
      <button onclick="clearVolRes()" style="padding:4px 11px;border-radius:5px;background:rgba(255,71,87,.12);color:var(--red);border:1px solid rgba(255,71,87,.3);cursor:pointer;font-size:11px;font-weight:700">🗑 지우기</button>
    </span>
  </div>
  <div class="vs-thead">
    <span></span><span>순위</span><span>코드</span><span>종목명</span>
    <span style="text-align:right">현재가</span>
    <span style="text-align:right">등락률</span>
    <span style="text-align:right">거래량증가</span>
    <span style="text-align:right">양봉비율</span>
    <span style="text-align:right">시장</span>
  </div>`;

  results.forEach((r,i)=>{
    const barW=Math.min(100,(r.vol_ratio/maxRatio*100)).toFixed(0);
    const ratioCls=r.vol_ratio>=2?'hot':r.vol_ratio>=1.5?'warm':'norm';
    const vsStar=makeStar(r.code,r.name,r.market||'',r.price||0,r.change||0,'volscan');
    const barColor=r.vol_ratio>=2?'#fb923c':r.vol_ratio>=1.5?'#ffa502':'#7d8590';
    const chgColor=r.change>=0?'var(--green)':'var(--red)';
    const mktCls=r.market==='코스피'?'kp':'kq';
    html+=`<div class="vs-row" style="animation-delay:${Math.min(i,30)*10}ms">
      <span style="text-align:center">${vsStar}</span><span class="vs-rank">${i+1}</span>
      <span class="vs-code">${r.code}</span>
      <span class="vs-name">${r.name}</span>
      <span class="vs-price">${r.price.toLocaleString()}원</span>
      <span class="vs-chg" style="color:${chgColor}">${r.change>=0?'+':''}${r.change.toFixed(1)}%</span>
      <span>
        <span class="vs-ratio ${ratioCls}">+${((r.vol_ratio-1)*100).toFixed(0)}%</span>
        <div class="vs-bar-wrap"><div class="vs-bar-fill" style="width:${barW}%;background:${barColor}"></div></div>
        <span class="vs-vol">${Math.round(r.recent_vol/1000).toLocaleString()}천주</span>
      </span>
      <span class="vs-bull" style="color:${r.bull_pct>=60?'var(--green)':r.bull_pct>=40?'var(--yellow)':'var(--red)'}">
        ${r.bull_pct.toFixed(0)}% 🟢
      </span>
      <span><span class="vs-badge ${mktCls}">${r.market}</span></span>
    </div>`;
  });

  wrap.innerHTML=html+'</div>';
  // wrap table
  const tableStart=wrap.innerHTML.indexOf('<div class="vs-summary');
  wrap.innerHTML=wrap.innerHTML.slice(0,tableStart)+
    '<div class="vs-table-wrap">'+wrap.innerHTML.slice(tableStart)+'</div>';
}
/* ══════════════════════════════════════
   교집합 스캔 (CROSS SCAN)
══════════════════════════════════════ */
let cxSelected = new Set(['surge','high']);

function cxToggle(type, el){
  if(cxSelected.has(type)){
    if(cxSelected.size<=2){ alert('최소 2개 이상 선택해야 합니다'); return; }
    cxSelected.delete(type);
    el.classList.remove('sel');
    document.getElementById('cx-'+type+'-chk').textContent='';
  } else {
    cxSelected.add(type);
    el.classList.add('sel');
    document.getElementById('cx-'+type+'-chk').textContent='✓';
  }
}

function doCross(){
  if(cxSelected.size < 2){ alert('2개 이상 선택해주세요'); return; }
  const btn=document.getElementById('btn-cross'); btn.disabled=true;
  const mkt=document.getElementById('cx-mkt').value;
  const period=document.getElementById('cx-period').value;
  const minpct=document.getElementById('cx-minpct').value;
  const voldays=document.getElementById('cx-voldays').value;
  const volratio=document.getElementById('cx-volratio').value;
  const types=[...cxSelected];

  hideAll();
  document.getElementById('cross-res').style.display='block';
  document.getElementById('cross-res').innerHTML=`
    <div class="cross-prog">
      <div class="spin" style="border-top-color:#06b6d4"></div>
      <div class="cross-prog-lbl">교집합 스캔 실행 중...</div>
      <div class="cross-prog-detail" id="cx-prog-detail">스캔 준비 중...</div>
      <div class="cross-prog-bar"><div class="cross-prog-fill" id="cx-prog-fill" style="width:5%"></div></div>
      <div style="font-size:11px;color:var(--muted2);margin-top:4px">선택: ${types.map(t=>({surge:'급등',high:'전고점',vol:'거래량'})[t]).join(' ∩ ')} · 전체 종목 스캔 (3~8분 소요)</div>
    </div>`;

  let prog=5;
  const steps=['종목 리스트 수집...','급등 종목 스캔...','전고점 돌파 확인...','거래량 분석...','교집합 추출...'];
  let stepIdx=0;
  const iv=setInterval(()=>{
    prog=Math.min(prog+1.5,88);
    const pf=document.getElementById('cx-prog-fill'); if(pf) pf.style.width=prog+'%';
    const pd=document.getElementById('cx-prog-detail');
    if(pd){ stepIdx=Math.floor(prog/20); pd.textContent=steps[Math.min(stepIdx,steps.length-1)]; }
    if(prog>=88) clearInterval(iv);
  },1200);

  fetch('/cross_scan',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({types,market:mkt,period:parseInt(period),
      minpct:parseFloat(minpct),vol_days:parseInt(voldays),vol_min_ratio:parseFloat(volratio)/100+1})})
    .then(r=>r.json())
    .then(d=>{ clearInterval(iv); btn.disabled=false; renderCross(d); })
    .catch(e=>{ clearInterval(iv); btn.disabled=false;
      document.getElementById('cross-res').innerHTML=`<div class="empty">❌ ${e}</div>`; });
}

function renderCross(d){
  const wrap=document.getElementById('cross-res');
  if(d.error){ wrap.innerHTML=`<div class="empty">❌ ${d.error}</div>`; return; }
  const results=d.results||[];
  const types=d.types||[];
  const typeLabel={surge:'급등',high:'전고점',vol:'거래량'};
  const typeClass={surge:'surge',high:'high',vol:'vol'};

  if(!results.length){
    wrap.innerHTML=`<div class="empty">교집합 조건을 만족하는 종목이 없습니다.<br><span style="color:var(--muted2);font-size:11px">조건을 완화해 다시 시도해보세요</span></div>`;
    return;
  }

  const m3=results.filter(r=>r.match_count===3).length;
  const m2=results.filter(r=>r.match_count===2).length;

  let html=`
  <div class="vs-table-wrap">
    <div class="cross-result-header">
      <span style="font-family:'Bebas Neue';font-size:22px;color:#06b6d4">교집합 결과 ${results.length}종목</span>
      ${types.map(t=>`<span class="cross-tag ${typeClass[t]}">${typeLabel[t]}</span>`).join('<span style="color:var(--muted);font-size:14px">∩</span>')}
      <span style="margin-left:auto;display:flex;gap:8px;align-items:center">
        ${m3>0?`<span style="font-size:11px;color:#a78bfa;font-weight:700">🔥 3중 교집합: ${m3}개</span>`:''}
        ${m2>0?`<span style="font-size:11px;color:var(--yellow);font-weight:700">✅ 2중 교집합: ${m2}개</span>`:''}
        <button onclick="clearCrossRes()" style="padding:4px 10px;border-radius:5px;background:rgba(255,71,87,.12);color:var(--red);border:1px solid rgba(255,71,87,.3);cursor:pointer;font-size:11px;font-weight:700">🗑 지우기</button>
      </span>
    </div>
    <div class="cross-thead">
      <span></span><span>순위</span><span>코드</span><span>종목명</span>
      <span style="text-align:right">현재가</span>
      <span style="text-align:right">등락률</span>
      <span style="text-align:right">급등률</span>
      <span style="text-align:right">돌파율</span>
      <span style="text-align:right">거래량↑</span>
      <span>조건</span>
    </div>`;

  results.forEach((r,i)=>{
    const rowCls=r.match_count===3?'cross-match-3':'cross-match-2';
    const chgColor=r.change>=0?'var(--green)':'var(--red)';
    const mktBadge=r.market==='코스피'?`<span class="vs-badge kp">KOSPI</span>`:`<span class="vs-badge kq">KOSDAQ</span>`;
    const badges=(r.matched_types||[]).map(t=>`<span class="cross-b ${typeClass[t]}">${typeLabel[t]}</span>`).join('');
    const crStar=makeStar(r.code,r.name,r.market||'',r.price||0,r.change||0,'cross');
    html+=`<div class="cross-row ${rowCls}" style="animation-delay:${Math.min(i,30)*12}ms">
      <span style="text-align:center">${crStar}</span><span class="vs-rank">${i+1}</span>
      <span class="vs-code">${r.code}</span>
      <span class="vs-name">${r.name} ${mktBadge}</span>
      <span class="vs-price" style="text-align:right">${r.price.toLocaleString()}원</span>
      <span class="vs-chg" style="color:${chgColor};text-align:right">${r.change>=0?'+':''}${r.change.toFixed(1)}%</span>
      <span style="font-family:'JetBrains Mono';font-size:11px;color:var(--green);text-align:right">${r.surge_pct!=null?'+'+r.surge_pct.toFixed(1)+'%':'—'}</span>
      <span style="font-family:'JetBrains Mono';font-size:11px;color:var(--yellow);text-align:right">${r.high_pct!=null?'+'+r.high_pct.toFixed(1)+'%':'—'}</span>
      <span style="font-family:'JetBrains Mono';font-size:11px;color:#fb923c;text-align:right">${r.vol_ratio!=null?'+'+((r.vol_ratio-1)*100).toFixed(0)+'%':'—'}</span>
      <span><div class="cross-badge-wrap">${badges}</div></span>
    </div>`;
  });
  html+='</div>';
  wrap.innerHTML=html;
}

function clearCrossRes(){
  document.getElementById('cross-res').style.display='none';
  document.getElementById('cross-res').innerHTML='';
}

/* ══════════════════════════════════════
   📅 즐겨찾기 가격 히스토리 히트맵
══════════════════════════════════════ */
let favViewMode = 'list';

function switchFavView(mode){
  favViewMode = mode;
  document.getElementById('fav-tab-list').className='hist-tab'+(mode==='list'?' active':'');
  document.getElementById('fav-tab-hist').className='hist-tab'+(mode==='hist'?' active':'');
  document.getElementById('fav-res').style.display     = mode==='list'?'block':'none';
  document.getElementById('fav-hist-res').style.display = mode==='hist'?'block':'none';
  if(mode==='hist') loadFavHistory();
}

function loadFavHistory(){
  const wrap = document.getElementById('fav-hist-res');
  wrap.innerHTML=loading('동기화 후 이력 불러오는 중...');

  // Step 1: localStorage → 서버 sync 먼저
  const favs = loadFavs();
  if(!favs.length){
    wrap.innerHTML='<div class="empty">즐겨찾기 종목이 없습니다.<br><span style="font-size:11px">스캔 결과에서 ★ 버튼으로 추가하세요</span></div>';
    return;
  }

  fetch('/api/fav/sync',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({favs})})
    .then(r=>r.json())
    .then(()=>{
      // Step 2: sync 완료 후 히스토리 로드
      return fetch('/api/fav/history');
    })
    .then(r=>{if(r.status===401){location.href='/login';return null;} return r.json();})
    .then(d=>{ if(d) renderFavHistory(d); })
    .catch(e=>{ wrap.innerHTML=`<div class="empty">❌ ${e}</div>`; });
}

function renderFavHistory(d){
  const wrap = document.getElementById('fav-hist-res');
  const stocks = d.stocks||[];
  const dates  = d.dates||[];

  if(!stocks.length){
    wrap.innerHTML='<div class="empty">즐겨찾기 종목이 없습니다.<br><span style="font-size:11px">스캔 결과에서 ★ 버튼으로 추가하세요</span></div>';
    return;
  }

  const noData = stocks.every(s=>s.days_tracked===0);

  // 히트맵 셀 색상 결정
  function cellClass(chg){
    if(chg===null||chg===undefined) return 'nodata';
    if(Math.abs(chg)<0.05) return 'flat';
    if(chg>=3)  return 'up-3';
    if(chg>=1)  return 'up-2';
    if(chg>0)   return 'up-1';
    if(chg<=-3) return 'dn-3';
    if(chg<=-1) return 'dn-2';
    return 'dn-1';
  }

  // 날짜 라벨 (MM/DD)
  const dateLbls = dates.map(d=>{
    const [,m,day]=d.split('-');
    return `${m}/${day}`;
  });

  // 주말 구분
  const isWeekend = dates.map(d=>{
    const dw=new Date(d).getDay(); return dw===0||dw===6;
  });

  let html=`<div class="hist-wrap">
  <div class="hist-header">
    <div>
      <span style="font-family:'Bebas Neue';font-size:20px;color:var(--txt)">📅 가격 히스토리</span>
      <span style="font-size:12px;color:var(--muted);margin-left:10px">최근 30일 · 일별 등락률 히트맵</span>
    </div>
    <div class="hist-legend">
      <span class="hist-legend-cell" style="background:rgba(0,200,100,.9)"></span>+3%↑
      <span class="hist-legend-cell" style="background:rgba(0,200,100,.6)"></span>+1%↑
      <span class="hist-legend-cell" style="background:rgba(0,200,100,.3)"></span>+0%↑
      <span class="hist-legend-cell" style="background:rgba(255,255,255,.06)"></span>보합
      <span class="hist-legend-cell" style="background:rgba(255,60,60,.25)"></span>-0%↓
      <span class="hist-legend-cell" style="background:rgba(255,60,60,.6)"></span>-1%↓
      <span class="hist-legend-cell" style="background:rgba(255,60,60,.9)"></span>-3%↓
    </div>
  </div>

  ${noData?`<div style="padding:32px;text-align:center;color:var(--muted)">
    <div style="font-size:28px;margin-bottom:10px">📊</div>
    <div style="font-size:14px;font-weight:600;color:var(--txt);margin-bottom:6px">아직 가격 이력이 없습니다</div>
    <div style="font-size:12px">이메일 알림을 발송하거나 설정 페이지에서 서버 동기화하면<br>매일 가격이 자동 기록됩니다.</div>
    <a href="/settings/email" style="display:inline-block;margin-top:14px;padding:8px 18px;background:rgba(0,255,136,.12);color:var(--green);border:1px solid rgba(0,255,136,.3);border-radius:7px;text-decoration:none;font-size:12px;font-weight:700">📧 이메일 설정 → 지금 발송</a>
  </div>`:``}

  <!-- 날짜 헤더 -->
  <div class="hist-date-row">
    <div class="hist-corner">종목</div>
    <div class="hist-dates">
      ${dateLbls.map((lbl,i)=>`<div class="hist-date-lbl" style="${isWeekend[i]?'opacity:.35':''}">
        ${lbl.replace('/',`<br>`)}</div>`).join('')}
    </div>
  </div>`;

  // 종목별 행
  stocks.forEach((s,si)=>{
    const tracked = s.days_tracked||0;
    const totalChg = s.total_chg||0;
    const totalColor = totalChg>=0?'var(--green)':'var(--red)';
    const totalSign  = totalChg>=0?'+':'';
    const scanLabel  = {surge:'🚀',high:'🏆',volscan:'📈',cross:'🔀',fvgscan:'🧲',breakout:'🌅'}[s.scanType]||'⭐';

    const cells = s.series.map((p,i)=>{
      if(!p.has_data) return `<div class="hist-cell nodata" title="${dates[i]} 데이터 없음">·</div>`;
      const cls=cellClass(p.chg);
      const sign=(p.chg||0)>=0?'+':'';
      const pct=p.chg!=null?`${sign}${p.chg.toFixed(1)}%`:'—';
      const priceStr=p.price?`${Math.round(p.price).toLocaleString()}원`:'';
      return `<div class="hist-cell ${cls}" title="${dates[i]}\n${priceStr}\n${pct}">${(p.chg||0)===0&&p.has_data?'—':pct}</div>`;
    }).join('');

    html+=`<div class="hist-stock-row" style="animation-delay:${si*20}ms">
      <div class="hist-stock-info">
        <span style="font-size:16px">${scanLabel}</span>
        <div style="flex:1;min-width:0">
          <div class="hist-stock-name">${s.name}</div>
          <div class="hist-stock-sub">${s.code} · ${s.market||''}</div>
        </div>
        <div class="hist-stock-pct" style="color:${totalColor}">${totalSign}${totalChg.toFixed(1)}%</div>
        <div style="font-size:10px;color:var(--muted2);text-align:right;font-family:'JetBrains Mono';min-width:30px">${tracked}일</div>
      </div>
      <div class="hist-cells">${cells}</div>
    </div>`;
  });

  html+=`</div>`;
  wrap.innerHTML=html;
}

/* ══════════════════════════════════════
   🧲 FVG 재진입 스캐너
══════════════════════════════════════ */
function doFvgScan(){
  const btn=document.getElementById('btn-fvgscan'); btn.disabled=true;
  const mkt=document.getElementById('fs-mkt').value;
  const tf=document.getElementById('fs-tf').value;
  const lookback=parseInt(document.getElementById('fs-lookback').value)||60;
  const minsize=parseFloat(document.getElementById('fs-minsize').value)||0.5;
  const tolerance=parseFloat(document.getElementById('fs-tolerance').value)||10;
  const limit=parseInt(document.getElementById('fs-lim').value)||50;

  hideAll();
  document.getElementById('fvgscan-res').style.display='block';
  document.getElementById('fvgscan-res').innerHTML=loading(
    `FVG 재진입 종목 스캔 중...<br><span style="font-size:11px;color:var(--muted2)">${mkt==='ALL'?'전체':mkt} · ${tf}봉 기준 · FVG 구간 내 가격 탐색 (2~4분 소요)</span>`);

  fetch('/fvg_scan',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({market:mkt,tf,lookback,min_fvg_pct:minsize,tolerance_pct:tolerance,limit})})
    .then(r=>{if(r.status===401){location.href='/login';return null;} return r.json();})
    .then(d=>{btn.disabled=false; if(d) renderFvgScan(d);})
    .catch(e=>{btn.disabled=false;
      document.getElementById('fvgscan-res').innerHTML=`<div class="empty">❌ ${e}</div>`;});
}

function renderFvgScan(d){
  const wrap=document.getElementById('fvgscan-res');
  if(d.error){wrap.innerHTML=`<div class="empty">❌ ${d.error}</div>`;return}
  const results=d.results||[];
  if(!results.length){
    wrap.innerHTML='<div class="empty">현재 FVG 재진입 중인 종목이 없습니다.<br><span style="font-size:11px;color:var(--muted2)">FVG 최소 크기를 낮추거나 허용 범위를 넓혀보세요</span></div>';
    return;
  }
  const bull=results.filter(r=>r.fvg_type==='bull').length;
  const bear=results.filter(r=>r.fvg_type==='bear').length;

  let html=`<div class="fs-table-wrap">
  <div class="fs-summary">
    <span><span class="fs-sum-lbl">발견</span><span class="fs-sum-val" style="color:#06b6d4">${results.length}종목</span></span>
    <span><span class="fs-sum-lbl">📈 상승FVG</span><span class="fs-sum-val" style="color:var(--green)">${bull}개</span></span>
    <span><span class="fs-sum-lbl">📉 하락FVG</span><span class="fs-sum-val" style="color:var(--red)">${bear}개</span></span>
    <span class="fs-tf-badge">${d.tf}봉</span>
    <span style="margin-left:auto;font-size:11px;color:var(--muted)">FVG 내 진입 깊이 낮을수록 초입 신호</span>
  </div>
  <div class="fs-thead">
    <span></span><span>순위</span><span>코드</span><span>종목명</span>
    <span style="text-align:right">현재가</span>
    <span style="text-align:right">등락률</span>
    <span style="text-align:right">FVG범위</span>
    <span style="text-align:right">진입깊이</span>
    <span style="text-align:center">FVG종류</span>
  </div>`;

  results.forEach((r,i)=>{
    const isBull=r.fvg_type==='bull';
    const rowCls=isBull?'bull':'bear';
    const chgColor=r.change>=0?'var(--green)':'var(--red)';
    const mktBadge=r.market==='코스피'?`<span class="vs-badge kp">KOSPI</span>`:`<span class="vs-badge kq">KOSDAQ</span>`;
    const depth=r.fill_pct||0;
    const depthCls=depth<20?'hot':depth<50?'warm':'ok';
    const actualDepthLabel=depth<20?'🔥 초입':depth<50?'⚡ 중간':'✅ 깊음';

    const fsStar=makeStar(r.code,r.name,r.market||'',r.price||0,r.change||0,'fvgscan');
    html+=`<div class="fs-row ${rowCls}" style="animation-delay:${Math.min(i,30)*10}ms">
      <span style="text-align:center">${fsStar}</span><span class="fs-rank">${i+1}</span>
      <span class="fs-code">${r.code}</span>
      <span class="fs-name">${r.name} ${mktBadge}</span>
      <span class="fs-price">${r.price.toLocaleString()}원</span>
      <span class="fs-chg" style="color:${chgColor}">${r.change>=0?'+':''}${r.change.toFixed(1)}%</span>
      <span class="fs-fvg" style="color:${isBull?'var(--green)':'var(--red)'}">
        ${r.fvg_low.toLocaleString()}<br>~${r.fvg_high.toLocaleString()}
      </span>
      <span class="fs-near ${depthCls}">${actualDepthLabel}<br>${depth.toFixed(0)}%</span>
      <span class="fs-type"><span class="fs-type-badge ${rowCls}">${isBull?'📈 상승FVG':'📉 하락FVG'}</span></span>
    </div>`;
  });
  html+=`</div>
  <div style="margin-top:12px;padding:11px 16px;background:rgba(6,182,212,.05);border:1px solid rgba(6,182,212,.2);border-radius:9px;font-size:11px;color:var(--muted);line-height:1.8">
    💡 <strong style="color:#06b6d4">FVG 재진입 매매법:</strong>
    상승FVG 구간 내 진입 → 상단 돌파 시 추가 상승 기대 · 하단 이탈 시 손절 |
    진입깊이 <strong style="color:#fb923c">초입(0~20%)</strong>일수록 강한 신호 |
    EMA/추세 방향과 같은 방향의 FVG만 매매 권장
  </div>`;

  wrap.innerHTML=html;
}

function clearFvgScanRes(){
  document.getElementById('fvgscan-res').style.display='none';
  document.getElementById('fvgscan-res').innerHTML='';
}

/* ══════════════════════════════════════
   🌅 상승 준비 구간 스캐너
══════════════════════════════════════ */
function doBreakout(){
  const btn=document.getElementById('btn-breakout'); btn.disabled=true;
  const mkt=document.getElementById('bo-mkt').value;
  const tf=document.getElementById('bo-tf').value;
  const lookback=parseInt(document.getElementById('bo-lookback').value)||60;
  const minScore=parseInt(document.getElementById('bo-minscore').value)||4;
  const limit=parseInt(document.getElementById('bo-lim').value)||50;

  hideAll();
  document.getElementById('breakout-res').style.display='block';
  document.getElementById('breakout-res').innerHTML=loading(
    `상승 준비 구간 스캔 중...<br><span style="font-size:11px;color:var(--muted2)">${mkt==='ALL'?'전체':mkt} · ${tf}봉 · 거래량 상위 종목 (1~2분 소요)</span>`);

  fetch('/breakout_scan',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({market:mkt,tf,lookback,min_score:minScore,limit})})
    .then(r=>{if(r.status===401){location.href='/login';return null;} return r.json();})
    .then(d=>{btn.disabled=false; if(d) renderBreakout(d);})
    .catch(e=>{btn.disabled=false;
      document.getElementById('breakout-res').innerHTML=`<div class="empty">❌ ${e}</div>`;});
}

function renderBreakout(d){
  const wrap=document.getElementById('breakout-res');
  if(d.error){wrap.innerHTML=`<div class="empty">❌ ${d.error}</div>`;return}
  const results=d.results||[];
  if(!results.length){
    wrap.innerHTML='<div class="empty">조건을 만족하는 종목이 없습니다.<br><span style="font-size:11px;color:var(--muted2)">최소 점수를 낮추거나 분석 기간을 늘려보세요</span></div>';
    return;
  }

  const COND_LABELS = {
    fvg_stack:  {label:'FVG 누적', ok:'var(--green)'},
    rsi_ok:     {label:'RSI 회복', ok:'var(--green)'},
    low_stable: {label:'저점 안정', ok:'var(--green)'},
    ema_near:   {label:'EMA 수렴', ok:'var(--green)'},
    vol_shrink: {label:'거래량↓',  ok:'var(--green)'},
  };

  let html=`<div class="bo-table-wrap">
  <div class="bo-summary">
    <span style="font-family:'Bebas Neue';font-size:22px;color:#ffa502">🌅 상승 준비 ${results.length}종목</span>
    <span style="font-size:11px;color:var(--muted)">점수 높은 순 · ${d.tf}봉 · 거래량 상위 ${d.scanned}종목 스캔</span>
    <span style="margin-left:auto;display:flex;gap:8px">
      <button onclick="clearBoRes()" style="padding:4px 10px;border-radius:5px;background:rgba(255,71,87,.12);color:var(--red);border:1px solid rgba(255,71,87,.3);cursor:pointer;font-size:11px;font-weight:700">🗑 지우기</button>
    </span>
  </div>
  <div class="bo-thead">
    <span></span><span>점수</span><span>코드</span><span>종목명</span>
    <span style="text-align:right">현재가</span>
    <span style="text-align:right">등락률</span>
    <span style="text-align:right">RSI</span>
    <span style="text-align:center">FVG수</span>
    <span style="text-align:right">EMA이격</span>
    <span>충족 조건</span>
  </div>`;

  results.forEach((r,i)=>{
    const scoreCls=['','s1','s2','s3','s4','s5'][Math.min(r.score,5)];
    const scoreColor=['','#7d8590','#fb923c','#ffa502','#4ade80','#00ff88'][Math.min(r.score,5)];
    const fillW = (r.score/5*100).toFixed(0);
    const chgColor=r.change>=0?'var(--green)':'var(--red)';
    const rsiColor=r.rsi<35?'var(--green)':r.rsi<50?'#4ade80':r.rsi<60?'var(--yellow)':'var(--muted)';
    const fvgCls=r.fvg_count>=3?'many':r.fvg_count>=2?'some':'few';
    const mktBadge=r.market==='코스피'?`<span class="vs-badge kp">KOSPI</span>`:`<span class="vs-badge kq">KOSDAQ</span>`;
    const boStar=makeStar(r.code,r.name,r.market||'',r.price||0,r.change||0,'breakout');
    const tags=(r.conditions||[]).map(c=>{
      const info=COND_LABELS[c]||{label:c,ok:'var(--green)'};
      return `<span class="bo-tag ok">${info.label}</span>`;
    }).join('');

    html+=`<div class="bo-row" style="animation-delay:${Math.min(i,30)*12}ms">
      <span style="text-align:center">${boStar}</span>
      <span>
        <span class="bo-score ${scoreCls}">${r.score}/5</span>
        <div class="bo-score-bar"><div class="bo-score-fill" style="width:${fillW}%;background:${scoreColor}"></div></div>
      </span>
      <span class="fav-code">${r.code}</span>
      <span class="fav-name">${r.name} ${mktBadge}</span>
      <span class="fav-price">${r.price.toLocaleString()}원</span>
      <span class="fav-chg" style="color:${chgColor}">${r.change>=0?'+':''}${r.change.toFixed(1)}%</span>
      <span class="bo-rsi" style="color:${rsiColor}">${r.rsi.toFixed(0)}</span>
      <span class="bo-fvg ${fvgCls}">${r.fvg_count}개</span>
      <span style="font-family:'JetBrains Mono';font-size:11px;text-align:right;color:${Math.abs(r.ema_gap)<1?'var(--green)':'var(--muted)'}">${r.ema_gap>=0?'+':''}${r.ema_gap.toFixed(1)}%</span>
      <span class="bo-cond-tags">${tags}</span>
    </div>`;
  });

  html+=`</div>
  <div style="margin-top:12px;padding:11px 16px;background:rgba(255,165,2,.05);border:1px solid rgba(255,165,2,.2);border-radius:9px;font-size:11px;color:var(--muted);line-height:1.9">
    💡 <strong style="color:#ffa502">상승 준비 구간 매매법:</strong>
    5점 만점 = 모든 조건 충족 → 가장 강한 신호 |
    <strong>FVG 누적</strong> + <strong>RSI 회복</strong> 동시 충족이 핵심 |
    반드시 캔들 방향 확인 후 진입 (음봉에서 매수 금지) |
    손절은 최근 저점 하단
  </div>`;

  wrap.innerHTML=html;
}

function clearBoRes(){
  document.getElementById('breakout-res').style.display='none';
  document.getElementById('breakout-res').innerHTML='';
}

/* ══════════════════════════════════════
   ⚡ 고확신 신호 스캐너
══════════════════════════════════════ */
function doHcs(){
  const btn=document.getElementById('btn-hcs'); btn.disabled=true;
  const mkt=document.getElementById('hcs-mkt').value;
  const tf=document.getElementById('hcs-tf').value;
  const lookback=parseInt(document.getElementById('hcs-lookback').value)||60;
  const limit=parseInt(document.getElementById('hcs-lim').value)||30;
  hideAll();
  document.getElementById('hcs-res').style.display='block';
  document.getElementById('hcs-res').innerHTML=loading(
    `⚡ 고확신 신호 스캔 중...<br><span style="font-size:11px;color:var(--muted2)">3가지 복합 조건 분석 · 거래량 상위 종목 (1~2분)</span>`);
  fetch('/hcs_scan',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({market:mkt,tf,lookback,limit})})
    .then(r=>{if(r.status===401){location.href='/login';return null;} return r.json();})
    .then(d=>{btn.disabled=false; if(d) renderHcs(d);})
    .catch(e=>{btn.disabled=false;
      document.getElementById('hcs-res').innerHTML=`<div class="empty">❌ ${e}</div>`;});
}

function renderHcs(d){
  const wrap=document.getElementById('hcs-res');
  if(d.error){wrap.innerHTML=`<div class="empty">❌ ${d.error}</div>`;return;}
  const results=d.results||[];
  if(!results.length){
    wrap.innerHTML=`<div class="empty">현재 고확신 신호 종목이 없습니다.<br><span style="font-size:11px;color:var(--muted2)">시장 상황에 따라 조건을 만족하는 종목이 적을 수 있습니다</span></div>`;
    return;
  }

  const COMBO = {
    A:{label:'전략A · 급등+FVG+거래량',cls:'a',badge:'a',
       desc:'급등 중 FVG 구간 재진입 + 매수거래량 급증 — 세력 매집 가능성 최고',
       winRate:'75%'},
    B:{label:'전략B · 상승준비+RSI회복',cls:'b',badge:'b',
       desc:'5개 조건 4점↑ + RSI 30~55 회복 — 저점 탈출 초입',
       winRate:'70%'},
    C:{label:'전략C · 25%이격+눌림목',cls:'c',badge:'c',
       desc:'22이평 3%↑ 이격 + 거래량 감소 — 되돌림 직전 최적 타이밍',
       winRate:'80%'},
  };

  const countA=results.filter(r=>r.combo==='A').length;
  const countB=results.filter(r=>r.combo==='B').length;
  const countC=results.filter(r=>r.combo==='C').length;

  let html=`<div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:16px">
    <span style="font-family:'Bebas Neue';font-size:22px;color:var(--txt)">⚡ 고확신 신호 ${results.length}종목</span>
    <span class="hcs-badge a">전략A ${countA}개</span>
    <span class="hcs-badge b">전략B ${countB}개</span>
    <span class="hcs-badge c">전략C ${countC}개</span>
    <span style="margin-left:auto;font-size:11px;color:var(--muted)">스캔 ${d.scanned}종목 분석</span>
  </div>`;

  results.forEach((r,i)=>{
    const combo=COMBO[r.combo]||COMBO.A;
    const chg=r.change||0;
    const chgColor=chg>=0?'var(--green)':'var(--red)';
    const mktBadge=r.market==='코스피'?`<span class="vs-badge kp">KOSPI</span>`:`<span class="vs-badge kq">KOSDAQ</span>`;
    const star=makeStar(r.code,r.name,r.market||'',r.price||0,chg,'hcs');

    // 조건 카드
    const conds=(r.conditions||[]).map(c=>`
      <div class="hcs-cond met">
        <div class="hcs-cond-icon">${c.icon}</div>
        <div class="hcs-cond-name">${c.name}</div>
        <div class="hcs-cond-val">${c.val||''}</div>
      </div>`).join('');

    html+=`<div class="hcs-card combo-${combo.cls}" style="animation-delay:${i*30}ms">
      <div class="hcs-card-head">
        ${star}
        <span class="hcs-badge ${combo.badge}">${combo.label}</span>
        <div>
          <div class="hcs-name">${r.name} ${mktBadge}</div>
          <div class="hcs-sub">${r.code} · ${r.market||''}</div>
        </div>
        <div style="margin-left:auto;text-align:right">
          <div class="hcs-price">${(r.price||0).toLocaleString()}원</div>
          <div class="hcs-chg" style="color:${chgColor}">${chg>=0?'+':''}${chg.toFixed(1)}%</div>
        </div>
      </div>
      <div class="hcs-body">${conds}</div>
      <div class="hcs-summary">
        <div>
          <div class="hcs-strategy-label">${combo.desc}</div>
        </div>
        <div style="text-align:right">
          <div class="hcs-win-rate">${combo.winRate}</div>
          <div class="hcs-win-lbl">예상 승률</div>
        </div>
      </div>
    </div>`;
  });

  html+=`<div style="margin-top:14px;padding:12px 16px;background:rgba(251,146,60,.05);border:1px solid rgba(251,146,60,.2);border-radius:9px;font-size:11px;color:var(--muted);line-height:1.9">
    ⚠️ <strong>매매 원칙:</strong> 고확신 신호여도 반드시 <strong style="color:var(--txt)">음봉에서는 매수 금지</strong> · 손절은 항상 사전 설정 · 자금의 10% 이내 진입
  </div>`;

  wrap.innerHTML=html;
}

/* ══════════════════════════════════════
   🔔 조건 알림 시스템
══════════════════════════════════════ */
const ALERT_KEY='krscan_alerts';

function loadAlerts(){
  try{return JSON.parse(localStorage.getItem(ALERT_KEY)||'[]');}catch(e){return [];}
}
function saveAlerts(arr){
  try{localStorage.setItem(ALERT_KEY,JSON.stringify(arr));}catch(e){}
  // 서버 동기화
  fetch('/api/alerts/sync',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({alerts:arr})}).catch(()=>{});
}
function deleteAlert(id){
  const arr=loadAlerts().filter(a=>a.id!==id);
  saveAlerts(arr);
  renderAlertList();
}
function toggleAlert(id){
  const arr=loadAlerts();
  const idx=arr.findIndex(a=>a.id===id);
  if(idx>=0) arr[idx].active=!arr[idx].active;
  saveAlerts(arr);
  renderAlertList();
}
function renderAlertList(){
  const wrap=document.getElementById('alert-list-wrap');
  if(!wrap) return;
  const alerts=loadAlerts();
  if(!alerts.length){
    wrap.innerHTML='<div style="color:var(--muted);font-size:13px;padding:16px 0">저장된 알림이 없습니다.</div>';
    return;
  }
  const COND_LABELS={
    surge_pct:'급등률',rsi_below:'RSI 이하',rsi_above:'RSI 이상',
    ma_gap_below:'이평 이격 하락',fvg_retest:'FVG 재진입',
    vol_surge:'거래량 급증',breakout_score:'상승준비 점수'
  };
  wrap.innerHTML=alerts.map(a=>`
    <div class="alert-card ${a.active?'alert-active':'alert-inactive'}">
      <div class="alert-dot ${a.active?'on':'off'}"></div>
      <div style="flex:1">
        <div class="alert-name">${a.code} ${a.name||''}</div>
        <div class="alert-cond">${COND_LABELS[a.cond_type]||a.cond_type} ${a.operator} ${a.value}${a.unit||''}</div>
      </div>
      <button class="alert-toggle ${a.active?'on':'off'}" onclick="toggleAlert('${a.id}')">${a.active?'중지':'활성화'}</button>
      <button onclick="deleteAlert('${a.id}')" style="padding:4px 8px;border-radius:5px;border:1px solid var(--bd);background:transparent;color:var(--muted);cursor:pointer;font-size:12px;margin-left:4px">✕</button>
    </div>`).join('');
}
function addAlert(){
  const code=document.getElementById('al-code').value.trim().padStart(6,'0');
  const name=document.getElementById('al-name').value.trim();
  const cond=document.getElementById('al-cond').value;
  const op=document.getElementById('al-op').value;
  const val=parseFloat(document.getElementById('al-val').value);
  const mkt=document.getElementById('al-mkt').value;
  const UNITS={surge_pct:'%',rsi_below:'',rsi_above:'',ma_gap_below:'%',vol_surge:'배',breakout_score:'점',fvg_retest:''};
  if(!code||isNaN(val)){alert('종목 코드와 조건값을 입력하세요');return;}
  const alerts=loadAlerts();
  alerts.unshift({id:Date.now()+'',code,name,market:mkt,cond_type:cond,
    operator:op,value:val,unit:UNITS[cond]||'',active:true,
    created:new Date().toLocaleDateString('ko-KR')});
  saveAlerts(alerts);
  renderAlertList();
  document.getElementById('al-code').value='';
  document.getElementById('al-name').value='';
  document.getElementById('al-val').value='';
}

/* ══════════════════════════════════════
   🎯 고승률 복합 조건 스캐너
══════════════════════════════════════ */
let hpCombo='a';
function hpSelect(c){
  hpCombo=c;
  ['a','b','c'].forEach(x=>{
    const el=document.getElementById('hp-card-'+x);
    if(!el) return;
    el.className='hp-card'+(x===c?' sel sel-'+x:'');
  });
}

function doHighProb(){
  const btn=document.getElementById('btn-highprob'); btn.disabled=true;
  const mkt=document.getElementById('hp-mkt').value;
  const lim=parseInt(document.getElementById('hp-lim').value)||50;
  const labels={a:'콤보 A — 모멘텀 급등',b:'콤보 B — 상승 준비',c:'콤보 C — 25% 눌림목'};
  hideAll();
  document.getElementById('highprob-res').style.display='block';
  document.getElementById('highprob-res').innerHTML=loading(
    `🎯 고승률 자리 스캔 중 (${labels[hpCombo]})<br><span style="font-size:11px;color:var(--muted2)">거래량 상위 종목 복합 조건 분석 중... (1~3분)</span>`);
  fetch('/highprob_scan',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({combo:hpCombo,market:mkt,limit:lim})})
    .then(r=>{if(r.status===401){location.href='/login';return null;} return r.json();})
    .then(d=>{btn.disabled=false; if(d) renderHighProb(d);})
    .catch(e=>{btn.disabled=false;
      document.getElementById('highprob-res').innerHTML=`<div class="empty">❌ ${e}</div>`;});
}

function renderHighProb(d){
  const wrap=document.getElementById('highprob-res');
  if(d.error){wrap.innerHTML=`<div class="empty">❌ ${d.error}</div>`;return}
  const results=d.results||[];
  const comboColors={a:'var(--green)',b:'#a78bfa',c:'#fb923c'};
  const comboColor=comboColors[d.combo]||'var(--green)';
  const comboName={a:'콤보 A',b:'콤보 B',c:'콤보 C'}[d.combo]||'';
  const COND_CLS={surge:'surge',fvg_retest:'fvg',vol_surge:'vol',
    breakout_ready:'breakout',retracement:'retr'};
  const COND_LBL={surge:'급등',fvg_retest:'FVG',vol_surge:'거래량',
    breakout_ready:'상승준비',rsi_ok:'RSI',low_stable:'저점↑',
    vol_shrink:'거래량↓',ema_near:'EMA수렴',retracement:'이격'};

  if(!results.length){
    wrap.innerHTML='<div class="empty">현재 조건을 충족하는 종목이 없습니다.<br><span style="font-size:11px;color:var(--muted2)">시장 조건에 따라 결과가 달라집니다. 다른 콤보를 시도해보세요.</span></div>';
    return;
  }

  let html=`<div class="hp-table-wrap">
  <div class="vs-summary" style="border-color:${comboColor}20;background:${comboColor}08">
    <span style="font-family:'Bebas Neue';font-size:22px;color:${comboColor}">🎯 ${comboName} — ${results.length}종목</span>
    <span style="font-size:12px;color:var(--muted)">${d.scanned}종목 분석 · 복합 조건 충족 종목만 표시</span>
    <span style="margin-left:auto;display:flex;gap:6px">
      <button onclick="clearHPRes()" style="padding:4px 10px;border-radius:5px;background:rgba(255,71,87,.12);color:var(--red);border:1px solid rgba(255,71,87,.3);cursor:pointer;font-size:11px;font-weight:700">🗑 지우기</button>
    </span>
  </div>
  <div class="hp-thead">
    <span></span><span>점수</span><span>코드</span><span>종목명</span>
    <span style="text-align:right">현재가</span>
    <span style="text-align:right">등락률</span>
    <span style="text-align:right">RSI</span>
    <span style="text-align:right">이격</span>
    <span>충족 조건</span>
    <span></span>
  </div>`;

  results.forEach((r,i)=>{
    const sc=r.score||0;
    const scCls=sc>=3?'s3':sc>=2?'s2':'s1';
    const scColor=sc>=3?'var(--green)':sc>=2?'var(--yellow)':'var(--muted)';
    const chgColor=(r.change||0)>=0?'var(--green)':'var(--red)';
    const mktBadge=r.market==='코스피'?'<span class="vs-badge kp">KOSPI</span>':'<span class="vs-badge kq">KOSDAQ</span>';
    const hpStar=makeStar(r.code,r.name,r.market||'',r.price||0,r.change||0,'highprob');
    const condBadges=(r.conditions||[]).map(c=>{
      const cls=COND_CLS[c]||'vol'; const lbl=COND_LBL[c]||c;
      return `<span class="hp-b ${cls}">${lbl}</span>`;
    }).join('');
    const rsiColor=(r.rsi||50)<35?'var(--green)':(r.rsi||50)<50?'#4ade80':'var(--muted)';
    const gapColor=Math.abs(r.ema_gap||0)<1?'var(--green)':Math.abs(r.ema_gap||0)<3?'var(--yellow)':'#fb923c';

    html+=`<div class="hp-row" style="animation-delay:${Math.min(i,30)*12}ms${sc>=3?';border-left:3px solid var(--green)':''}">
      <span style="text-align:center">${hpStar}</span>
      <span><span class="hp-score ${scCls}" style="color:${scColor}">${sc}</span></span>
      <span class="vs-code">${r.code}</span>
      <span class="fav-name">${r.name} ${mktBadge}</span>
      <span class="fav-price">${(r.price||0).toLocaleString()}원</span>
      <span class="fav-chg" style="color:${chgColor};text-align:right">${(r.change||0)>=0?'+':''}${(r.change||0).toFixed(1)}%</span>
      <span style="font-family:'JetBrains Mono';font-size:11px;text-align:right;color:${rsiColor}">${r.rsi?r.rsi.toFixed(0):'—'}</span>
      <span style="font-family:'JetBrains Mono';font-size:11px;text-align:right;color:${gapColor}">${r.ema_gap!=null?((r.ema_gap>=0?'+':'')+r.ema_gap.toFixed(1)+'%'):'—'}</span>
      <span class="hp-badge-wrap">${condBadges}</span>
      <span></span>
    </div>`;
  });
  html+=`</div>`;
  wrap.innerHTML=html;
}

function clearHPRes(){
  document.getElementById('highprob-res').style.display='none';
  document.getElementById('highprob-res').innerHTML='';
}

/* ══════════════════════════════════════
   🔔 조건 저장 자동 알림
══════════════════════════════════════ */
function openAlertPage(){
  window.location.href='/alerts';
}

/* ══════════════════════════════════════
   ⭐ 즐겨찾기 시스템
══════════════════════════════════════ */
const FAV_KEY = 'krscan_favorites';
const SCAN_LABELS = {
  surge:   {label:'🚀 급등 종목',  cls:'surge'},
  high:    {label:'🏆 전고점 돌파', cls:'high'},
  volscan: {label:'📈 매수거래량',  cls:'volscan'},
  cross:   {label:'🔀 교집합',      cls:'cross'},
  fvgscan: {label:'🧲 FVG 재진입',  cls:'fvgscan'},
};

function syncFavsToServer(){
  const favs=loadFavs();
  fetch('/api/fav/sync',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({favs})}).catch(()=>{});
}
function loadFavs(){
  try{ return JSON.parse(localStorage.getItem(FAV_KEY)||'[]'); }
  catch(e){ return []; }
}
function saveFavs(arr){
  try{ localStorage.setItem(FAV_KEY, JSON.stringify(arr)); }catch(e){}
}
function isFaved(code){
  return loadFavs().some(f=>f.code===code);
}
function toggleFav(code, name, market, price, change, scanType){
  let favs = loadFavs();
  const idx = favs.findIndex(f=>f.code===code);
  if(idx>=0){
    favs.splice(idx,1);
    saveFavs(favs);
    // update all star buttons with this code
    document.querySelectorAll(`.fav-star[data-code="${code}"]`).forEach(b=>{
      b.textContent='☆'; b.classList.remove('active'); b.title='즐겨찾기 추가';
    });
    return false;
  } else {
    favs.unshift({code,name,market,price,change,scanType,
      savedAt: new Date().toLocaleString('ko-KR')});
    saveFavs(favs);
    document.querySelectorAll(`.fav-star[data-code="${code}"]`).forEach(b=>{
      b.textContent='★'; b.classList.add('active'); b.title='즐겨찾기 해제';
    });
    return true;
  }
}
function makeStar(code, name, market, price, change, scanType){
  const faved = isFaved(code);
  return `<span class="fav-star${faved?' active':''}" data-code="${code}"
    title="${faved?'즐겨찾기 해제':'즐겨찾기 추가'}"
    onclick="event.stopPropagation();toggleFav('${code}','${name.replace(/'/g,"\\'")}','${market}',${price},${change},'${scanType}')"
  >${faved?'★':'☆'}</span>`;
}

function renderFavList(){
  const wrap = document.getElementById('fav-res');
  const favs = loadFavs();
  if(!favs.length){
    wrap.innerHTML=`<div class="fav-empty">
      <span class="fav-empty-icon">⭐</span>
      <div style="font-size:14px;font-weight:600;color:var(--txt);margin-bottom:8px">즐겨찾기가 없습니다</div>
      <div style="font-size:12px;color:var(--muted)">스캔 결과에서 ☆ 버튼을 클릭해 종목을 추가하세요</div>
    </div>`;
    return;
  }

  // Group by scan type
  const groups = {};
  favs.forEach(f=>{
    const k = f.scanType||'기타';
    if(!groups[k]) groups[k]=[];
    groups[k].push(f);
  });

  let html=`<div style="background:var(--s2);border:1px solid var(--bd);border-radius:12px;overflow:hidden">
  <div class="fav-header">
    <span style="font-family:'Bebas Neue';font-size:22px;color:#ffd700">⭐ 즐겨찾기 ${favs.length}종목</span>
    <div style="display:flex;gap:8px">
      <button onclick="exportFavCSV()" style="padding:5px 12px;border-radius:6px;background:var(--s3);border:1px solid var(--bd);color:var(--muted);cursor:pointer;font-size:11px">⬇ CSV</button>
      <button onclick="clearAllFavs()" style="padding:5px 12px;border-radius:6px;background:rgba(255,71,87,.1);border:1px solid rgba(255,71,87,.2);color:var(--red);cursor:pointer;font-size:11px">🗑 전체 삭제</button>
    </div>
  </div>
  <div class="fav-thead">
    <span></span><span>코드</span><span>종목명</span>
    <span style="text-align:right">저장가</span>
    <span style="text-align:right">등락률</span>
    <span>스캔 종류</span>
    <span>저장 시간</span>
    <span></span>
  </div>`;

  favs.forEach((f,i)=>{
    const meta = SCAN_LABELS[f.scanType]||{label:f.scanType||'기타',cls:'surge'};
    const chgColor = (f.change||0)>=0?'var(--green)':'var(--red)';
    html+=`<div class="fav-row" style="animation-delay:${Math.min(i,30)*15}ms">
      <span class="fav-star active" data-code="${f.code}"
        onclick="event.stopPropagation();toggleFav('${f.code}','${(f.name||'').replace(/'/g,"\\'")}','${f.market||''}',${f.price||0},${f.change||0},'${f.scanType||''}');renderFavList()"
        title="즐겨찾기 해제">★</span>
      <span class="fav-code">${f.code}</span>
      <span class="fav-name">${f.name} <span style="font-size:10px;color:var(--muted)">${f.market||''}</span></span>
      <span class="fav-price">${(f.price||0).toLocaleString()}원</span>
      <span class="fav-chg" style="color:${chgColor}">${(f.change||0)>=0?'+':''}${(f.change||0).toFixed(1)}%</span>
      <span><span class="fav-from ${meta.cls}">${meta.label}</span></span>
      <span class="fav-time">${f.savedAt||''}</span>
      <span class="fav-del" onclick="delFav('${f.code}');renderFavList()" title="삭제">✕</span>
    </div>`;
  });

  html+='</div>';
  wrap.innerHTML=html;
}

function delFav(code){
  const favs=loadFavs().filter(f=>f.code!==code);
  saveFavs(favs);
  document.querySelectorAll(`.fav-star[data-code="${code}"]`).forEach(b=>{
    b.textContent='☆'; b.classList.remove('active'); b.title='즐겨찾기 추가';
  });
}

function clearAllFavs(){
  if(!confirm('즐겨찾기를 모두 삭제할까요?')) return;
  saveFavs([]);
  // reset all star buttons
  document.querySelectorAll('.fav-star.active').forEach(b=>{
    b.textContent='☆'; b.classList.remove('active');
  });
  renderFavList();
}

function exportFavCSV(){
  const favs=loadFavs();
  if(!favs.length) return;
  const cols=['코드','종목명','시장','저장가','등락률','스캔종류','저장시간'];
  const rows=[cols.join(','),...favs.map(f=>[
    f.code,f.name,f.market||'',f.price||0,(f.change||0).toFixed(2),
    (SCAN_LABELS[f.scanType]||{label:f.scanType}).label,f.savedAt||''
  ].join(','))];
  const blob=new Blob(['\uFEFF'+rows.join('\n')],{type:'text/csv;charset=utf-8'});
  const a=document.createElement('a');a.href=URL.createObjectURL(blob);
  a.download=`krscan_favorites_${new Date().toISOString().slice(0,10)}.csv`;a.click();
}
/* ══════════════════════════════════════
   📐 25% 되돌림 전략 (김직선)
══════════════════════════════════════ */
let retrAsset='crypto', retrTicker='BTC-USD', retrCls_='btc';

function retrType(t){
  retrAsset=t; const isC=t==='crypto';
  document.getElementById('retr-ac').className='at-btn'+(isC?' on-crypto':'');
  document.getElementById('retr-as').className='at-btn'+(!isC?' on-stock':'');
  document.getElementById('retr-c').style.display=isC?'':'none';
  document.getElementById('retr-s').style.display=isC?'none':'';
  document.getElementById('retr-sm').style.display=isC?'none':'';
  document.getElementById('retr-unit').textContent=isC?'USD':'원';
  document.getElementById('retr-acct').value=isC?'10000':'10000000';
}
function retrCoin(tk,cls){
  retrTicker=tk; retrCls_=cls;
  ['btc','eth','xrp'].forEach(c=>{
    const b=document.getElementById('retr-'+c); if(!b)return;
    b.className='cb'+(c===cls?' sel-'+c:'');
  });
}
function doRetr(){
  let ticker, isCrypto;
  const tf=document.getElementById('retr-tf').value;
  const maPeriod=parseInt(document.getElementById('retr-ma').value)||22;
  const acct=parseFloat(document.getElementById('retr-acct').value)||10000;
  const risk=parseFloat(document.getElementById('retr-risk').value)||1;
  if(retrAsset==='crypto'){
    ticker=retrTicker; isCrypto=true;
  } else {
    const code=document.getElementById('retr-code').value.trim().padStart(6,'0');
    const mkt=document.getElementById('retr-mkt').value;
    ticker=code+'.'+mkt; isCrypto=false;
  }
  hideAll();
  document.getElementById('retr-res').style.display='block';
  document.getElementById('retr-res').innerHTML=loading(`25% 되돌림 분석 중...<br><span style="font-size:11px;color:var(--muted2)">${ticker} · MA${maPeriod}</span>`);
  fetch('/retracement',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({ticker,is_crypto:isCrypto,tf,ma_period:maPeriod,account:acct,risk_pct:risk})})
    .then(r=>r.json()).then(d=>renderRetr(d))
    .catch(e=>{document.getElementById('retr-res').innerHTML=`<div class="empty">❌ ${e}</div>`});
}

function renderRetr(d){
  const wrap=document.getElementById('retr-res');
  if(d.error){wrap.innerHTML=`<div class="empty">❌ ${d.error}</div>`;return}
  const fmt=v=>d.is_crypto?(v>=1?'$'+v.toLocaleString(undefined,{maximumFractionDigits:2}):'$'+v.toFixed(4)):v.toLocaleString()+'원';
  const fmtAmt=v=>d.is_crypto?'$'+v.toLocaleString(undefined,{maximumFractionDigits:0}):Math.round(v/10000).toLocaleString()+'만원';
  const chg=d.change||0; const div=d.divergence||{}; const sig=d.signal||{};
  const iconMap={'BTC-USD':'₿','ETH-USD':'Ξ','XRP-USD':'✕'};
  const icon=iconMap[d.ticker]||'📈';
  const sigCls=sig.type==='LONG'?(sig.strength>=2?'strong-long':'long'):sig.type==='SHORT'?(sig.strength>=2?'strong-short':'short'):'neutral';
  const absDivPct=Math.abs(div.pct||0);
  const barColor=(div.pct||0)<0?'var(--green)':'var(--red)';
  const barW=Math.min(100,absDivPct*8);
  const stA=d.strategy_a||{}; const stB=d.strategy_b||{};
  const wrData=[{r:25,wr:90,c:'#00ff88'},{r:50,wr:60,c:'#ffa502'},{r:75,wr:20,c:'#ff6b35'},{r:100,wr:10,c:'#ff4757'}];

  wrap.innerHTML=`
  <div class="retr-hero">
    <div>
      <div class="retr-title">${icon} ${d.name||d.ticker.split('-')[0]}</div>
      <div style="font-size:11px;color:var(--muted);margin-top:3px">${d.ticker} · MA${d.ma_period} · ${d.tf}봉</div>
    </div>
    <div style="margin-left:auto;text-align:right">
      <div class="retr-price">${fmt(d.price||0)}<span class="retr-chg" style="color:${chg>=0?'var(--green)':'var(--red)'}">&nbsp;${chg>=0?'+':''}${chg.toFixed(2)}%</span></div>
      <div style="font-size:11px;color:var(--muted);font-family:'JetBrains Mono';margin-top:4px">MA${d.ma_period}: ${fmt(d.ma_val||0)}</div>
    </div>
  </div>

  <div class="retr-diverge-card">
    <div class="retr-diverge-title">📏 이평선 괴리율 — 진입 신호 강도</div>
    <div class="retr-diverge-label">
      <span style="color:var(--muted)">MA${d.ma_period} 대비 현재 위치</span>
      <span style="font-family:'JetBrains Mono';font-weight:700;color:${(div.pct||0)<0?'var(--green)':'var(--red)'}">${(div.pct||0)>=0?'+':''}${(div.pct||0).toFixed(2)}%</span>
    </div>
    <div class="retr-diverge-bar"><div class="retr-diverge-fill" style="width:${barW}%;background:${barColor}"></div></div>
    <div style="display:flex;justify-content:space-between;font-size:10px;color:var(--muted2);margin-top:4px">
      <span>0% (이평선)</span>
      <span style="color:${absDivPct>=3?'#fb923c':'var(--muted)'}">${absDivPct>=3?'🔥 진입 구간!':absDivPct>=1.5?'⚡ 형성 중':'⏳ 대기'}</span>
      <span>10%+</span>
    </div>
    <div style="margin-top:10px;padding:9px 13px;background:var(--s1);border-radius:7px;font-size:12px;color:var(--muted)">
      ${absDivPct>=3?`<strong style="color:#fb923c">✅ 진입 적합!</strong> 이평선에서 ${absDivPct.toFixed(1)}% 이격 — 되돌림 발생 확률 높음. 지금이 타이밍입니다.`:
        absDivPct>=1.5?`<strong style="color:var(--yellow)">⚡ 이격 형성 중</strong> (${absDivPct.toFixed(1)}%) — 더 큰 이격이 발생할 때까지 대기하면 성공률 ↑`:
        `<strong style="color:var(--muted)">⏳ 이격 부족</strong> (${absDivPct.toFixed(1)}%) — 이평선 근접 상태. 급등락 후 재분석 권장`}
    </div>
  </div>

  <div class="retr-signal ${sigCls}">
    <div style="font-size:40px;flex-shrink:0">${sig.type==='LONG'?'📈':sig.type==='SHORT'?'📉':'⚖️'}</div>
    <div>
      <div class="retr-signal-title">${sig.title||'중립 — 대기'}</div>
      <div class="retr-signal-sub">${sig.desc||'이평선 괴리 발생 시 신호 생성'}</div>
    </div>
  </div>

  <div style="font-size:11px;color:var(--muted);letter-spacing:2px;text-transform:uppercase;margin-bottom:8px">📊 되돌림 비율별 승률 (김직선 나스닥 백테스팅)</div>
  <div class="retr-winrate">
    ${wrData.map((w,i)=>`<div class="retr-wr-box" style="animation-delay:${i*60}ms${w.r===25?';border:1px solid rgba(0,255,136,.35)':''}">
      <div class="retr-wr-pct" style="color:${w.c}">${w.wr}%</div>
      <div class="retr-wr-lbl">${w.r}% 되돌림</div>
      <div class="retr-wr-fill" style="background:${w.c};width:${w.wr}%"></div>
      ${w.r===25?'<div style="font-size:9px;color:#00ff88;margin-top:5px;font-weight:700">★ 추천 익절</div>':''}
    </div>`).join('')}
  </div>

  <div style="font-size:11px;color:var(--muted);letter-spacing:2px;text-transform:uppercase;margin-bottom:8px">🎯 전략별 타점 계산</div>
  <div class="retr-levels-grid">
    <div class="retr-level-card strategy-a">
      <div class="retr-lc-head"><div class="retr-lc-title">전략 A · 단타</div><span class="retr-lc-badge">승률 90%</span></div>
      <div class="retr-lv"><span class="retr-lv-lbl">📍 현재가</span><span class="retr-lv-val" style="color:var(--blue)">${fmt(d.price)}</span></div>
      <div class="retr-lv"><span class="retr-lv-lbl">🎯 진입가</span><span class="retr-lv-val" style="color:var(--yellow)">${fmt(stA.entry||0)}</span></div>
      <div class="retr-lv"><span class="retr-lv-lbl">✅ 익절 (25%)</span><span><span class="retr-lv-val" style="color:var(--green)">${fmt(stA.tp||0)}</span><span class="retr-lv-pct" style="background:rgba(0,255,136,.1);color:var(--green)">+${(stA.tp_pct||0).toFixed(2)}%</span></span></div>
      <div class="retr-lv"><span class="retr-lv-lbl">🛑 손절 (1%)</span><span><span class="retr-lv-val" style="color:var(--red)">${fmt(stA.sl||0)}</span><span class="retr-lv-pct" style="background:rgba(255,71,87,.1);color:var(--red)">-${(stA.sl_pct||0).toFixed(2)}%</span></span></div>
      <div style="margin-top:10px;padding:9px 11px;background:rgba(0,255,136,.04);border-radius:7px;font-size:11px;color:var(--muted)">
        수량 <strong style="color:var(--txt)">${d.is_crypto?((stA.shares||0).toFixed(4)+' '+d.ticker.split('-')[0]):(stA.shares||0).toLocaleString()+'주'}</strong>
        &nbsp;·&nbsp;투자 <strong style="color:var(--blue)">${fmtAmt(stA.invest||0)}</strong>
        &nbsp;·&nbsp;최대손실 <strong style="color:var(--red)">${fmtAmt(stA.risk_amt||0)}</strong>
      </div>
    </div>
    <div class="retr-level-card strategy-b">
      <div class="retr-lc-head"><div class="retr-lc-title">전략 B · 스윙전환</div><span class="retr-lc-badge">75%+ 반등 시</span></div>
      <div class="retr-lv"><span class="retr-lv-lbl">🔁 1차 익절 (25%)</span><span><span class="retr-lv-val" style="color:#4ade80">${fmt(stB.tp1||0)}</span><span class="retr-lv-pct" style="background:rgba(74,222,128,.1);color:#4ade80">+${(stB.tp1_pct||0).toFixed(2)}%</span></span></div>
      <div class="retr-lv"><span class="retr-lv-lbl">♻️ 눌림목 재진입</span><span class="retr-lv-val" style="color:var(--yellow)">${fmt(stB.reentry||0)}</span></div>
      <div class="retr-lv"><span class="retr-lv-lbl">🚀 2차 목표 (1:2)</span><span><span class="retr-lv-val" style="color:var(--green)">${fmt(stB.tp2||0)}</span><span class="retr-lv-pct" style="background:rgba(0,255,136,.1);color:var(--green)">+${(stB.tp2_pct||0).toFixed(2)}%</span></span></div>
      <div class="retr-lv"><span class="retr-lv-lbl">💎 3차 목표 (1:3)</span><span><span class="retr-lv-val" style="color:#34d399">${fmt(stB.tp3||0)}</span><span class="retr-lv-pct" style="background:rgba(52,211,153,.1);color:#34d399">+${(stB.tp3_pct||0).toFixed(2)}%</span></span></div>
      <div class="retr-lv"><span class="retr-lv-lbl">🛑 손절 (직전 저점)</span><span class="retr-lv-val" style="color:var(--red)">${fmt(stB.sl||0)}</span></div>
      <div style="margin-top:10px;padding:9px 11px;background:rgba(167,139,250,.04);border-radius:7px;font-size:11px;color:var(--muted)">
        <strong style="color:#a78bfa">75%+ 반등 확인 후 눌림목 재진입!</strong> 직전 저점 손절 → 손익비 1:2, 1:3
      </div>
    </div>
  </div>

  <div class="retr-guide-box">
    📌 <strong>김직선 25% 법칙 — 실전 원칙 5가지</strong><br>
    ① <span class="hl">이평선 이격이 클수록</span> 되돌림 확률 ↑ — 급등락 직후 이격 3%+ 가 최적 타이밍<br>
    ② <span class="hl">25% 구간 익절 생활화</span> — 100% 먹으려다 물리는 것이 계좌 파괴의 원인<br>
    ③ 반등이 <span class="hl">50% 미만</span>이면 저점 재갱신 가능성 높음 → 미련 없이 청산<br>
    ④ 반등이 <span class="hl">75% 이상</span>이면 추세전환 신호 → 전략 B로 스위칭<br>
    ⑤ 손실은 <span class="hl">항상 1% 이내</span> — 한 달간 25% 익절 훈련 → 복리로 계좌 성장
  </div>`;
}
</script>
</body>
</html>
"""

# ── /scan ────────────────────────────────────────────────
@app.route("/scan", methods=["POST"])
@login_required
def scan():
    params=request.json; scan_type=params.get("type"); market=params.get("market","ALL")
    limit=int(params.get("limit",100)); today=datetime.today(); today_str=today.strftime("%Y-%m-%d")
    mkts=[("KOSPI","코스피"),("KOSDAQ","코스닥")] if market=="ALL" else [("KOSPI","코스피")] if market=="KOSPI" else [("KOSDAQ","코스닥")]
    results=[]
    if scan_type=="surge":
        days=int(params.get("period",30)); minpct=float(params.get("minpct",30))
        from_dt=(today-timedelta(days=days)).strftime("%Y-%m-%d")
        for mkt,label in mkts:
            listing=fdr.StockListing(mkt)[["Code","Name"]]
            for _,row in listing.iterrows():
                try:
                    df=fdr.DataReader(row["Code"],from_dt,today_str)
                    if len(df)<2: continue
                    sp_,ep=float(df["Close"].iloc[0]),float(df["Close"].iloc[-1])
                    pct=(ep-sp_)/sp_*100
                    if pct>=minpct: results.append({"코드":row["Code"],"종목명":row["Name"],"시장":label,"등락률":round(pct,2),"시작가":int(sp_),"현재가":int(ep)})
                except: continue
        results.sort(key=lambda x:x["등락률"],reverse=True)
    elif scan_type=="high":
        pd_=int(params.get("period",365)); rd=int(params.get("recent",30))
        from_dt=(today-timedelta(days=pd_)).strftime("%Y-%m-%d"); rec_dt=(today-timedelta(days=rd)).strftime("%Y-%m-%d")
        for mkt,label in mkts:
            listing=fdr.StockListing(mkt)[["Code","Name"]]
            for _,row in listing.iterrows():
                try:
                    df=fdr.DataReader(row["Code"],from_dt,today_str)
                    if len(df)<20: continue
                    df.index=pd.to_datetime(df.index)
                    before,recent=df[df.index<rec_dt],df[df.index>=rec_dt]
                    if len(before)<5 or len(recent)<3: continue
                    ph,rh=float(before["High"].max()),float(recent["High"].max()); cur=float(df["Close"].iloc[-1])
                    if rh>ph: results.append({"코드":row["Code"],"종목명":row["Name"],"시장":label,"돌파율":round((rh-ph)/ph*100,2),"전고점":int(ph),"돌파고점":int(rh),"현재가":int(cur)})
                except: continue
        results.sort(key=lambda x:x["돌파율"],reverse=True)
    return jsonify({"results":results[:limit]})


# ── /fvg ────────────────────────────────────────────────
@app.route("/fvg", methods=["POST"])
@login_required
def fvg():
    params = request.json
    ticker = params.get("ticker")
    is_crypto = params.get("is_crypto", False)
    try:
        stk, price, change, name = get_yf_data(ticker)
        tf_data = fetch_all_tf(stk if stk else ticker, ticker if is_crypto else None)
        return jsonify({"ticker":ticker,"name":name,"price":round(price,6),"change":change,"timeframes":tf_data})
    except Exception as e:
        return jsonify({"error":str(e)})


# ── /setup ───────────────────────────────────────────────
@app.route("/setup", methods=["POST"])
@login_required
def setup():
    params    = request.json
    ticker    = params.get("ticker")
    is_crypto = params.get("is_crypto", False)
    account   = float(params.get("account", 10_000_000))
    risk_pct  = float(params.get("risk_pct", 1.0))
    try:
        stk, price, change, name = get_yf_data(ticker)
        tf_data = fetch_all_tf(stk if stk else ticker, ticker if is_crypto else None)
        setups = {}
        for tf_key, fvg_info in tf_data.items():
            setups[tf_key] = calc_setup(fvg_info, price, account, risk_pct)
        return jsonify({"ticker":ticker,"name":name,"price":round(price,6),"change":change,
                        "account":account,"risk_pct":risk_pct,"is_crypto":is_crypto,"setups":setups})
    except Exception as e:
        return jsonify({"error":str(e)})


@app.route("/")
@login_required
def index():
    from flask import Response
    return Response(HTML, mimetype='text/html')


LOGIN_HTML = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>KRSCAN — 로그인</title>
<link href="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=Noto+Sans+KR:wght@300;400;500;700&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#07090d;color:#e6edf3;font-family:'Noto Sans KR',sans-serif;min-height:100vh;display:flex;align-items:center;justify-content:center}
body::before{content:'';position:fixed;inset:0;background-image:linear-gradient(rgba(0,255,136,.015) 1px,transparent 1px),linear-gradient(90deg,rgba(0,255,136,.015) 1px,transparent 1px);background-size:48px 48px;pointer-events:none}
.wrap{width:440px;padding:0 20px}
.logo{font-family:'Bebas Neue';font-size:52px;letter-spacing:4px;background:linear-gradient(135deg,#00ff88,#06b6d4);-webkit-background-clip:text;-webkit-text-fill-color:transparent;text-align:center;margin-bottom:4px}
.logo-sub{text-align:center;font-size:11px;color:#7d8590;letter-spacing:3px;text-transform:uppercase;margin-bottom:36px}
.card{background:#0d1117;border:1px solid #21262d;border-radius:14px;padding:32px 36px}
.tab-row{display:flex;background:#07090d;border-radius:8px;padding:3px;margin-bottom:28px;gap:3px}
.tab{flex:1;padding:10px;text-align:center;border-radius:6px;cursor:pointer;font-size:13px;font-weight:600;color:#7d8590;transition:all .2s}
.tab.active{background:#1c2128;color:#e6edf3}
label{display:block;font-size:11px;color:#7d8590;letter-spacing:1px;text-transform:uppercase;margin-bottom:6px;margin-top:16px}
label:first-of-type{margin-top:0}
input{width:100%;padding:11px 14px;background:#161b22;border:1px solid #30363d;border-radius:8px;color:#e6edf3;font-size:14px;font-family:'Noto Sans KR';transition:border-color .2s;outline:none}
input:focus{border-color:#00ff88}
.btn{width:100%;padding:12px;background:linear-gradient(135deg,#00ff88,#06b6d4);border:none;border-radius:8px;color:#000;font-size:14px;font-weight:700;cursor:pointer;margin-top:24px;font-family:'Noto Sans KR';transition:opacity .2s}
.btn:hover{opacity:.9}
.msg{margin-top:14px;padding:10px 14px;border-radius:7px;font-size:13px;text-align:center}
.msg.error{background:rgba(255,71,87,.1);color:#ff4757;border:1px solid rgba(255,71,87,.2)}
.msg.success{background:rgba(0,255,136,.1);color:#00ff88;border:1px solid rgba(0,255,136,.2)}
.msg.pending{background:rgba(255,165,2,.1);color:#ffa502;border:1px solid rgba(255,165,2,.2)}
.form-section{display:none}
.form-section.active{display:block}
.hint{font-size:11px;color:#484f58;text-align:center;margin-top:20px;line-height:1.7}
</style>
</head>
<body>
<div class="wrap">
  <div class="logo">KRSCAN</div>
  <div class="logo-sub">Korean Stock Screener v4</div>
  <div class="card">
    <div class="tab-row">
      <div class="tab active" onclick="switchTab('login')">🔑 로그인</div>
      <div class="tab" onclick="switchTab('register')">✨ 회원가입</div>
    </div>
    <div class="form-section active" id="sec-login">
      <form method="POST" action="/login">
        <label>아이디</label>
        <input type="text" name="username" placeholder="아이디 입력" required autocomplete="username">
        <label>비밀번호</label>
        <input type="password" name="password" placeholder="비밀번호 입력" required autocomplete="current-password">
        <button class="btn" type="submit">로그인</button>
      </form>
      {% if login_error %}<div class="msg error">{{ login_error }}</div>{% endif %}
    </div>
    <div class="form-section" id="sec-register">
      <form method="POST" action="/register">
        <label>아이디 (영문/숫자/_, 4~20자)</label>
        <input type="text" name="username" placeholder="사용할 아이디" required minlength="4" maxlength="20" pattern="[a-zA-Z0-9_]+">
        <label>비밀번호 (6자 이상)</label>
        <input type="password" name="password" placeholder="비밀번호" required minlength="6" autocomplete="new-password">
        <label>비밀번호 확인</label>
        <input type="password" name="password2" placeholder="비밀번호 재입력" required autocomplete="new-password">
        <button class="btn" type="submit">회원가입 신청</button>
      </form>
      {% if reg_error %}<div class="msg error">{{ reg_error }}</div>{% endif %}
      {% if reg_success %}<div class="msg pending">{{ reg_success }}</div>{% endif %}
    </div>
    <div class="hint">
      가입 후 <strong style="color:#ffa502">관리자 승인</strong>이 필요합니다<br>
      승인 후 로그인 가능 · 개인 운영 서버 · 상업적 이용 금지
    </div>
  </div>
</div>
<script>
function switchTab(t){
  document.querySelectorAll('.tab').forEach((el,i)=>el.classList.toggle('active',i===(t==='login'?0:1)));
  document.querySelectorAll('.form-section').forEach((el,i)=>el.classList.toggle('active',i===(t==='login'?0:1)));
}
{% if reg_error or reg_success %}switchTab('register');{% endif %}
</script>
</body>
</html>"""

ADMIN_HTML = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>KRSCAN 관리자</title>
<link href="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=Noto+Sans+KR:wght@300;400;500;700&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#07090d;color:#e6edf3;font-family:'Noto Sans KR',sans-serif;min-height:100vh;padding:30px 20px}
.header{display:flex;align-items:center;gap:16px;margin-bottom:30px;padding-bottom:20px;border-bottom:1px solid #21262d}
.logo{font-family:'Bebas Neue';font-size:36px;color:#00ff88;letter-spacing:3px}
.header a{margin-left:auto;font-size:12px;color:#7d8590;text-decoration:none;padding:6px 14px;border:1px solid #30363d;border-radius:6px}
.header a:hover{color:#e6edf3}
.wrap{max-width:900px;margin:0 auto}
.section{background:#0d1117;border:1px solid #21262d;border-radius:12px;padding:20px 24px;margin-bottom:20px}
.sec-title{font-size:13px;font-weight:700;letter-spacing:2px;text-transform:uppercase;margin-bottom:16px;display:flex;align-items:center;gap:8px}
.sec-title.pending{color:#ffa502}
.sec-title.approved{color:#00ff88}
.sec-title.all{color:#58a6ff}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;padding:9px 12px;font-size:10px;color:#7d8590;letter-spacing:1px;text-transform:uppercase;border-bottom:1px solid #21262d}
td{padding:10px 12px;border-bottom:1px solid #161b22}
tr:last-child td{border-bottom:none}
tr:hover td{background:rgba(255,255,255,.02)}
.badge{font-size:10px;font-weight:700;padding:2px 8px;border-radius:10px;font-family:JetBrains Mono}
.badge.pending{background:rgba(255,165,2,.15);color:#ffa502;border:1px solid rgba(255,165,2,.25)}
.badge.approved{background:rgba(0,255,136,.12);color:#00ff88;border:1px solid rgba(0,255,136,.25)}
.badge.admin{background:rgba(88,166,255,.15);color:#58a6ff;border:1px solid rgba(88,166,255,.25)}
.btn-approve{padding:5px 14px;border-radius:6px;border:none;background:rgba(0,255,136,.15);color:#00ff88;cursor:pointer;font-size:12px;font-weight:700;font-family:'Noto Sans KR';border:1px solid rgba(0,255,136,.3);transition:all .2s}
.btn-approve:hover{background:rgba(0,255,136,.25)}
.btn-reject{padding:5px 14px;border-radius:6px;border:none;background:rgba(255,71,87,.1);color:#ff4757;cursor:pointer;font-size:12px;font-weight:700;font-family:'Noto Sans KR';border:1px solid rgba(255,71,87,.2);transition:all .2s;margin-left:6px}
.btn-reject:hover{background:rgba(255,71,87,.2)}
.btn-revoke{padding:4px 10px;border-radius:5px;border:1px solid rgba(255,165,2,.2);background:rgba(255,165,2,.08);color:#ffa502;cursor:pointer;font-size:11px;font-family:'Noto Sans KR'}
.empty{color:#484f58;font-size:13px;padding:16px 12px}
.stats{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:20px}
.stat-box{background:#0d1117;border:1px solid #21262d;border-radius:10px;padding:14px 18px;flex:1;min-width:100px}
.stat-num{font-family:'Bebas Neue';font-size:32px;line-height:1}
.stat-lbl{font-size:10px;color:#7d8590;letter-spacing:1px;text-transform:uppercase;margin-top:4px}
.back-btn{display:inline-block;margin-bottom:20px;font-size:12px;color:#7d8590;text-decoration:none;padding:7px 14px;border:1px solid #21262d;border-radius:6px}
.back-btn:hover{color:#e6edf3}
</style>
</head>
<body>
<div class="wrap">
  <div class="header">
    <div class="logo">KRSCAN 관리자</div>
    <span style="font-size:12px;color:#58a6ff">👑 {{ admin }}</span>
    <a href="/">← 스캐너로</a>
    <a href="/logout" style="color:#ff4757;border-color:rgba(255,71,87,.3)">로그아웃</a>
  </div>

  <div class="stats">
    <div class="stat-box"><div class="stat-num" style="color:#ffa502">{{ pending|length }}</div><div class="stat-lbl">승인 대기</div></div>
    <div class="stat-box"><div class="stat-num" style="color:#00ff88">{{ approved|length }}</div><div class="stat-lbl">승인된 계정</div></div>
    <div class="stat-box"><div class="stat-num" style="color:#7d8590">{{ total }}</div><div class="stat-lbl">전체 계정</div></div>
  </div>

  {% if pending %}
  <div class="section">
    <div class="sec-title pending">⏳ 승인 대기 ({{ pending|length }})</div>
    <table>
      <tr><th>아이디</th><th>상태</th><th>작업</th></tr>
      {% for u in pending %}
      <tr>
        <td style="font-family:JetBrains Mono;font-weight:700">{{ u }}</td>
        <td><span class="badge pending">대기 중</span></td>
        <td>
          <form method="POST" action="/admin/approve" style="display:inline">
            <input type="hidden" name="username" value="{{ u }}">
            <button class="btn-approve" type="submit">✅ 승인</button>
          </form>
          <form method="POST" action="/admin/reject" style="display:inline">
            <input type="hidden" name="username" value="{{ u }}">
            <button class="btn-reject" type="submit">❌ 거절</button>
          </form>
        </td>
      </tr>
      {% endfor %}
    </table>
  </div>
  {% else %}
  <div class="section">
    <div class="sec-title pending">⏳ 승인 대기</div>
    <div class="empty">대기 중인 가입 신청이 없습니다.</div>
  </div>
  {% endif %}

  <div class="section">
    <div class="sec-title approved">✅ 승인된 계정 ({{ approved|length }})</div>
    {% if approved %}
    <table>
      <tr><th>아이디</th><th>권한</th><th>작업</th></tr>
      {% for u in approved %}
      <tr>
        <td style="font-family:JetBrains Mono;font-weight:700">{{ u }}</td>
        <td>{% if u == admin %}<span class="badge admin">👑 관리자</span>{% else %}<span class="badge approved">일반</span>{% endif %}</td>
        <td>
          {% if u != admin %}
          <form method="POST" action="/admin/revoke" style="display:inline">
            <input type="hidden" name="username" value="{{ u }}">
            <button class="btn-revoke" type="submit">권한 취소</button>
          </form>
          {% else %}<span style="color:#484f58;font-size:11px">변경 불가</span>{% endif %}
        </td>
      </tr>
      {% endfor %}
    </table>
    {% else %}<div class="empty">승인된 계정이 없습니다.</div>{% endif %}
  </div>

  {% if msg %}<div style="margin-top:16px;padding:12px 16px;background:rgba(0,255,136,.1);border:1px solid rgba(0,255,136,.25);border-radius:8px;color:#00ff88;font-size:13px">{{ msg }}</div>{% endif %}
</div>
</body>
</html>"""

@app.route("/login", methods=["GET","POST"])
def login_page():
    if session.get("user"):
        return redirect(url_for("index"))
    login_error = None
    if request.method == "POST":
        username = request.form.get("username","").strip()
        password = request.form.get("password","")
        users = load_users()
        u = users.get(username)
        if not u:
            login_error = "❌ 존재하지 않는 아이디입니다"
        elif u.get("pw") != hash_pw(password):
            login_error = "❌ 비밀번호가 틀렸습니다"
        else:
            # langpoon은 항상 approved로 자동 수정
            if username == ADMIN_ID and u.get("status") != "approved":
                u["status"] = "approved"
                users[username] = u
                save_users(users)

            status = u.get("status", "")
            if status == "pending":
                login_error = "⏳ 관리자 승인 대기 중입니다."
            elif status == "rejected":
                login_error = "❌ 가입이 거절되었습니다."
            elif status in ("approved", "admin") or username == ADMIN_ID:
                session["user"]     = username
                session["is_admin"] = (username == ADMIN_ID)
                session.permanent   = True
                return redirect(url_for("index"))
            else:
                # status 필드 없는 구버전 계정 → approved로 처리
                if username == ADMIN_ID:
                    u["status"] = "approved"
                    users[username] = u
                    save_users(users)
                    session["user"]     = username
                    session["is_admin"] = True
                    session.permanent   = True
                    return redirect(url_for("index"))
                login_error = "❌ 계정 상태를 확인해주세요 (관리자 문의)"
    return render_template_string(LOGIN_HTML, login_error=login_error,
                                  reg_error=None, reg_success=None)

@app.route("/register", methods=["POST"])
def register():
    import re
    username  = request.form.get("username","").strip()
    password  = request.form.get("password","")
    password2 = request.form.get("password2","")
    reg_error = reg_success = None

    if not re.match(r'^[a-zA-Z0-9_]{4,20}$', username):
        reg_error = "❌ 아이디는 영문/숫자/언더스코어 4~20자"
    elif len(password) < 6:
        reg_error = "❌ 비밀번호는 6자 이상"
    elif password != password2:
        reg_error = "❌ 비밀번호가 일치하지 않습니다"
    else:
        users = load_users()
        if username in users:
            reg_error = "❌ 이미 존재하는 아이디입니다"
        else:
            # 관리자는 즉시 approved
            status = "approved" if username == ADMIN_ID else "pending"
            users[username] = {"pw": hash_pw(password), "status": status}
            save_users(users)
            if username == ADMIN_ID:
                reg_success = f"✅ 관리자 계정 생성 완료. 바로 로그인하세요!"
            else:
                reg_success = f"✅ 가입 신청 완료! 관리자 승인 후 로그인 가능합니다."

    return render_template_string(LOGIN_HTML, login_error=None,
                                  reg_error=reg_error, reg_success=reg_success)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login_page"))

@app.route("/api/me")
def api_me():
    user = session.get("user","")
    return jsonify({"user": user, "is_admin": user == ADMIN_ID})

# ── 알림 조건 저장 ──────────────────────────────────────
ALERTS_FILE = os.path.join(DATA_DIR, "alert_conditions.json")

def load_alerts():
    if os.path.exists(ALERTS_FILE):
        try:
            with open(ALERTS_FILE,"r",encoding="utf-8") as f: return json.load(f)
        except: pass
    return []

def save_alerts(alerts):
    with open(ALERTS_FILE,"w",encoding="utf-8") as f:
        json.dump(alerts, f, ensure_ascii=False, indent=2)

@app.route("/api/alerts/sync", methods=["POST"])
@login_required
def alerts_sync():
    alerts = request.json.get("alerts",[])
    save_alerts(alerts)
    return jsonify({"ok": True, "count": len(alerts)})

# ── 즐겨찾기 서버 동기화 ─────────────────────────────────
@app.route("/api/fav/sync", methods=["POST"])
@login_required
def fav_sync():
    """브라우저 localStorage 즐겨찾기 → 서버 저장"""
    favs = request.json.get("favs", [])
    save_server_favs(favs)
    return jsonify({"ok": True, "count": len(favs)})

@app.route("/api/fav/list", methods=["GET"])
@login_required
def fav_list():
    return jsonify(load_server_favs())

@app.route("/api/fav/history", methods=["GET"])
@login_required
def fav_history():
    """즐겨찾기 종목별 30일 가격 이력 반환"""
    favs  = load_server_favs()   # sync 후 server_favorites.json에서 읽음
    hist  = load_price_history()
    today = datetime.now()
    # 최근 30일 날짜 목록
    dates = [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(29,-1,-1)]

    result = []
    for f in favs:
        code      = f.get("code","")
        he        = hist.get(code, {})
        h_data    = he.get("history", {})
        init_p    = he.get("initial_price") or f.get("price", 0)
        init_date = he.get("initial_date", f.get("savedAt","")[:10] if f.get("savedAt") else "")

        # 날짜별 변동률 계산 (전일 대비)
        day_changes = {}
        sorted_dates = sorted(h_data.keys())
        for i, d in enumerate(sorted_dates):
            price = h_data[d]
            if i > 0:
                prev_p = h_data[sorted_dates[i-1]]
                day_changes[d] = round((price - prev_p) / prev_p * 100, 2) if prev_p else 0
            else:
                day_changes[d] = 0

        # 최신 가격
        latest_price = h_data[sorted_dates[-1]] if sorted_dates else init_p
        total_chg    = round((latest_price - init_p) / init_p * 100, 2) if init_p and latest_price else 0

        # 30일 시리즈
        series = []
        for d in dates:
            if d in h_data:
                series.append({"date":d,"price":h_data[d],"chg":day_changes.get(d,0),"has_data":True})
            else:
                series.append({"date":d,"price":None,"chg":None,"has_data":False})

        result.append({
            "code":         code,
            "name":         f.get("name",""),
            "market":       f.get("market",""),
            "scanType":     f.get("scanType",""),
            "init_price":   init_p,
            "init_date":    init_date,
            "latest_price": latest_price,
            "total_chg":    total_chg,
            "days_tracked": len(h_data),
            "series":       series,
        })

    return jsonify({"dates": dates, "stocks": result})


# ── 이메일 설정 ─────────────────────────────────────────
EMAIL_SETTINGS_HTML = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>KRSCAN — 알림 설정</title>
<link href="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=Noto+Sans+KR:wght@300;400;500;700&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#07090d;color:#e6edf3;font-family:'Noto Sans KR',sans-serif;padding:30px 20px}
.wrap{max-width:680px;margin:0 auto}
.header{display:flex;align-items:center;gap:16px;margin-bottom:28px;padding-bottom:18px;border-bottom:1px solid #21262d}
.logo{font-family:'Bebas Neue';font-size:32px;color:#00ff88;letter-spacing:3px}
.header a{margin-left:auto;font-size:12px;color:#7d8590;text-decoration:none;padding:6px 14px;border:1px solid #30363d;border-radius:6px}
.section{background:#0d1117;border:1px solid #21262d;border-radius:12px;padding:22px 26px;margin-bottom:18px}
.sec-title{font-size:12px;font-weight:700;letter-spacing:2px;text-transform:uppercase;color:#58a6ff;margin-bottom:18px;display:flex;align-items:center;gap:8px}
label{display:block;font-size:11px;color:#7d8590;letter-spacing:1px;text-transform:uppercase;margin-bottom:6px;margin-top:14px}
label:first-of-type{margin-top:0}
input,select{width:100%;padding:10px 14px;background:#161b22;border:1px solid #30363d;border-radius:7px;color:#e6edf3;font-size:13px;font-family:'Noto Sans KR';outline:none;transition:border-color .2s}
input:focus,select:focus{border-color:#00ff88}
input[type=checkbox]{width:auto;margin-right:8px;cursor:pointer}
.check-row{display:flex;align-items:center;font-size:13px;color:#e6edf3;margin-top:10px;cursor:pointer}
.btn-primary{padding:11px 24px;background:linear-gradient(135deg,#00ff88,#06b6d4);border:none;border-radius:8px;color:#000;font-size:13px;font-weight:700;cursor:pointer;font-family:'Noto Sans KR';margin-top:18px;transition:opacity .2s}
.btn-primary:hover{opacity:.9}
.btn-test{padding:9px 20px;background:var(--s2,#161b22);border:1px solid #30363d;border-radius:7px;color:#58a6ff;font-size:12px;font-weight:700;cursor:pointer;font-family:'Noto Sans KR';margin-left:10px;transition:all .2s}
.btn-test:hover{border-color:#58a6ff}
.msg{margin-top:12px;padding:10px 14px;border-radius:7px;font-size:13px}
.msg.ok{background:rgba(0,255,136,.1);color:#00ff88;border:1px solid rgba(0,255,136,.2)}
.msg.err{background:rgba(255,71,87,.1);color:#ff4757;border:1px solid rgba(255,71,87,.2)}
.schedule-row{display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;margin-top:8px}
.sch-item{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:12px;text-align:center}
.sch-time{font-family:'JetBrains Mono';font-size:20px;font-weight:700;color:#06b6d4}
.sch-lbl{font-size:11px;color:#7d8590;margin-top:4px}
.hint{font-size:11px;color:#484f58;line-height:1.7;margin-top:10px}
</style>
</head>
<body>
<div class="wrap">
  <div class="header">
    <div class="logo">KRSCAN</div>
    <span style="font-size:13px;color:#e6edf3">📧 이메일 알림 설정</span>
    <a href="/">← 스캐너로</a>
  </div>

  <div class="section">
    <div class="sec-title">📬 알림 발송 시간</div>
    <div class="schedule-row">
      <div class="sch-item"><div class="sch-time">09:05</div><div class="sch-lbl">장 시작 직후</div></div>
      <div class="sch-item"><div class="sch-time">13:00</div><div class="sch-lbl">점심 체크</div></div>
      <div class="sch-item"><div class="sch-time">15:35</div><div class="sch-lbl">장 마감 후</div></div>
    </div>
    <div class="hint">⚠️ 서버가 실행 중인 동안만 발송됩니다. 장이 없는 주말에는 발송되지 않습니다.<br>
    💡 <strong>지금 발송</strong> 버튼으로 언제든 수동 발송 가능합니다. 처음 발송 시 오늘 가격이 이력에 기록됩니다.</div>
  </div>

  <div class="section">
    <div class="sec-title">⚙️ SMTP 설정 (Gmail 권장)</div>
    <form method="POST" action="/settings/email">
      <label>발신 이메일 주소</label>
      <input type="email" name="smtp_user" value="__SMTP_USER__" placeholder="your@gmail.com" required>
      <label>앱 비밀번호 <span style="color:#484f58;font-size:10px">(Gmail → 구글계정 → 2단계인증 → 앱 비밀번호)</span></label>
      <input type="password" name="smtp_pass" value="" placeholder="앱 비밀번호 16자리">
      <label>수신 이메일</label>
      <input type="email" name="to_email" value="__TO_EMAIL__" placeholder="받을 이메일" required>
      <label>SMTP 서버</label>
      <input type="text" name="smtp_host" value="__SMTP_HOST__">
      <label>포트</label>
      <input type="number" name="smtp_port" value="__SMTP_PORT__" style="width:120px">

      <div style="margin-top:18px;display:flex;flex-wrap:wrap;gap:8px">
        <label class="check-row">
          <input type="checkbox" name="enabled" value="1" __ENABLED__>
          알림 활성화
        </label>
      </div>
      <div style="display:flex;align-items:center;flex-wrap:wrap;gap:8px;margin-top:4px">
        <button class="btn-primary" type="submit">💾 저장</button>
        <button class="btn-test" type="button" onclick="testEmail()">📧 테스트 발송</button>
        <button class="btn-test" id="btn-send-now" type="button" onclick="sendNow()" style="background:rgba(0,255,136,.08);border-color:rgba(0,255,136,.3);color:#1d9e75">📤 지금 발송</button>
        <button class="btn-test" type="button" onclick="diagnose()" style="background:rgba(88,166,255,.08);border-color:rgba(88,166,255,.3);color:#58a6ff">🔍 진단</button>
      </div>
    </form>
    __MSG_BLOCK__
  </div>

  <div class="section">
    <div class="sec-title" style="color:#ffa502">🔔 조건 알림 설정</div>
    <p style="font-size:12px;color:#7d8590;margin-bottom:6px;line-height:1.8">
      종목 코드만 입력하면 <strong style="color:#e6edf3">5가지 신호를 자동 감지</strong>합니다<br>
      평일 <strong style="color:#ffa502">09:07 / 13:02 / 15:37</strong> 자동 체크 → 조건 충족 시 이메일 발송
    </p>
    <div style="background:#0d1117;border:1px solid #21262d;border-radius:8px;padding:10px 14px;margin-bottom:14px;font-size:11px;color:#7d8590;line-height:2">
      자동 감지 조건 &nbsp;
      <span style="background:rgba(0,255,136,.1);color:#00ff88;border:1px solid rgba(0,255,136,.2);padding:2px 8px;border-radius:10px;font-size:10px;font-weight:700">RSI &lt; 32</span>&nbsp;
      <span style="background:rgba(6,182,212,.1);color:#06b6d4;border:1px solid rgba(6,182,212,.2);padding:2px 8px;border-radius:10px;font-size:10px;font-weight:700">이평 이격 -3%↓</span>&nbsp;
      <span style="background:rgba(251,146,60,.1);color:#fb923c;border:1px solid rgba(251,146,60,.2);padding:2px 8px;border-radius:10px;font-size:10px;font-weight:700">거래량 2배↑</span>&nbsp;
      <span style="background:rgba(167,139,250,.1);color:#a78bfa;border:1px solid rgba(167,139,250,.2);padding:2px 8px;border-radius:10px;font-size:10px;font-weight:700">급등 +15%↑</span>&nbsp;
      <span style="background:rgba(255,165,2,.1);color:#ffa502;border:1px solid rgba(255,165,2,.2);padding:2px 8px;border-radius:10px;font-size:10px;font-weight:700">상승준비 4점↑</span>
    </div>

    <div style="display:flex;gap:8px;align-items:flex-end;flex-wrap:wrap">
      <div>
        <label style="font-size:10px;color:#7d8590;display:block;margin-bottom:4px;letter-spacing:1px;text-transform:uppercase">종목 코드 (6자리)</label>
        <input type="text" id="al-code" placeholder="005930" maxlength="6"
          style="padding:10px 14px;background:#161b22;border:1px solid #30363d;border-radius:7px;color:#e6edf3;font-size:15px;font-family:monospace;width:120px;outline:none;font-weight:700"
          onkeydown="if(event.key==='Enter') addAlertFromSettings()">
      </div>
      <div>
        <label style="font-size:10px;color:#7d8590;display:block;margin-bottom:4px;letter-spacing:1px;text-transform:uppercase">종목명 (선택)</label>
        <input type="text" id="al-name" placeholder="삼성전자"
          style="padding:10px 14px;background:#161b22;border:1px solid #30363d;border-radius:7px;color:#e6edf3;font-size:13px;width:160px;outline:none"
          onkeydown="if(event.key==='Enter') addAlertFromSettings()">
      </div>
      <div>
        <label style="font-size:10px;color:#7d8590;display:block;margin-bottom:4px;letter-spacing:1px;text-transform:uppercase">시장</label>
        <select id="al-mkt" style="padding:10px 14px;background:#161b22;border:1px solid #30363d;border-radius:7px;color:#e6edf3;font-size:13px;outline:none">
          <option value="코스닥">코스닥</option>
          <option value="코스피">코스피</option>
        </select>
      </div>
      <button onclick="addAlertFromSettings()"
        style="padding:10px 24px;background:linear-gradient(135deg,rgba(255,165,2,.2),rgba(251,146,60,.2));border:1px solid rgba(255,165,2,.4);border-radius:8px;color:#ffa502;cursor:pointer;font-size:14px;font-weight:800;font-family:'Noto Sans KR'">
        🔔 추가
      </button>
    </div>

    <div id="alert-list-wrap" style="margin-top:16px">
      <div style="color:#484f58;font-size:13px;padding:12px 0">불러오는 중...</div>
    </div>
  </div>

  <div class="section">
    <div class="sec-title" id="fav-section-title">📋 현재 즐겨찾기 (불러오는 중...)</div>
    <div id="fav-table-wrap">
      <div style="color:#484f58;font-size:13px;padding:16px 0">불러오는 중...</div>
    </div>
    <div style="margin-top:12px">
      <button onclick="manualSync()" style="padding:7px 16px;border-radius:7px;background:rgba(6,182,212,.1);border:1px solid rgba(6,182,212,.3);color:#06b6d4;cursor:pointer;font-size:12px;font-weight:700">🔄 서버 동기화</button>
      <span id="sync-status" style="font-size:11px;color:#484f58;margin-left:10px"></span>
    </div>
  </div>
</div>
<script>
function testEmail(){
  fetch('/settings/email/test',{method:'POST'})
    .then(r=>r.json())
    .then(d=>alert(d.ok?'✅ 테스트 이메일 발송 성공!':'❌ 발송 실패: '+(d.error||'알 수 없는 오류')))
    .catch(e=>alert('오류: '+e));
}
function diagnose(){
  fetch('/settings/email/diagnose')
    .then(r=>r.json())
    .then(d=>{
      const issues = d.issues||[];
      const ok = issues.length===0;
      let msg = ok
        ? '✅ 모든 설정 정상!\n\n'
        : '⚠️ 문제 발견:\n'+issues.join('\n')+'\n\n';
      msg += '─────────────────\n';
      msg += '📧 발신: '+d.smtp_user+'\n';
      msg += '📬 수신: '+d.to_email+'\n';
      msg += '✅ 알림 활성화: '+(d.enabled?'켜짐':'❌ 꺼짐')+'\n';
      msg += '⭐ 즐겨찾기: '+d.favs_count+'종목\n';
      msg += '⏰ 스케줄러: '+(d.has_scheduler?'설치됨':'❌ apscheduler 미설치')+'\n';
      alert(msg);
    });
}
function sendNow(){
  if(!confirm('지금 즐겨찾기 알림 이메일을 발송할까요?')) return;
  const btn=document.getElementById('btn-send-now');
  btn.disabled=true; btn.textContent='발송 중...';
  fetch('/settings/email/send_now',{method:'POST'})
    .then(r=>r.json())
    .then(d=>{
      btn.disabled=false; btn.textContent='📤 지금 발송';
      alert(d.ok?'✅ 발송 완료! 메일함을 확인하세요.':'❌ 실패: '+(d.error||'오류'));
    })
    .catch(e=>{btn.disabled=false;btn.textContent='📤 지금 발송';alert('오류: '+e);});
}

const SCAN_NAMES = {surge:'🚀 급등',high:'🏆 전고점',volscan:'📈 거래량',
  cross:'🔀 교집합',fvgscan:'🧲 FVG',breakout:'🌅 상승준비'};

function renderFavTable(favs){
  const wrap = document.getElementById('fav-table-wrap');
  const title = document.getElementById('fav-section-title');
  title.textContent = '📋 현재 즐겨찾기 (' + favs.length + '종목 모니터링 중)';
  if(!favs.length){
    wrap.innerHTML='<div style="color:#484f58;font-size:13px;padding:16px 0">즐겨찾기 종목이 없습니다.<br>스캐너 탭에서 스캔 후 ★ 버튼을 눌러 추가하세요.</div>';
    return;
  }
  let html='<table style="width:100%;border-collapse:collapse;font-size:12px">';
  html+='<tr style="border-bottom:1px solid #21262d;color:#7d8590;font-size:10px;text-transform:uppercase;letter-spacing:1px">';
  html+='<th style="padding:8px;text-align:left">코드</th><th style="padding:8px;text-align:left">종목명</th>';
  html+='<th style="padding:8px;text-align:right">저장가</th><th style="padding:8px;text-align:left">스캔 출처</th>';
  html+='<th style="padding:8px;text-align:center">저장일</th></tr>';
  favs.forEach(f=>{
    const scanLbl = SCAN_NAMES[f.scanType]||f.scanType||'—';
    const savedAt = (f.savedAt||'').slice(0,10);
    html+=`<tr style="border-bottom:1px solid #161b22">
      <td style="padding:8px;font-family:JetBrains Mono;color:#7d8590">${f.code||''}</td>
      <td style="padding:8px;font-weight:600">${f.name||''} <span style="font-size:10px;color:#7d8590">${f.market||''}</span></td>
      <td style="padding:8px;text-align:right;font-family:JetBrains Mono">${(f.price||0).toLocaleString()}원</td>
      <td style="padding:8px;color:#06b6d4;font-size:11px">${scanLbl}</td>
      <td style="padding:8px;color:#484f58;font-size:11px;text-align:center">${savedAt}</td>
    </tr>`;
  });
  html+='</table>';
  wrap.innerHTML=html;
}

function manualSync(){
  const favs = JSON.parse(localStorage.getItem('krscan_favorites')||'[]');
  const st = document.getElementById('sync-status');
  st.textContent='동기화 중...'; st.style.color='#ffa502';
  fetch('/api/fav/sync',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({favs})})
    .then(r=>r.json())
    .then(d=>{
      st.textContent = d.ok ? '✅ 서버 동기화 완료 ('+d.count+'종목)' : '❌ 실패';
      st.style.color = d.ok ? '#00ff88' : '#ff4757';
    })
    .catch(()=>{ st.textContent='❌ 오류'; st.style.color='#ff4757'; });
}

// 페이지 로드: localStorage에서 읽어 바로 표시 + 서버 동기화
(function init(){
  const favs = JSON.parse(localStorage.getItem('krscan_favorites')||'[]');
  renderFavTable(favs);
  if(favs.length){
    fetch('/api/fav/sync',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({favs})})
      .then(r=>r.json())
      .then(d=>{
        const st=document.getElementById('sync-status');
        if(st){ st.textContent='서버 동기화됨 ('+d.count+'종목)'; st.style.color='#484f58'; }
      }).catch(()=>{});
  }
  // 알림 목록 불러오기
  renderAlertListSettings();
})();

const ALERT_KEY_S='krscan_alerts';
function loadAlertsS(){
  try{return JSON.parse(localStorage.getItem(ALERT_KEY_S)||'[]');}catch(e){return[];}
}
function saveAlertsS(arr){
  localStorage.setItem(ALERT_KEY_S,JSON.stringify(arr));
  fetch('/api/alerts/sync',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({alerts:arr})}).catch(()=>{});
}
function addAlertFromSettings(){
  const code=document.getElementById('al-code').value.trim().padStart(6,'0');
  const name=document.getElementById('al-name').value.trim();
  const mkt=document.getElementById('al-mkt').value;
  if(!code||code==='000000'){alert('종목 코드 6자리를 입력하세요');return;}
  const alerts=loadAlertsS();
  if(alerts.find(a=>a.code===code)){alert('이미 등록된 종목입니다');return;}
  // 5가지 자동 조건 세트로 저장
  alerts.unshift({
    id:Date.now()+'',
    code, name:name||code, market:mkt,
    active:true,
    created:new Date().toLocaleDateString('ko-KR'),
    // 자동 감지 조건들 (백엔드에서 모두 체크)
    auto_conditions:[
      {type:'rsi_below',   label:'RSI 과매도',   value:32,  icon:'📉'},
      {type:'ma_gap_below',label:'이평 이격',     value:3,   icon:'📐'},
      {type:'vol_surge',   label:'거래량 급증',   value:2.0, icon:'📈'},
      {type:'surge_pct',   label:'급등 포착',     value:15,  icon:'🚀'},
      {type:'breakout_score',label:'상승준비',    value:4,   icon:'🌅'},
    ]
  });
  saveAlertsS(alerts);
  document.getElementById('al-code').value='';
  document.getElementById('al-name').value='';
  renderAlertListSettings();
}
function toggleAlertS(id){
  const arr=loadAlertsS();
  const idx=arr.findIndex(a=>a.id===id);
  if(idx>=0) arr[idx].active=!arr[idx].active;
  saveAlertsS(arr);
  renderAlertListSettings();
}
function deleteAlertS(id){
  saveAlertsS(loadAlertsS().filter(a=>a.id!==id));
  renderAlertListSettings();
}
function renderAlertListSettings(){
  const wrap=document.getElementById('alert-list-wrap');
  if(!wrap) return;
  const alerts=loadAlertsS();
  if(!alerts.length){
    wrap.innerHTML='<div style="color:#484f58;font-size:12px;padding:8px 0">등록된 종목이 없습니다. 코드를 입력해 추가하세요.</div>';
    return;
  }
  const COND_COLORS={rsi_below:'#00ff88',ma_gap_below:'#06b6d4',vol_surge:'#fb923c',surge_pct:'#ff4757',breakout_score:'#ffa502'};
  wrap.innerHTML=alerts.map(a=>`
    <div style="display:flex;align-items:center;gap:10px;padding:12px 14px;background:#161b22;
      border:1px solid ${a.active?'rgba(255,165,2,.25)':'#21262d'};border-radius:9px;margin-bottom:6px">
      <div style="width:9px;height:9px;border-radius:50%;flex-shrink:0;
        background:${a.active?'#ffa502':'#484f58'}"></div>
      <div style="flex:1;min-width:0">
        <div style="font-size:14px;font-weight:800;color:#e6edf3">
          ${a.code}
          <span style="font-weight:600;color:#c9d1d9;margin-left:4px">${a.name||''}</span>
          <span style="font-size:11px;color:#484f58;margin-left:6px">${a.market||''}</span>
        </div>
        <div style="display:flex;gap:4px;flex-wrap:wrap;margin-top:5px">
          ${(a.auto_conditions||[]).map(c=>`
            <span style="font-size:10px;font-weight:700;padding:2px 7px;border-radius:8px;
              background:rgba(255,255,255,.05);color:${COND_COLORS[c.type]||'#7d8590'};
              border:1px solid rgba(255,255,255,.08)">${c.icon||''} ${c.label}</span>`).join('')}
        </div>
      </div>
      <span style="font-size:11px;color:#484f58;white-space:nowrap">${a.created||''}</span>
      <button onclick="toggleAlertS('${a.id}')" style="padding:5px 12px;border-radius:6px;
        background:${a.active?'rgba(255,71,87,.1)':'rgba(0,255,136,.1)'};
        border:1px solid ${a.active?'rgba(255,71,87,.3)':'rgba(0,255,136,.3)'};
        color:${a.active?'#ff4757':'#00ff88'};cursor:pointer;font-size:11px;font-weight:700;
        font-family:'Noto Sans KR';white-space:nowrap">
        ${a.active?'⏸ 중지':'▶ 활성화'}</button>
      <button onclick="deleteAlertS('${a.id}')" style="padding:5px 10px;border-radius:6px;
        border:1px solid #30363d;background:transparent;color:#7d8590;cursor:pointer;font-size:13px">✕</button>
    </div>`).join('');
}

</script>
</body>
</html>"""

@app.route("/settings/email", methods=["GET","POST"])
@login_required
def email_settings():
    msg=""; ok_flag=False
    if request.method=="POST":
        cfg = {
            "smtp_user": request.form.get("smtp_user",""),
            "smtp_pass": request.form.get("smtp_pass",""),
            "smtp_host": request.form.get("smtp_host","smtp.gmail.com"),
            "smtp_port": int(request.form.get("smtp_port",587)),
            "to_email":  request.form.get("to_email",""),
            "enabled":   bool(request.form.get("enabled")),
        }
        save_email_cfg(cfg)
        msg="✅ 저장되었습니다"; ok_flag=True

    cfg  = load_email_cfg()

    # Jinja2 우회 — Python f-string으로 직접 치환 후 Response 반환
    html = EMAIL_SETTINGS_HTML
    html = html.replace("__SMTP_USER__",  cfg.get("smtp_user",""))
    html = html.replace("__SMTP_HOST__",  cfg.get("smtp_host","smtp.gmail.com"))
    html = html.replace("__SMTP_PORT__",  str(cfg.get("smtp_port",587)))
    html = html.replace("__TO_EMAIL__",   cfg.get("to_email",""))
    html = html.replace("__ENABLED__",    "checked" if cfg.get("enabled") else "")
    if msg:
        cls = "ok" if ok_flag else "err"
        html = html.replace("__MSG_BLOCK__",
            f'<div class="msg {cls}">{msg}</div>')
    else:
        html = html.replace("__MSG_BLOCK__", "")

    from flask import Response as Resp
    return Resp(html, mimetype="text/html")

@app.route("/settings/email/test", methods=["POST"])
@login_required
def email_test():
    try:
        send_stock_email(is_test=True)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/settings/email/send_now", methods=["POST"])
@login_required
def email_send_now():
    """수동 즉시 발송"""
    try:
        send_stock_email(is_test=False)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/settings/email/diagnose", methods=["GET"])
@login_required
def email_diagnose():
    """이메일 발송 진단"""
    cfg   = load_email_cfg()
    favs  = load_server_favs()
    try:
        import apscheduler
        has_sched = True
    except:
        has_sched = False
    return jsonify({
        "smtp_user":    cfg.get("smtp_user","❌ 없음"),
        "to_email":     cfg.get("to_email","❌ 없음"),
        "enabled":      cfg.get("enabled", False),
        "favs_count":   len(favs),
        "has_scheduler": has_sched,
        "issues": [
            x for x in [
                "❌ smtp_user 미설정" if not cfg.get("smtp_user") else None,
                "❌ to_email 미설정" if not cfg.get("to_email") else None,
                "❌ 알림 활성화 체크 안됨" if not cfg.get("enabled") else None,
                "❌ 즐겨찾기 0종목 — 서버 동기화 필요" if not favs else None,
                "❌ APScheduler 미설치" if not has_sched else None,
            ] if x
        ]
    })





# ── /status ── 실시간 모니터링 대시보드 ─────────────────
@app.route("/status")
@login_required
def status_page():
    from flask import Response as Resp
    cfg         = load_email_cfg()
    favs        = load_server_favs()
    alerts_lst  = load_alerts()
    hist        = load_price_history()
    try:
        import apscheduler; has_sched = True
    except: has_sched = False

    enabled     = cfg.get("enabled", False)
    smtp_ok     = bool(cfg.get("smtp_user") and cfg.get("smtp_pass") and cfg.get("to_email"))
    fav_count   = len(favs)
    alert_count = len([a for a in alerts_lst if a.get("active")])
    hist_count  = sum(len(v.get("history",{})) for v in hist.values())
    now         = datetime.now()
    now_str     = now.strftime("%Y년 %m월 %d일 %H:%M:%S")
    cur_min     = now.hour * 60 + now.minute
    schedules   = [(9,5,"🌅","장 시작"),(13,0,"☀️","점심"),(15,35,"🌆","장 마감")]

    # 즐겨찾기 테이블 행
    fav_rows = ""
    for f in favs[:60]:
        code = f.get("code","")
        he   = hist.get(code, {})
        days = len(he.get("history",{}))
        init_p = he.get("initial_price") or f.get("price",0)
        h_data = he.get("history",{})
        latest = h_data[max(h_data)] if h_data else init_p
        total_chg = round((latest-init_p)/init_p*100,2) if init_p and latest and init_p>0 else 0
        chg_color = "#1d9e75" if total_chg>=0 else "#e24b4a"
        chg_str   = f"{'+' if total_chg>=0 else ''}{total_chg:.1f}%"
        scan_lbl  = {"surge":"🚀","high":"🏆","volscan":"📈","cross":"🔀",
                     "fvgscan":"🧲","breakout":"🌅","hcs":"⚡"}.get(f.get("scanType",""),"⭐")
        mini = ""
        for d in range(13,-1,-1):
            dt = (now-timedelta(days=d)).strftime("%Y-%m-%d")
            if dt in h_data:
                pdt = (now-timedelta(days=d+1)).strftime("%Y-%m-%d")
                pp  = h_data.get(pdt, h_data[dt])
                cd  = (h_data[dt]-pp)/pp*100 if pp else 0
                bg  = "#00c864" if cd>=3 else "#4ade80" if cd>=1 else "#86efac" if cd>0 else "#e24b4a" if cd<=-3 else "#f87171" if cd<=-1 else "#fca5a5" if cd<0 else "#484f58"
                mini += f'<div style="width:14px;height:14px;border-radius:2px;background:{bg}" title="{dt}: {cd:+.1f}%"></div>'
            else:
                mini += '<div style="width:14px;height:14px;border-radius:2px;background:#21262d"></div>'

        fav_rows += f"""<tr style="border-bottom:1px solid #21262d">
          <td style="padding:10px 14px;font-family:monospace;font-size:13px;color:#7d8590">{code}</td>
          <td style="padding:10px 14px;font-size:14px;font-weight:700">{scan_lbl} {f.get("name","")}</td>
          <td style="padding:10px 14px;text-align:right;font-family:monospace;font-size:14px;font-weight:700">{int(latest):,}원</td>
          <td style="padding:10px 14px;text-align:right;font-family:monospace;font-size:14px;font-weight:800;color:{chg_color}">{chg_str}</td>
          <td style="padding:10px 14px;text-align:center;color:{"#00ff88" if days>=7 else "#ffa502" if days>0 else "#484f58"};font-family:monospace">{days}일</td>
          <td style="padding:10px 14px"><div style="display:flex;gap:2px">{mini}</div></td>
        </tr>"""

    # 알림 조건 행
    alert_rows = ""
    for a in alerts_lst[:30]:
        active = a.get("active", True)
        conds  = a.get("auto_conditions",[])
        tags   = "".join(
            f'<span style="font-size:10px;padding:2px 7px;border-radius:8px;background:rgba(255,255,255,.06);color:#7d8590;margin-right:3px">{c.get("icon","")} {c.get("label","")}</span>'
            for c in conds)
        alert_rows += f"""<tr style="border-bottom:1px solid #21262d;opacity:{'1' if active else '0.4'}">
          <td style="padding:10px 14px"><span style="width:8px;height:8px;border-radius:50%;background:{'#00ff88' if active else '#484f58'};display:inline-block"></span></td>
          <td style="padding:10px 14px;font-family:monospace;font-size:13px;font-weight:700">{a.get("code","")}</td>
          <td style="padding:10px 14px;font-size:14px;font-weight:600">{a.get("name","")}</td>
          <td style="padding:10px 14px;font-size:11px;color:#7d8590">{a.get("market","")}</td>
          <td style="padding:10px 14px">{tags}</td>
          <td style="padding:10px 14px;font-size:11px;color:#484f58">{a.get("created","")}</td>
        </tr>"""

    # 타임라인 도트 상태
    def tl_cls(h, m):
        t = h*60+m
        if cur_min > t: return "done"
        # 다음 발송 시간 = 현재 이후 가장 가까운 것
        future = [(hh*60+mm) for hh,mm,_,__ in schedules if hh*60+mm > cur_min]
        if future and t == min(future): return "next"
        return "pending"

    tl_items = "".join(f"""<div class="tl-item">
      <div class="tl-dot {tl_cls(h,m)}">{emoji}</div>
      <div class="tl-time">{h:02d}:{m:02d}</div>
      <div class="tl-label">{lbl}</div>
    </div>""" for h,m,emoji,lbl in schedules)

    html = f"""<!DOCTYPE html>
<html lang="ko"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<meta http-equiv="refresh" content="60">
<title>KRSCAN 📡 상태</title>
<link href="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=Noto+Sans+KR:wght@400;500;700&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:#07090d;color:#e6edf3;font-family:'Noto Sans KR',sans-serif;padding:16px;min-height:100vh}}
.wrap{{max-width:1100px;margin:0 auto}}
.topbar{{display:flex;align-items:center;gap:12px;margin-bottom:20px;padding-bottom:14px;border-bottom:1px solid #21262d;flex-wrap:wrap}}
.logo{{font-family:'Bebas Neue';font-size:28px;color:#00ff88;letter-spacing:3px}}
.topbar-sub{{font-size:13px;color:#8b949e}}
.topbar a{{margin-left:auto;font-size:12px;color:#7d8590;text-decoration:none;padding:6px 14px;border:1px solid #30363d;border-radius:6px;transition:all .2s}}
.topbar a:hover{{color:#e6edf3;border-color:#7d8590}}
.refresh{{font-size:11px;color:#484f58;margin-left:4px}}

.stat-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px;margin-bottom:18px}}
.sc{{background:#0d1117;border:1px solid #21262d;border-radius:12px;padding:16px 18px;transition:border-color .2s}}
.sc.ok{{border-color:rgba(0,255,136,.3)}} .sc.warn{{border-color:rgba(255,165,2,.3)}} .sc.err{{border-color:rgba(255,71,87,.3)}}
.sc-icon{{font-size:20px;margin-bottom:6px}}
.sc-lbl{{font-size:10px;color:#7d8590;letter-spacing:1px;text-transform:uppercase;margin-bottom:4px}}
.sc-val{{font-family:'JetBrains Mono';font-size:20px;font-weight:700}}
.sc.ok .sc-val{{color:#00ff88}} .sc.warn .sc-val{{color:#ffa502}} .sc.err .sc-val{{color:#ff4757}}
.sc-sub{{font-size:10px;color:#484f58;margin-top:3px;line-height:1.4}}

.panel{{background:#0d1117;border:1px solid #21262d;border-radius:12px;overflow:hidden;margin-bottom:14px}}
.phead{{display:flex;align-items:center;justify-content:space-between;padding:11px 16px;background:#161b22;border-bottom:1px solid #21262d}}
.phead-title{{font-size:13px;font-weight:700}}
.phead-badge{{font-size:11px;color:#7d8590;font-family:'JetBrains Mono'}}

.timeline{{display:flex;align-items:center;padding:18px 24px;gap:0}}
.tl-item{{flex:1;text-align:center;position:relative}}
.tl-item:not(:last-child)::after{{content:'';position:absolute;top:18px;left:50%;width:100%;height:2px;background:#21262d;z-index:0}}
.tl-dot{{width:36px;height:36px;border-radius:50%;display:flex;align-items:center;justify-content:center;margin:0 auto 8px;font-size:16px;position:relative;z-index:1}}
.tl-dot.done{{background:rgba(0,255,136,.12);border:2px solid #00ff88}}
.tl-dot.next{{background:rgba(255,165,2,.12);border:2px solid #ffa502;animation:pulse 2s infinite}}
.tl-dot.pending{{background:#161b22;border:2px solid #30363d}}
.tl-time{{font-family:'JetBrains Mono';font-size:14px;font-weight:700}}
.tl-label{{font-size:11px;color:#7d8590;margin-top:2px}}
@keyframes pulse{{0%,100%{{box-shadow:0 0 0 0 rgba(255,165,2,.4)}}50%{{box-shadow:0 0 0 8px rgba(255,165,2,0)}}}}

table{{width:100%;border-collapse:collapse}}
th{{padding:9px 14px;background:#161b22;font-size:10px;color:#7d8590;letter-spacing:1px;text-transform:uppercase;text-align:left;border-bottom:1px solid #21262d;white-space:nowrap}}
.empty{{padding:24px;text-align:center;color:#484f58;font-size:13px}}
</style>
</head>
<body><div class="wrap">

<div class="topbar">
  <div class="logo">KRSCAN</div>
  <span class="topbar-sub">📡 상태 모니터</span>
  <span class="refresh">⟳ 60초 자동 갱신</span>
  <a href="/">← 스캐너</a>
  <a href="/settings/email" style="margin-left:4px">⚙ 설정</a>
</div>

<div class="stat-grid">
  <div class="sc {'ok' if smtp_ok else 'err'}">
    <div class="sc-icon">📧</div><div class="sc-lbl">이메일 설정</div>
    <div class="sc-val">{'정상' if smtp_ok else '오류'}</div>
    <div class="sc-sub">{(cfg.get("smtp_user","미설정"))[:28]}</div>
  </div>
  <div class="sc {'ok' if enabled else 'err'}">
    <div class="sc-icon">{'✅' if enabled else '❌'}</div><div class="sc-lbl">알림 활성화</div>
    <div class="sc-val">{'켜짐' if enabled else '꺼짐'}</div>
    <div class="sc-sub">{'자동 발송 중' if enabled else '설정에서 체크 필요'}</div>
  </div>
  <div class="sc {'ok' if has_sched else 'err'}">
    <div class="sc-icon">⏰</div><div class="sc-lbl">스케줄러</div>
    <div class="sc-val">{'실행 중' if has_sched else '미설치'}</div>
    <div class="sc-sub">{'09:05 / 13:00 / 15:35' if has_sched else 'pip install apscheduler'}</div>
  </div>
  <div class="sc {'ok' if fav_count>0 else 'warn'}">
    <div class="sc-icon">⭐</div><div class="sc-lbl">즐겨찾기</div>
    <div class="sc-val">{fav_count}종목</div>
    <div class="sc-sub">서버 동기화 기준</div>
  </div>
  <div class="sc {'ok' if alert_count>0 else 'warn'}">
    <div class="sc-icon">🔔</div><div class="sc-lbl">조건 알림</div>
    <div class="sc-val">{alert_count}개</div>
    <div class="sc-sub">활성 감시 종목</div>
  </div>
  <div class="sc {'ok' if hist_count>0 else 'warn'}">
    <div class="sc-icon">📊</div><div class="sc-lbl">가격 이력</div>
    <div class="sc-val">{hist_count}건</div>
    <div class="sc-sub">누적 기록 수</div>
  </div>
</div>

<div class="panel">
  <div class="phead"><span class="phead-title">⏰ 오늘 발송 스케줄</span>
    <span class="phead-badge">{'평일' if now.weekday()<5 else '⚠ 주말 — 미발송'}</span></div>
  <div class="timeline">{tl_items}</div>
</div>

<div class="panel">
  <div class="phead">
    <span class="phead-title">⭐ 즐겨찾기 현황</span>
    <span class="phead-badge">{fav_count}종목 · 최근 14일 히트맵</span>
  </div>
  {'<table><thead><tr><th>코드</th><th>종목명</th><th style="text-align:right">최근가</th><th style="text-align:right">저장 후 수익</th><th style="text-align:center">기록일</th><th>최근 14일</th></tr></thead><tbody>'+fav_rows+'</tbody></table>'
   if favs else '<div class="empty">즐겨찾기 없음 · settings/email 에서 🔄 서버 동기화</div>'}
</div>

<div class="panel">
  <div class="phead">
    <span class="phead-title">🔔 조건 알림 현황</span>
    <span class="phead-badge">{len(alerts_lst)}개 등록 · {alert_count}개 활성</span>
  </div>
  {'<table><thead><tr><th>상태</th><th>코드</th><th>종목명</th><th>시장</th><th>감시 조건 (자동)</th><th>등록일</th></tr></thead><tbody>'+alert_rows+'</tbody></table>'
   if alerts_lst else '<div class="empty">등록된 알림 없음 · settings/email 에서 추가</div>'}
</div>

<div style="text-align:center;padding:12px;font-size:11px;color:#484f58">
  마지막 갱신: {now_str}
</div>
</div></body></html>"""
    return Resp(html, mimetype="text/html")

@app.route("/admin")
@admin_required
def admin_page():
    users = load_users()
    pending  = [u for u,d in users.items() if d.get("status")=="pending"]
    approved = [u for u,d in users.items() if d.get("status") in ("approved","admin")]
    msg = request.args.get("msg","")
    return render_template_string(ADMIN_HTML,
        admin=ADMIN_ID, pending=pending, approved=approved,
        total=len(users), msg=msg)

@app.route("/admin/approve", methods=["POST"])
@admin_required
def admin_approve():
    username = request.form.get("username","")
    users = load_users()
    if username in users:
        users[username]["status"] = "approved"
        save_users(users)
    return redirect(url_for("admin_page", msg=f"✅ {username} 승인 완료"))

@app.route("/admin/reject", methods=["POST"])
@admin_required
def admin_reject():
    username = request.form.get("username","")
    users = load_users()
    if username in users:
        del users[username]   # 거절 시 삭제
        save_users(users)
    return redirect(url_for("admin_page", msg=f"🗑 {username} 거절 및 삭제"))

@app.route("/admin/revoke", methods=["POST"])
@admin_required
def admin_revoke():
    username = request.form.get("username","")
    if username == ADMIN_ID:
        return redirect(url_for("admin_page", msg="❌ 관리자 계정은 변경 불가"))
    users = load_users()
    if username in users:
        users[username]["status"] = "pending"
        save_users(users)
    return redirect(url_for("admin_page", msg=f"⚠️ {username} 승인 취소"))

# ── /vwap ────────────────────────────────────────────────
@app.route("/vwap", methods=["POST"])
@login_required
def vwap():
    params    = request.json
    ticker    = params.get("ticker", "BTC-USD")
    is_crypto = params.get("is_crypto", True)
    period    = params.get("period", "1d")   # 1d | 7d | 1m
    # period→타임프레임 매핑
    tf_period_map = {
        "1d": [("5m",288),("15m",192),("1h",168)],
        "7d": [("1h",168),("4h",84)],
        "1m": [("4h",180),("1d",30)],
    }
    yf_period_map = {
        "1d": {"5m":("5m","5d"),"15m":("15m","5d"),"1h":("1h","30d")},
        "7d": {"1h":("1h","30d"),"4h":("1h","60d")},
        "1m": {"4h":("1h","60d"),"1d":("1d","1y")},
    }

    def calc_vwap(df):
        """VWAP + 표준편차 밴드 + 기울기 + 거래량 추세"""
        if df is None or len(df) < 5:
            return None
        df = df.copy().tail(300)
        df["tp"] = (df["High"] + df["Low"] + df["Close"]) / 3
        df["tpv"] = df["tp"] * df["Volume"]
        df["cum_tpv"] = df["tpv"].cumsum()
        df["cum_vol"] = df["Volume"].cumsum()
        df["vwap"] = df["cum_tpv"] / df["cum_vol"].replace(0, float("nan"))

        # 표준편차 밴드
        df["dev"] = (df["Close"] - df["vwap"]) ** 2
        df["dev_ma"] = df["dev"].rolling(20).mean().fillna(df["dev"].mean())
        df["std"] = df["dev_ma"].apply(lambda x: math.sqrt(max(x, 0)))

        vwap_val  = float(df["vwap"].iloc[-1])
        std_val   = float(df["std"].iloc[-1])
        price_val = float(df["Close"].iloc[-1])
        upper1    = round(vwap_val + std_val, 6)
        lower1    = round(vwap_val - std_val, 6)
        upper2    = round(vwap_val + std_val * 2, 6)
        lower2    = round(vwap_val - std_val * 2, 6)

        diff_pct = round((price_val - vwap_val) / vwap_val * 100, 3) if vwap_val else 0

        # 위치 판정 (±0.3% 이내 = NEAR)
        if   diff_pct >  0.3: position = "ABOVE"
        elif diff_pct < -0.3: position = "BELOW"
        else:                 position = "NEAR"

        # VWAP 기울기 (최근 5봉)
        recent_vwap = df["vwap"].dropna().iloc[-5:]
        if len(recent_vwap) >= 2:
            slope = float(recent_vwap.iloc[-1]) - float(recent_vwap.iloc[0])
            slope_pct = slope / float(recent_vwap.iloc[0]) * 100 if recent_vwap.iloc[0] else 0
            slope_dir = "UP" if slope_pct > 0.02 else "DOWN" if slope_pct < -0.02 else "SIDE"
        else:
            slope_dir = "SIDE"

        # 거래량 추세 (최근 5봉 vs 이전 5봉)
        if len(df) >= 10:
            vol_recent = float(df["Volume"].iloc[-5:].mean())
            vol_prev   = float(df["Volume"].iloc[-10:-5].mean())
            vol_ratio  = vol_recent / vol_prev if vol_prev else 1
            vol_trend  = "증가↑" if vol_ratio > 1.15 else "감소↓" if vol_ratio < 0.85 else "보통"
        else:
            vol_trend = "—"

        return {
            "vwap":         round(vwap_val, 6),
            "price":        round(price_val, 6),
            "vband_upper":  upper1,
            "vband_lower":  lower1,
            "vband_upper2": upper2,
            "vband_lower2": lower2,
            "diff_pct":     diff_pct,
            "position":     position,
            "slope_dir":    slope_dir,
            "vol_trend":    vol_trend,
        }

    try:
        tf_configs = tf_period_map.get(period, tf_period_map["1d"])
        timeframes = {}

        if is_crypto_ticker(ticker):
            price, chg24, name = binance_price(ticker)
            for tf_key, limit in tf_configs:
                try:
                    df = binance_ohlcv(ticker, tf_key, limit=limit)
                    if tf_key == "4h" and (df is None or df.empty):
                        df1h = binance_ohlcv(ticker, "1h", limit=limit*4)
                        df = df1h.resample("4h").agg({"Open":"first","High":"max","Low":"min","Close":"last","Volume":"sum"}).dropna() if not df1h.empty else df
                    timeframes[tf_key] = calc_vwap(df) or {}
                except Exception:
                    timeframes[tf_key] = {}
        else:
            stk = yf.Ticker(ticker)
            info = stk.info
            price = float(info.get("currentPrice") or info.get("regularMarketPrice") or info.get("previousClose") or 0)
            prev  = float(info.get("regularMarketPreviousClose") or info.get("previousClose") or price)
            chg24 = round((price - prev) / prev * 100, 2) if prev else 0
            name  = info.get("longName") or info.get("shortName") or ticker.split(".")[0]
            yf_map = yf_period_map.get(period, yf_period_map["1d"])
            for tf_key, (yf_iv, yf_per) in yf_map.items():
                try:
                    df_raw = stk.history(period=yf_per, interval=yf_iv)
                    if tf_key == "4h" and not df_raw.empty:
                        df_raw = df_raw.resample("4h").agg({"Open":"first","High":"max","Low":"min","Close":"last","Volume":"sum"}).dropna()
                    timeframes[tf_key] = calc_vwap(df_raw) or {}
                except Exception:
                    timeframes[tf_key] = {}

        return jsonify({
            "ticker": ticker, "name": name,
            "price": round(float(price), 6),
            "chg24": chg24,
            "is_crypto": is_crypto,
            "period": period,
            "timeframes": timeframes,
        })
    except Exception as e:
        return jsonify({"error": str(e)})


# ── /trend ──────────────────────────────────────────────
@app.route("/trend", methods=["POST"])
@login_required
def trend():
    params    = request.json
    ticker    = params.get("ticker", "BTC-USD")
    is_crypto = params.get("is_crypto", True)
    period    = params.get("period", "1d")

    # 기간별 결과 키 & 라벨 (API 호출 수는 항상 동일: 1h + 1d = 2회)
    PERIOD_TF = {
        "1d": [("1h","1시간"),  ("4h","4시간"),  ("1d","일봉")],
        "7d": [("1h","1시간"),  ("4h","4시간"),  ("1d","일봉")],
        "1m": [("4h","4시간"),  ("1d","일봉"),   ("1w","주봉")],
    }
    tf_cfg = PERIOD_TF.get(period, PERIOD_TF["1d"])
    tf_keys   = [x[0] for x in tf_cfg]
    tf_labels = {x[0]: x[1] for x in tf_cfg}

    def analyze_trend(df):
        if df is None or len(df) < 10:
            return {"trend":"UNKNOWN","strength":0,"ema20":0,"ema50":0,"ema200":0,
                    "rsi":50,"close":0,"chg":0,"hh_hl":False,"ll_lh":False,"reason":"데이터 부족"}
        c = df["Close"].astype(float); h = df["High"].astype(float); l = df["Low"].astype(float)
        e20  = float(c.ewm(span=20,  adjust=False).mean().iloc[-1])
        e50  = float(c.ewm(span=50,  adjust=False).mean().iloc[-1])
        e200 = float(c.ewm(span=min(len(c),200), adjust=False).mean().iloc[-1])
        delta = c.diff()
        gain  = delta.clip(lower=0).rolling(14).mean()
        loss  = (-delta.clip(upper=0)).rolling(14).mean()
        rsi   = float(100-(100/(1+gain.iloc[-1]/(loss.iloc[-1]+1e-9))))
        close = float(c.iloc[-1]); prev = float(c.iloc[-2]) if len(c)>1 else close
        chg   = round((close-prev)/prev*100,2) if prev else 0
        rh = h.iloc[-6:-1].values; rl = l.iloc[-6:-1].values
        hh_hl = bool(len(rh)>=2 and float(rh[-1])>float(rh[-2]) and float(rl[-1])>float(rl[-2]))
        ll_lh = bool(len(rh)>=2 and float(rh[-1])<float(rh[-2]) and float(rl[-1])<float(rl[-2]))
        score=0; reasons=[]
        if close>e20: score+=1
        if close>e50: score+=1
        if close>e200: score+=1; reasons.append("가격>EMA200")
        if e20>e50:   score+=1; reasons.append("EMA20>EMA50")
        if e50>e200:  score+=1; reasons.append("EMA50>EMA200")
        if hh_hl:     score+=1; reasons.append("고점저점상승")
        if rsi>55:    score+=1; reasons.append("RSI강세")
        if rsi>70:    reasons.append("⚠️과매수")
        if close<e20: score-=1
        if close<e50: score-=1
        if close<e200: score-=1; reasons.append("가격<EMA200")
        if e20<e50:   score-=1; reasons.append("EMA20<EMA50")
        if ll_lh:     score-=1; reasons.append("고점저점하락")
        if rsi<45:    score-=1; reasons.append("RSI약세")
        if rsi<30:    reasons.append("⚠️과매도")
        if   score>=4:  ts="강한 상승"
        elif score>=2:  ts="상승"
        elif score>=1:  ts="약한 상승"
        elif score<=-4: ts="강한 하락"
        elif score<=-2: ts="하락"
        elif score<=-1: ts="약한 하락"
        else:           ts="횡보"
        fmt=lambda v: round(v,2) if v>10 else round(v,5)
        return {"trend":"UP" if score>0 else "DOWN" if score<0 else "SIDE",
                "trend_str":ts,"strength":max(-6,min(6,score)),
                "ema20":fmt(e20),"ema50":fmt(e50),"ema200":fmt(e200),
                "rsi":round(rsi,1),"close":fmt(close),"chg":chg,
                "hh_hl":hh_hl,"ll_lh":ll_lh,"reason":" · ".join(reasons[:4]) or "중립"}

    def safe_resample(df, rule):
        try:
            return df.resample(rule).agg({"Open":"first","High":"max","Low":"min","Close":"last","Volume":"sum"}).dropna()
        except Exception:
            return pd.DataFrame()

    try:
        results = {}

        if is_crypto_ticker(ticker):
            price, chg24, name = binance_price(ticker)
            # 1h 데이터 1번만 fetch (4h는 리샘플)
            try:
                df1h = binance_ohlcv(ticker, "1h", limit=200)
            except Exception:
                df1h = pd.DataFrame()
            # 1d 데이터 (1d/1w 필요 시)
            df1d = pd.DataFrame()
            if "1d" in tf_keys or "1w" in tf_keys:
                try:
                    df1d = binance_ohlcv(ticker, "1d", limit=365)
                except Exception:
                    df1d = pd.DataFrame()
            # 타임프레임별 분석
            src_map = {
                "1h": df1h if not df1h.empty else None,
                "4h": safe_resample(df1h, "4h") if not df1h.empty else None,
                "1d": df1d if not df1d.empty else None,
                "1w": safe_resample(df1d, "W") if not df1d.empty else None,
            }
            for tf in tf_keys:
                results[tf] = analyze_trend(src_map.get(tf))

        else:
            stk = yf.Ticker(ticker)
            try:
                info  = stk.info
                price = float(info.get("currentPrice") or info.get("regularMarketPrice") or info.get("previousClose") or 0)
                prev  = float(info.get("regularMarketPreviousClose") or info.get("previousClose") or price)
                name  = info.get("longName") or info.get("shortName") or ticker.split(".")[0]
                chg24 = round((price-prev)/prev*100,2) if prev else 0
            except Exception:
                price, chg24, name = 0, 0, ticker.split(".")[0]
            try:
                df1h = stk.history(period="60d", interval="1h")
            except Exception:
                df1h = pd.DataFrame()
            df1d = pd.DataFrame()
            if "1d" in tf_keys or "1w" in tf_keys:
                try:
                    df1d = stk.history(period="1y", interval="1d")
                except Exception:
                    df1d = pd.DataFrame()
            if not price and not df1d.empty:
                price = float(df1d["Close"].iloc[-1])
            src_map = {
                "1h": df1h if not df1h.empty else None,
                "4h": safe_resample(df1h, "4h") if not df1h.empty else None,
                "1d": df1d if not df1d.empty else None,
                "1w": safe_resample(df1d, "W") if not df1d.empty else None,
            }
            for tf in tf_keys:
                results[tf] = analyze_trend(src_map.get(tf))

        return jsonify({
            "ticker": ticker, "name": name,
            "price": round(float(price), 4),
            "chg24": chg24,
            "is_crypto": is_crypto,
            "period": period,
            "tf_labels": tf_labels,
            "tf_keys": tf_keys,
            "results": results,
        })
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()})

# ── /combo ──────────────────────────────────────────────
@app.route("/combo", methods=["POST"])
@login_required
def combo():
    params    = request.json
    ticker    = params.get("ticker", "BTC-USD")
    is_crypto = params.get("is_crypto", True)
    account   = float(params.get("account", 10_000_000))
    risk_pct  = float(params.get("risk_pct", 1.0))

    # ── 코인 이름 매핑 ──
    coin_names = {"BTC-USD":"Bitcoin","ETH-USD":"Ethereum","XRP-USD":"XRP"}
    icon_cls   = {"BTC-USD":"btc","ETH-USD":"eth","XRP-USD":"xrp"}

    try:
        # 1. 가격 정보
        if is_crypto:
            price, chg, name = binance_price(ticker)
            cls = icon_cls.get(ticker, "btc")
        else:
            stk  = yf.Ticker(ticker)
            info = stk.info
            price = float(info.get("currentPrice") or info.get("regularMarketPrice") or info.get("previousClose") or 0)
            prev  = float(info.get("regularMarketPreviousClose") or info.get("previousClose") or price)
            chg   = round((price - prev) / prev * 100, 2) if prev else 0
            name  = info.get("longName") or info.get("shortName") or ticker.split(".")[0]
            cls   = "stock"

        # 2. 4H 캔들 가져오기
        if is_crypto:
            df4h = binance_ohlcv(ticker, "4h", limit=200)
            df1h = binance_ohlcv(ticker, "1h", limit=200)
            df1d = binance_ohlcv(ticker, "1d", limit=200)
        else:
            stk  = yf.Ticker(ticker)
            df1h_raw = stk.history(period="60d", interval="1h")
            df4h = df1h_raw.resample("4h").agg({"Open":"first","High":"max","Low":"min","Close":"last","Volume":"sum"}).dropna() if not df1h_raw.empty else pd.DataFrame()
            df1h = df1h_raw
            df1d = stk.history(period="1y", interval="1d")

        # ── ① FVG 신호 (4H) ──────────────────────────────
        fvg4h = detect_fvg(df4h)
        fvg_score = 0; fvg_label = "없음"; fvg_detail = "4시간봉 FVG 미감지"
        if fvg4h["signal"] == "BULL":
            fvg_score = 3
            fvg_label = "불리시 FVG"
            fvg_detail = f"갭 구간: {round(fvg4h['gap_low'],4)} ~ {round(fvg4h['gap_high'],4)}<br><strong>진입: {round(fvg4h['gap_mid'],4)}</strong> (50% 지점)"
        elif fvg4h["signal"] == "BEAR":
            fvg_score = -3
            fvg_label = "베어리시 FVG"
            fvg_detail = f"갭 구간: {round(fvg4h['gap_low'],4)} ~ {round(fvg4h['gap_high'],4)}<br><strong>진입: {round(fvg4h['gap_mid'],4)}</strong> (50% 지점)"

        # ── ② 추세 신호 (EMA + RSI) ──────────────────────
        trend_score = 0; trend_label = "중립"; trend_detail = ""
        if not df4h.empty and len(df4h) >= 20:
            c4 = df4h["Close"].astype(float)
            e20  = float(c4.ewm(span=20, adjust=False).mean().iloc[-1])
            e50  = float(c4.ewm(span=50, adjust=False).mean().iloc[-1])
            e200 = float(c4.ewm(span=min(len(c4),200), adjust=False).mean().iloc[-1])
            delta = c4.diff()
            gain = delta.clip(lower=0).rolling(14).mean()
            loss = (-delta.clip(upper=0)).rolling(14).mean()
            rs   = gain / loss.replace(0, 1e-9)
            rsi  = float(100 - (100 / (1 + rs.iloc[-1])))
            cur  = float(c4.iloc[-1])

            t_pts = 0
            if cur > e20: t_pts += 1
            if cur > e50: t_pts += 1
            if cur > e200: t_pts += 1
            if e20 > e50: t_pts += 1
            if rsi > 55:  t_pts += 1
            if cur < e20: t_pts -= 1
            if cur < e50: t_pts -= 1
            if cur < e200: t_pts -= 1
            if e20 < e50: t_pts -= 1
            if rsi < 45:  t_pts -= 1

            trend_score = max(-3, min(3, t_pts))
            if trend_score >= 2:   trend_label = "강한 상승"
            elif trend_score >= 1: trend_label = "상승"
            elif trend_score <= -2:trend_label = "강한 하락"
            elif trend_score <= -1:trend_label = "하락"
            else:                  trend_label = "횡보"

            fmt_e = lambda v: f"${v:,.1f}" if v >= 1 else f"${v:.4f}"
            trend_detail = f"EMA20: {fmt_e(e20)} · EMA50: {fmt_e(e50)}<br>RSI: {round(rsi,1)} · 현재가: {fmt_e(cur)}"

        # ── ③ VWAP 신호 (1H) ─────────────────────────────
        vwap_score = 0; vwap_label = "중립"; vwap_detail = ""
        if not df1h.empty and len(df1h) >= 10:
            dv = df1h.copy().tail(200)
            dv["tp"]    = (dv["High"] + dv["Low"] + dv["Close"]) / 3
            dv["tpv"]   = dv["tp"] * dv["Volume"]
            dv["cum_tpv"] = dv["tpv"].cumsum()
            dv["cum_vol"] = dv["Volume"].cumsum()
            dv["vwap"]  = dv["cum_tpv"] / dv["cum_vol"].replace(0, float("nan"))
            vwap_val    = float(dv["vwap"].iloc[-1])
            vwap_price  = float(dv["Close"].iloc[-1])
            diff_pct    = (vwap_price - vwap_val) / vwap_val * 100 if vwap_val else 0

            if   diff_pct >  0.3: vwap_score =  1; vwap_label = "VWAP 위 (매수우위)"
            elif diff_pct < -0.3: vwap_score = -1; vwap_label = "VWAP 아래 (매도우위)"
            else:                 vwap_score =  0; vwap_label = "VWAP 근처 (중립)"

            fmt_vw = lambda v: f"${v:,.2f}" if v >= 1 else f"${v:.5f}"
            vwap_detail = f"VWAP: {fmt_vw(vwap_val)}<br>이격: {diff_pct:+.2f}%"

        # ── 종합 점수 ─────────────────────────────────────
        total_score = fvg_score + trend_score + vwap_score

        # ── 체크리스트 ────────────────────────────────────
        conditions = [
            {"ok": fvg4h["signal"] != "NONE", "text": f"4H FVG 감지됨 ({fvg4h['signal']})"},
            {"ok": abs(trend_score) >= 1, "text": f"추세 명확 ({trend_label})"},
            {"ok": abs(vwap_score) >= 1, "text": f"VWAP 신호 ({vwap_label})"},
            {"ok": fvg4h["signal"] != "NONE" and
                   ((fvg4h["signal"] == "BULL" and trend_score >= 1) or
                    (fvg4h["signal"] == "BEAR" and trend_score <= -1)),
             "text": "FVG 방향 = 추세 방향 일치"},
            {"ok": fvg4h["signal"] != "NONE" and
                   ((fvg4h["signal"] == "BULL" and vwap_score >= 0) or
                    (fvg4h["signal"] == "BEAR" and vwap_score <= 0)),
             "text": "FVG 방향 = VWAP 신호 일치"},
            {"ok": abs(total_score) >= 4, "text": f"종합 점수 충분 ({total_score:+d}/9)"},
        ]

        # ── 진입 타점 계산 ────────────────────────────────
        entry_data = {}
        fvg_setup = calc_setup(fvg4h, price, account, risk_pct)
        if fvg_setup:
            ep = fvg_setup
            entry_diff = round((ep["entry"] - price) / price * 100, 2) if price else 0
            entry_data = {
                "direction":   "LONG" if fvg4h["signal"] == "BULL" else "SHORT",
                "current":     round(price, 6),
                "entry_price": ep["entry"],
                "entry_diff":  entry_diff,
                "sl":          ep["sl"],
                "sl_pct":      ep["sl_pct"],
                "tp1":         ep["tp1"], "tp1_pct": ep["tp1_pct"],
                "tp2":         ep["tp2"], "tp2_pct": ep["tp2_pct"],
                "tp3":         ep["tp3"], "tp3_pct": ep["tp3_pct"],
                "shares":      ep["shares"],
                "invest_amt":  ep["invest_amt"],
                "risk_amt":    ep["risk_amt"],
                "rr":          ep["rr"],
            }

        return jsonify({
            "ticker":  ticker, "name": name, "cls": cls,
            "price":   round(float(price), 6), "change": chg,
            "is_crypto": is_crypto,
            "score":   total_score,
            "signals": {
                "fvg":   {"score": fvg_score,   "label": fvg_label,   "detail": fvg_detail},
                "trend": {"score": trend_score,  "label": trend_label, "detail": trend_detail},
                "vwap":  {"score": vwap_score,   "label": vwap_label,  "detail": vwap_detail},
            },
            "conditions": conditions,
            "entry": entry_data,
        })
    except Exception as e:
        return jsonify({"error": str(e)})


# ── /backtest ────────────────────────────────────────────
@app.route("/backtest", methods=["POST"])
@login_required
def backtest():
    params       = request.json
    ticker       = params.get("ticker", "BTC-USD")
    months       = int(params.get("months", 3))
    strategy     = params.get("strategy", "fvg")   # fvg | vwap | combo | emafvg | krma
    is_kr_stock  = bool(params.get("is_kr_stock", False))
    kr_market    = params.get("kr_market", "KS")
    min_score    = int(params.get("min_score", 3))
    vwap_thresh  = float(params.get("vwap_thresh", 0.3))
    account      = float(params.get("account", 10_000))
    risk_pct     = float(params.get("risk_pct", 1.0))
    timeout_c    = 20
    # 한국주식(krma)는 일봉 사용
    use_kr = (strategy == "krma" and is_kr_stock)

    def sim_trade(df, i, direction, entry_price, sl_price, tp1_price, tp2_price,
                  qty, account_, risk_pct_):
        """캔들 시뮬레이션 → (outcome, exit_price, pnl_usd, pnl_pct)"""
        n = len(df)
        outcome    = "timeout"
        exit_price = float(df["Close"].iloc[min(i+timeout_c, n-1)])
        for j in range(i+1, min(i+timeout_c+1, n)):
            hi = float(df["High"].iloc[j])
            lo = float(df["Low"].iloc[j])
            if direction == "LONG":
                if lo <= sl_price:   outcome="sl";  exit_price=sl_price;  break
                if hi >= tp2_price:  outcome="tp2"; exit_price=tp2_price; break
                if hi >= tp1_price:  outcome="tp1"; exit_price=tp1_price; break
            else:
                if hi >= sl_price:   outcome="sl";  exit_price=sl_price;  break
                if lo <= tp2_price:  outcome="tp2"; exit_price=tp2_price; break
                if lo <= tp1_price:  outcome="tp1"; exit_price=tp1_price; break
        pnl_usd = ((exit_price-entry_price) if direction=="LONG" else (entry_price-exit_price)) * qty
        pnl_pct = pnl_usd / account_ * 100
        return outcome, exit_price, pnl_usd, pnl_pct

    def calc_vwap_val(hist):
        dv = hist.tail(100).copy()
        dv["tp"]    = (dv["High"]+dv["Low"]+dv["Close"])/3
        dv["tpv"]   = dv["tp"]*dv["Volume"]
        cum_tpv = dv["tpv"].cumsum().iloc[-1]
        cum_vol = dv["Volume"].cumsum().iloc[-1]
        return float(cum_tpv/cum_vol) if cum_vol>0 else float(hist["Close"].iloc[-1])

    try:
        # ── 데이터 수집 (코인=바이낸스4H / 한국주식=yfinance일봉) ──
        if use_kr:
            stk  = yf.Ticker(ticker)
            info = stk.info
            name_kr = info.get("longName") or info.get("shortName") or ticker.split(".")[0]
            period_map = {1:"3mo", 3:"6mo", 6:"1y"}
            df_raw = stk.history(period=period_map.get(months,"6mo"), interval="1d")
            if df_raw.empty or len(df_raw) < 30:
                return jsonify({"error": f"한국 주식 데이터 없음 ({ticker}). 종목코드·시장을 확인하세요."})
            df_raw = df_raw.reset_index()
            time_col = "Date" if "Date" in df_raw.columns else df_raw.columns[0]
            df = df_raw.rename(columns={time_col:"time","Open":"Open","High":"High","Low":"Low","Close":"Close","Volume":"Volume"})[["time","Open","High","Low","Close","Volume"]]
            df["time"] = pd.to_datetime(df["time"]).dt.tz_localize(None)
        else:
            limit = months * 30 * 6 + 100
            df = binance_ohlcv(ticker, "4h", limit=min(limit, 1000))
            if df.empty or len(df) < 60:
                return jsonify({"error": "데이터 부족"})
            df = df.reset_index()
            df.columns = ["time","Open","High","Low","Close","Volume"]

        n = len(df)
        period_start = str(df["time"].iloc[0])[:10]
        period_end   = str(df["time"].iloc[-1])[:10]

        trades=[];equity=account;equity_curve=[{"idx":0,"equity":equity}]
        win_count=loss_count=timeout_count=0
        total_pnl_usd=0.0; win_pnls=[]; loss_pnls=[]
        max_equity=equity; max_dd=0.0
        lookback=50; cooldown=0

        for i in range(lookback, n-timeout_c-1):
            if cooldown>0: cooldown-=1; continue
            hist = df.iloc[:i+1]
            cur  = float(hist["Close"].iloc[-1])
            c    = hist["Close"].astype(float)

            # ── FVG 계산 (모든 전략) ──────────────────────
            fvg_info = detect_fvg(hist.tail(80).rename(columns=str))
            fvg_sig  = fvg_info["signal"]   # BULL / BEAR / NONE
            fvg_score = 3 if fvg_sig=="BULL" else -3 if fvg_sig=="BEAR" else 0

            # ── 추세 (EMA+RSI) — combo만 ─────────────────
            trend_score = 0
            if strategy in ("combo",):
                e20  = float(c.ewm(span=20,  adjust=False).mean().iloc[-1])
                e50  = float(c.ewm(span=50,  adjust=False).mean().iloc[-1])
                e200 = float(c.ewm(span=min(len(c),200), adjust=False).mean().iloc[-1])
                delta = c.diff()
                gain = delta.clip(lower=0).rolling(14).mean()
                loss_ = (-delta.clip(upper=0)).rolling(14).mean()
                rsi  = float(100-(100/(1+gain.iloc[-1]/(loss_.iloc[-1]+1e-9))))
                t_pts=0
                if cur>e20: t_pts+=1
                if cur>e50: t_pts+=1
                if cur>e200: t_pts+=1
                if e20>e50: t_pts+=1
                if rsi>55:  t_pts+=1
                if cur<e20: t_pts-=1
                if cur<e50: t_pts-=1
                if cur<e200: t_pts-=1
                if e20<e50: t_pts-=1
                if rsi<45:  t_pts-=1
                trend_score = max(-3, min(3, t_pts))

            # ── VWAP — vwap/combo ─────────────────────────
            vwap_score=0; vwap_diff=0.0
            if strategy in ("vwap","combo"):
                vwap_val = calc_vwap_val(hist)
                vwap_diff = (cur-vwap_val)/vwap_val*100 if vwap_val else 0
                thresh = vwap_thresh if strategy=="vwap" else 0.3
                vwap_score = 1 if vwap_diff>thresh else -1 if vwap_diff<-thresh else 0

            # ── 전략별 진입 신호 판단 ─────────────────────
            direction = None

            if strategy == "fvg":
                # FVG만: BULL→LONG, BEAR→SHORT
                if fvg_sig == "BULL": direction = "LONG"
                elif fvg_sig == "BEAR": direction = "SHORT"

            elif strategy == "vwap":
                # VWAP 단독: 가격이 VWAP 위→LONG, 아래→SHORT
                # 진입: VWAP에서 이격 후 다시 VWAP 쪽으로 돌아올 때
                # (전 캔들과 이격 비교로 반전 감지)
                if i > lookback:
                    prev_c   = float(df["Close"].iloc[i-1])
                    prev_vwap = calc_vwap_val(df.iloc[:i])
                    prev_diff = (prev_c-prev_vwap)/prev_vwap*100 if prev_vwap else 0
                    # 과도한 이격 후 VWAP으로 회귀 시도
                    if prev_diff > vwap_thresh*1.5 and vwap_diff < prev_diff:
                        direction = "LONG"   # VWAP 위 → 롱 (추세 방향)
                    elif prev_diff < -vwap_thresh*1.5 and vwap_diff > prev_diff:
                        direction = "SHORT"
                # 대안: VWAP 크로스
                if direction is None and abs(vwap_diff) < vwap_thresh*0.3:
                    if vwap_score == 1:  direction = "LONG"
                    elif vwap_score == -1: direction = "SHORT"

            elif strategy == "combo":
                total = fvg_score + trend_score + vwap_score
                if abs(total) < min_score or fvg_sig=="NONE": continue
                direction = "LONG" if total>0 else "SHORT"
            elif strategy == "emafvg":
                e20_  = float(c.ewm(span=20,  adjust=False).mean().iloc[-1])
                e50_  = float(c.ewm(span=50,  adjust=False).mean().iloc[-1])
                bull_ema = cur>e20_ and cur>e50_ and e20_>e50_
                bear_ema = cur<e20_ and cur<e50_ and e20_<e50_
                if fvg_sig=="BULL" and bull_ema: direction="LONG"
                elif fvg_sig=="BEAR" and bear_ema: direction="SHORT"

            elif strategy == "krma":
                ma20_ = float(c.rolling(20).mean().iloc[-1]) if len(c)>=20 else cur
                ma5_  = float(c.rolling(5).mean().iloc[-1])  if len(c)>=5  else cur
                vol_s = df["Volume"].astype(float)
                avg20_v = float(vol_s.iloc[-21:-1].mean()) if len(vol_s)>=21 else 1
                cur_v   = float(vol_s.iloc[-1])
                vol_r   = cur_v/avg20_v if avg20_v>0 else 1
                if cur>ma20_ and ma5_>ma20_ and vol_r>=1.5: direction="LONG"
                elif cur<ma20_ and ma5_<ma20_ and vol_r>=1.5: direction="SHORT"

            if direction is None: continue

            # ── FVG 기반 진입가/SL/TP 계산 ───────────────
            if fvg_sig != "NONE":
                setup = calc_setup(fvg_info, cur, account, risk_pct)
            else:
                # VWAP 전략: VWAP ±1% SL, VWAP ±2% / ±3% TP
                vwap_val2 = calc_vwap_val(hist)
                buf = vwap_val2 * 0.005
                if direction=="LONG":
                    entry_p = cur
                    sl_p    = vwap_val2 - buf
                    risk_e  = entry_p - sl_p
                    tp1_p   = entry_p + risk_e*2
                    tp2_p   = entry_p + risk_e*3
                else:
                    entry_p = cur
                    sl_p    = vwap_val2 + buf
                    risk_e  = sl_p - entry_p
                    tp1_p   = entry_p - risk_e*2
                    tp2_p   = entry_p - risk_e*3
                setup = {"entry":entry_p,"sl":sl_p,"tp1":tp1_p,"tp2":tp2_p,
                         "sl_pct":round(abs(sl_p-entry_p)/entry_p*100,2)}

            if not setup: continue
            entry_price = setup["entry"]; sl_price = setup["sl"]
            tp1_price   = setup["tp1"];  tp2_price = setup["tp2"]
            risk_per    = abs(entry_price - sl_price)
            if risk_per<=0: continue
            risk_usd = account * risk_pct / 100
            qty      = risk_usd / risk_per

            # ── 시뮬레이션 ────────────────────────────────
            outcome, exit_price, pnl_usd, pnl_pct = sim_trade(
                df, i, direction, entry_price, sl_price, tp1_price, tp2_price, qty, account, risk_pct)

            equity += pnl_usd; total_pnl_usd += pnl_usd
            if equity>max_equity: max_equity=equity
            dd=(max_equity-equity)/max_equity*100
            if dd>max_dd: max_dd=dd

            if pnl_usd>0:         win_count+=1;     win_pnls.append(pnl_pct)
            elif outcome=="timeout": timeout_count+=1; (win_pnls if pnl_usd>0 else loss_pnls).append(pnl_pct)
            else:                 loss_count+=1;    loss_pnls.append(pnl_pct)

            total_sig = (fvg_score+trend_score+vwap_score) if strategy=="combo" else (3 if fvg_sig=="BULL" else -3 if fvg_sig=="BEAR" else 0)
            trades.append({
                "idx": len(trades)+1,
                "direction": direction,
                "entry":  round(entry_price,4), "exit": round(exit_price,4),
                "pnl_usd": round(pnl_usd,2), "pnl_pct": round(pnl_pct,2),
                "outcome": outcome, "score": int(total_sig),
                "fvg_sig": fvg_sig, "diff_pct": round(vwap_diff,2),
            })
            equity_curve.append({"idx":len(trades),"equity":round(equity,2),"outcome":outcome})
            cooldown = 5  # 연속 진입 방지

        # ── 통계 ─────────────────────────────────────────
        total_trades = len(trades)
        win_rate  = win_count/total_trades*100 if total_trades else 0
        avg_win   = float(np.mean(win_pnls))  if win_pnls  else 0
        avg_loss  = float(np.mean(loss_pnls)) if loss_pnls else 0
        gp = sum(t["pnl_usd"] for t in trades if t["pnl_usd"]>0)
        gl = abs(sum(t["pnl_usd"] for t in trades if t["pnl_usd"]<=0))
        pf = round(gp/gl,2) if gl else 0
        ex = round((win_rate/100)*avg_win+(1-win_rate/100)*avg_loss,3)

        ticker_label = ticker if not use_kr else f"{ticker.split('.')[0]} ({kr_market})"
        return jsonify({
            "ticker":ticker_label,"months":months,"strategy":strategy,
            "is_kr_stock":use_kr,
            "currency":"KRW" if use_kr else "USD",
            "min_score":min_score,"vwap_thresh":vwap_thresh,
            "period_start":period_start,"period_end":period_end,
            "trades":trades,"equity_curve":equity_curve,
            "stats":{
                "total_trades":total_trades,"wins":win_count,
                "losses":loss_count,"timeouts":timeout_count,
                "win_rate":round(win_rate,1),
                "avg_win_pct":round(avg_win,2),"avg_loss_pct":round(avg_loss,2),
                "profit_factor":pf,"expectancy":ex,
                "max_drawdown_pct":round(max_dd,2),
                "total_pnl_usd":round(total_pnl_usd,2),
                "total_pnl_pct":round((equity-account)/account*100,2),
                "initial_capital":account,"final_capital":round(equity,2),
            }
        })
    except Exception as e:
        return jsonify({"error": str(e)})


# ── /emafvg ─────────────────────────────────────────────
@app.route("/emafvg", methods=["POST"])
@login_required
def emafvg():
    params    = request.json
    ticker    = params.get("ticker","BTC-USD")
    is_crypto = params.get("is_crypto", True)
    account   = float(params.get("account", 10_000))
    risk_pct  = float(params.get("risk_pct", 1.0))

    coin_names={"BTC-USD":"Bitcoin","ETH-USD":"Ethereum","XRP-USD":"XRP"}

    def get_ema_state(df, price):
        c = df["Close"].astype(float)
        e20  = float(c.ewm(span=20,  adjust=False).mean().iloc[-1])
        e50  = float(c.ewm(span=50,  adjust=False).mean().iloc[-1])
        e200 = float(c.ewm(span=min(len(c),200), adjust=False).mean().iloc[-1])
        # 정렬 판단
        bull_align = (price>e20) and (price>e50) and (price>e200) and (e20>e50) and (e50>e200)
        bear_align = (price<e20) and (price<e50) and (price<e200) and (e20<e50) and (e50<e200)
        partial_bull = (price>e20) and (e20>e50)
        partial_bear = (price<e20) and (e20<e50)
        if bull_align:    aligned="FULL_BULL"
        elif bear_align:  aligned="FULL_BEAR"
        elif partial_bull:aligned="PARTIAL_BULL"
        elif partial_bear:aligned="PARTIAL_BEAR"
        else:             aligned="NONE"
        ema_aligned_str = "FULL" if aligned.startswith("FULL") else "PARTIAL" if aligned.startswith("PARTIAL") else "NONE"
        return {"price":round(price,6),"ema":{"e20":round(e20,6),"e50":round(e50,6),"e200":round(e200,6)},
                "aligned":aligned,"ema_aligned":ema_aligned_str,
                "bull":bull_align or partial_bull,"bear":bear_align or partial_bear}

    def ema_confluence(fvg_info, ema_state):
        """FVG 구간이 EMA 레벨과 얼마나 겹치는지"""
        if fvg_info["signal"]=="NONE": return []
        gl, gh = fvg_info["gap_low"], fvg_info["gap_high"]
        tags=[]
        for name,val in [("EMA20",ema_state["ema"]["e20"]),
                          ("EMA50",ema_state["ema"]["e50"]),
                          ("EMA200",ema_state["ema"]["e200"])]:
            if gl<=val<=gh:
                tags.append({"text":f"✅ {name} FVG 안에 위치 (최강 컨플루언스)","ok":True,"warn":False})
            elif abs(val-gh)/gh<0.005 or abs(val-gl)/gl<0.005:
                tags.append({"text":f"⚡ {name} FVG 엣지 근접","ok":False,"warn":True})
        return tags

    def build_setup(fvg_info, ema_state, tf, price, account_, risk_pct_):
        if fvg_info["signal"]=="NONE": return None
        sig=fvg_info["signal"]
        # EMA 방향과 FVG 방향 일치 여부
        ema_bull=ema_state["bull"]; ema_bear=ema_state["bear"]
        if sig=="BULL" and not ema_bull: return None   # 방향 불일치
        if sig=="BEAR" and not ema_bear: return None

        setup=calc_setup(fvg_info, price, account_, risk_pct_)
        if not setup: return None

        # 컨플루언스 태그
        cf_tags=ema_confluence(fvg_info, ema_state)
        aligned=ema_state["ema_aligned"]

        # 퀄리티 판정
        cf_count=sum(1 for t in cf_tags if t["ok"])
        if aligned=="FULL" and cf_count>=1: quality="HIGH"
        elif aligned in ("FULL","PARTIAL") and (cf_count>=1 or aligned=="FULL"): quality="MID"
        else: quality="LOW"

        # 기본 태그
        base_tags=[
            {"text":f"EMA 정렬: {'완전 ✅' if aligned=='FULL' else '부분 ⚡' if aligned=='PARTIAL' else '없음 ❌'}","ok":aligned=="FULL","warn":aligned=="PARTIAL"},
            {"text":f"FVG: {'불리시 🟢' if sig=='BULL' else '베어리시 🔴'}","ok":sig=="BULL","warn":False},
            {"text":f"추세 일치: {'✅' if (sig=='BULL')==ema_bull else '⚡'}","ok":(sig=="BULL")==ema_bull,"warn":not((sig=="BULL")==ema_bull)},
        ]
        all_tags=base_tags+cf_tags

        # 진입 이유 설명
        direction="LONG" if sig=="BULL" else "SHORT"
        e=ema_state["ema"]
        reason_parts=[]
        if aligned=="FULL":
            reason_parts.append(f"<strong>EMA 완전 정렬</strong> (EMA20{'>'if sig=='BULL' else '<'}EMA50{'>'if sig=='BULL' else '<'}EMA200)")
        if cf_count>0:
            reason_parts.append(f"<strong>EMA-FVG 컨플루언스</strong> — EMA가 FVG 구간 안에 위치해 지지/저항 강화")
        reason_parts.append(f"FVG 50% ({round(fvg_info['gap_mid'],4)})까지 <strong>리테스트 대기</strong> 후 반응 캔들 확인하여 진입")
        reason=f"[{tf}] " + " · ".join(reason_parts)

        # 진입가와 현재가의 차이
        entry_diff=round((setup["entry"]-price)/price*100,2)
        qty=setup["shares"]
        invest_amt=round(qty*setup["entry"],2)

        return {
            "tf":tf,"direction":direction,"quality":quality,
            "fvg_high":fvg_info["gap_high"],"fvg_low":fvg_info["gap_low"],"fvg_mid":fvg_info["gap_mid"],
            "current":round(price,6),"entry":setup["entry"],
            "entry_diff":entry_diff,"sl":setup["sl"],"sl_pct":setup["sl_pct"],
            "tp1":setup["tp1"],"tp1_pct":setup["tp1_pct"],
            "tp2":setup["tp2"],"tp2_pct":setup["tp2_pct"],
            "tp3":setup["tp3"],"tp3_pct":setup["tp3_pct"],
            "qty":qty,"invest_amt":invest_amt,
            "risk_amt":setup["risk_amt"],"rr":setup["rr"],
            "confluence_tags":all_tags,"reason":reason,
        }

    try:
        # 가격 수집
        if is_crypto:
            price, chg, name = binance_price(ticker)
            cls="btc" if "BTC" in ticker else "eth" if "ETH" in ticker else "xrp"
        else:
            stk=yf.Ticker(ticker); info=stk.info
            price=float(info.get("currentPrice") or info.get("regularMarketPrice") or info.get("previousClose") or 0)
            prev=float(info.get("regularMarketPreviousClose") or info.get("previousClose") or price)
            chg=round((price-prev)/prev*100,2) if prev else 0
            name=info.get("longName") or info.get("shortName") or ticker.split(".")[0]
            cls="stock"

        # 타임프레임별 데이터
        tf_configs={"1h":("1h",200),"4h":("4h",200),"1d":("1d",300)}
        tf_results={}
        for tf,(iv,lim) in tf_configs.items():
            try:
                if is_crypto:
                    df=binance_ohlcv(ticker, tf, limit=lim)
                else:
                    stk=yf.Ticker(ticker)
                    period_map={"1h":"60d","4h":"60d","1d":"1y"}
                    df_raw=stk.history(period=period_map[tf],interval="1h" if tf=="4h" else iv)
                    if tf=="4h":
                        df=df_raw.resample("4h").agg({"Open":"first","High":"max","Low":"min","Close":"last","Volume":"sum"}).dropna()
                    else:
                        df=df_raw
                if df.empty or len(df)<20:
                    continue
                ema_st=get_ema_state(df, price)
                fvg_inf=detect_fvg(df)
                tf_results[tf]={"ema":ema_st["ema"],"aligned":ema_st["aligned"],
                                 "ema_aligned":ema_st["ema_aligned"],
                                 "fvg":fvg_inf,"price":round(price,6)}
            except: continue

        # 타점 생성
        setups=[]
        for tf in ["1d","4h","1h"]:
            t=tf_results.get(tf)
            if not t: continue
            ema_st={"ema":t["ema"],"aligned":t["aligned"],"ema_aligned":t["ema_aligned"],
                    "bull":"BULL" in t["aligned"],"bear":"BEAR" in t["aligned"]}
            s=build_setup(t["fvg"], ema_st, tf, price, account, risk_pct)
            if s: setups.append(s)

        # 품질 순 정렬
        quality_order={"HIGH":0,"MID":1,"LOW":2}
        setups.sort(key=lambda x:quality_order.get(x["quality"],3))

        return jsonify({
            "ticker":ticker,"name":name,"price":round(price,6),"change":chg,
            "is_crypto":is_crypto,"cls":cls,
            "timeframes":tf_results,
            "setups":setups[:6],
        })
    except Exception as e:
        return jsonify({"error":str(e)})


# ── /krma ───────────────────────────────────────────────
@app.route("/krma", methods=["POST"])
@login_required
def krma():
    params  = request.json
    code    = params.get("code","").strip().zfill(6)
    market  = params.get("market","KS")
    account = float(params.get("account", 10_000_000))
    risk_pct= float(params.get("risk_pct", 1.0))
    ticker  = f"{code}.{market}"

    try:
        stk  = yf.Ticker(ticker)
        info = stk.info
        price= float(info.get("currentPrice") or info.get("regularMarketPrice") or info.get("previousClose") or 0)
        prev = float(info.get("regularMarketPreviousClose") or info.get("previousClose") or price)
        chg  = round((price-prev)/prev*100, 2) if prev else 0
        name = info.get("longName") or info.get("shortName") or code

        # 일봉 180일 수집
        df = stk.history(period="1y", interval="1d")
        if df.empty or len(df) < 30:
            return jsonify({"error": "데이터 부족 — yfinance에서 데이터를 가져올 수 없습니다"})

        c  = df["Close"].astype(float)
        v  = df["Volume"].astype(float)
        hi = df["High"].astype(float)
        lo = df["Low"].astype(float)

        # 이동평균
        ma5   = float(c.rolling(5).mean().iloc[-1])   if len(c)>=5   else None
        ma20  = float(c.rolling(20).mean().iloc[-1])  if len(c)>=20  else None
        ma60  = float(c.rolling(60).mean().iloc[-1])  if len(c)>=60  else None
        ma120 = float(c.rolling(120).mean().iloc[-1]) if len(c)>=120 else None

        # 거래량
        today_vol = float(v.iloc[-1])
        avg5_vol  = float(v.iloc[-6:-1].mean()) if len(v)>=6 else today_vol
        avg20_vol = float(v.iloc[-21:-1].mean()) if len(v)>=21 else today_vol
        vol_ratio = today_vol / avg20_vol if avg20_vol > 0 else 1.0

        # 5일 고가/저가 (저항/지지)
        recent_high = float(hi.iloc[-6:-1].max()) if len(hi)>=6 else price
        recent_low  = float(lo.iloc[-6:-1].min()) if len(lo)>=6 else price

        # ATR (14일) for dynamic SL
        df2 = df.copy(); df2["prev_close"]=df2["Close"].shift(1)
        df2["tr"] = df2.apply(lambda r: max(r["High"]-r["Low"], abs(r["High"]-r["prev_close"]), abs(r["Low"]-r["prev_close"])) if not pd.isna(r["prev_close"]) else r["High"]-r["Low"], axis=1)
        atr = float(df2["tr"].rolling(14).mean().iloc[-1]) if len(df2)>=14 else price*0.02

        def make_setup(entry, sl, direction, strategy, tags, reason, sl_basis):
            risk = abs(entry - sl)
            if risk <= 0: return None
            risk_amt = account * risk_pct / 100
            shares   = max(1, int(risk_amt / risk))
            rr = round(risk*2/risk, 2)
            tp1 = round(entry + risk*2 if direction=="BUY" else entry - risk*2, 0)
            tp2 = round(entry + risk*3 if direction=="BUY" else entry - risk*3, 0)
            tp3 = round(entry + risk*5 if direction=="BUY" else entry - risk*5, 0)
            sl_pct  = round(abs(sl-entry)/entry*100, 1)
            tp1_pct = round(abs(tp1-entry)/entry*100, 1)
            tp2_pct = round(abs(tp2-entry)/entry*100, 1)
            tp3_pct = round(abs(tp3-entry)/entry*100, 1)
            entry_diff = round((entry-price)/price*100, 1)
            return {
                "type": direction, "strategy": strategy,
                "entry": round(entry,0), "entry_diff": entry_diff,
                "sl": round(sl,0), "sl_pct": sl_pct, "sl_basis": sl_basis,
                "tp1": round(tp1,0), "tp1_pct": tp1_pct,
                "tp2": round(tp2,0), "tp2_pct": tp2_pct,
                "tp3": round(tp3,0), "tp3_pct": tp3_pct,
                "shares": shares, "invest_amt": round(shares*entry),
                "risk_amt": round(risk_amt), "rr": 2.0, "tags": tags, "reason": reason,
            }

        signals = []

        # ── 전략 1: 20일선 돌파 + 거래량 급증 ──────────
        if ma20:
            prev_close = float(c.iloc[-2]) if len(c)>=2 else price
            golden = (prev_close < ma20) and (price >= ma20)   # 오늘 돌파
            near   = abs(price - ma20) / ma20 < 0.015           # 1.5% 이내
            if (golden or near) and vol_ratio >= 1.5:
                tags = [
                    {"text": f"20일선 {'돌파 ✅' if golden else '근접 ⚡'}", "ok": golden, "warn": near},
                    {"text": f"거래량 {vol_ratio:.1f}배 {'🔥' if vol_ratio>=2 else '📈'}", "ok": vol_ratio>=2, "warn": vol_ratio>=1.5},
                    {"text": f"{'이평선 상승정렬 ✅' if ma5 and ma20 and ma5>ma20 else '이평선 미정렬 ⚠️'}", "ok": bool(ma5 and ma20 and ma5>ma20), "warn": not bool(ma5 and ma20 and ma5>ma20)},
                ]
                entry = price
                sl    = round(ma20 * 0.985, 0)   # 20일선 -1.5%
                reason = f"<strong>20일선 {'돌파' if golden else '근접'}</strong> + 거래량 평균의 {vol_ratio:.1f}배 급증 — " \
                         f"기관/세력 진입 가능성. 20일선({int(ma20):,}원) 아래로 이탈 시 손절."
                s = make_setup(entry, sl, "BUY", "20일선 돌파+거래량", tags, reason, "20일선 -1.5%")
                if s: signals.append(s)

        # ── 전략 2: 5일선 눌림목 매수 (상승추세 중) ─────
        if ma5 and ma20 and ma5 > ma20 and price <= ma5 * 1.005:
            bull_align = ma20 and ma60 and ma20 > ma60
            tags = [
                {"text": "5일선 눌림목 ✅", "ok": True, "warn": False},
                {"text": f"5일선>20일선 {'✅' if ma5>ma20 else '❌'}", "ok": bool(ma5>ma20), "warn": False},
                {"text": f"20일선>60일선 {'✅' if bull_align else '⚠️'}", "ok": bool(bull_align), "warn": not bool(bull_align)},
            ]
            entry = price
            sl    = round(ma5 * 0.97, 0)   # 5일선 -3%
            reason = f"<strong>5일선 눌림목</strong> — 상승추세 중 5일선까지 조정 후 반등 노림. " \
                     f"5일선({int(ma5):,}원) 하향 이탈 시 손절."
            s = make_setup(entry, sl, "BUY", "5일선 눌림목", tags, reason, "5일선 -3%")
            if s: signals.append(s)

        # ── 전략 3: 60일선 지지 매수 (중기 추세) ────────
        if ma60 and abs(price - ma60) / ma60 < 0.02 and (ma120 is None or price > ma120):
            tags = [
                {"text": "60일선 지지 구간 ✅", "ok": True, "warn": False},
                {"text": f"거래량 {vol_ratio:.1f}배", "ok": vol_ratio>=1.5, "warn": vol_ratio<1.5},
                {"text": f"120일선 위 {'✅' if ma120 and price>ma120 else '⚠️'}", "ok": bool(ma120 and price>ma120), "warn": not bool(ma120 and price>ma120)},
            ]
            entry = price
            sl    = round(ma60 * 0.97, 0)
            reason = f"<strong>60일선 지지 구간</strong> — 기관 매수 기준선인 60일선({int(ma60):,}원)에서 지지 반응 확인 후 진입. " \
                     f"60일선 아래 이탈 시 손절."
            s = make_setup(entry, sl, "BUY", "60일선 지지", tags, reason, "60일선 -3%")
            if s: signals.append(s)

        # ── 전략 4: 이평선 역배열 + 거래량 → 매도 관망 ─
        bear_align = ma5 and ma20 and ma60 and ma5 < ma20 and ma20 < ma60
        if bear_align and price < ma20:
            tags = [
                {"text": "역배열 (5<20<60) ❌", "ok": False, "warn": False},
                {"text": "20일선 아래 ❌", "ok": False, "warn": False},
                {"text": "매수 금지 구간", "ok": False, "warn": True},
            ]
            entry = price
            sl    = round(price * 1.03, 0)
            reason = f"<strong>이평선 역배열</strong> — 5일선({int(ma5):,})&lt;20일선({int(ma20):,})&lt;60일선({int(ma60):,}) " \
                     f"하락 구조. 매수 진입 금지, 반등 시 매도 또는 관망 권장."
            s = make_setup(entry, sl, "SELL", "역배열 매도/관망", tags, reason, "진입가 +3%")
            if s: signals.append(s)

        # ── 외국인 · 기관 수급 (FinanceDataReader) ────────
        investor = {}
        try:
            from_inv = (datetime.today() - timedelta(days=30)).strftime("%Y-%m-%d")
            to_inv   = datetime.today().strftime("%Y-%m-%d")
            df_inv = fdr.DataReader(code, from_inv, to_inv)
            if df_inv is not None and not df_inv.empty:
                # FDR 컬럼: Close, Chg, ChgRate, Volume, Marcap, Shares
                # 수급 컬럼: Inst, Forg (있는 경우)
                cols = df_inv.columns.tolist()

                def get_inv_col(df, candidates):
                    for c2 in candidates:
                        if c2 in df.columns:
                            return df[c2].astype(float)
                    return None

                forg_s = get_inv_col(df_inv, ['Forg','Foreign','외국인'])
                inst_s = get_inv_col(df_inv, ['Inst','Institution','기관'])

                # 최근 5일 / 20일 누적 순매수
                def net_summary(s):
                    if s is None or len(s) < 1:
                        return None
                    s = s.fillna(0)
                    d5  = int(s.iloc[-5:].sum())  if len(s)>=5  else int(s.sum())
                    d20 = int(s.iloc[-20:].sum()) if len(s)>=20 else int(s.sum())
                    today_val = int(s.iloc[-1])
                    trend = "매수우세" if d5 > 0 else "매도우세" if d5 < 0 else "중립"
                    return {
                        "today":  today_val,
                        "5d":     d5,
                        "20d":    d20,
                        "trend":  trend,
                        "series": [int(x) for x in s.iloc[-20:].tolist()],
                    }

                investor = {
                    "foreign":     net_summary(forg_s),
                    "institution": net_summary(inst_s),
                }
        except Exception:
            investor = {}

        return jsonify({
            "code": code, "ticker": ticker, "name": name, "market": market,
            "price": round(price, 0), "change": chg,
            "ma": {
                "ma5":   round(ma5,0)   if ma5   else None,
                "ma20":  round(ma20,0)  if ma20  else None,
                "ma60":  round(ma60,0)  if ma60  else None,
                "ma120": round(ma120,0) if ma120 else None,
            },
            "volume": {
                "today": round(today_vol,0),
                "avg5":  round(avg5_vol,0),
                "avg20": round(avg20_vol,0),
                "ratio": round(vol_ratio,2),
            },
            "investor": investor,
            "signals": signals,
        })
    except Exception as e:
        return jsonify({"error": str(e)})


# ── /vol_scan ────────────────────────────────────────────
@app.route("/vol_scan", methods=["POST"])
@login_required
def vol_scan():
    params    = request.json
    days      = int(params.get("days", 3))
    market    = params.get("market", "ALL")
    min_ratio = float(params.get("min_ratio", 50)) / 100 + 1   # e.g. 50% → ratio >= 1.5
    limit     = int(params.get("limit", 50))
    needed    = days * 3 + 5   # 최소 필요 일봉 수

    try:
        # 상장 종목 리스트 수집
        mkts = []
        if market in ("ALL","KOSPI"):  mkts.append(("KOSPI","코스피"))
        if market in ("ALL","KOSDAQ"): mkts.append(("KOSDAQ","코스닥"))

        stock_list = []
        for mkt_code, mkt_label in mkts:
            try:
                df_list = fdr.StockListing(mkt_code)[["Code","Name"]]
                df_list["market"] = mkt_label
                stock_list.append(df_list)
            except: pass
        if not stock_list:
            return jsonify({"error": "종목 리스트를 불러올 수 없습니다"})

        all_stocks = pd.concat(stock_list, ignore_index=True)
        # 코드가 6자리 숫자인 것만 (ETF 등 제외)
        all_stocks = all_stocks[all_stocks["Code"].str.match(r"^\d{6}$")].reset_index(drop=True)

        results = []
        scanned = 0
        today = datetime.today()
        from_dt = (today - timedelta(days=max(days*3+30, 60))).strftime("%Y-%m-%d")
        to_dt   = today.strftime("%Y-%m-%d")

        for _, row in all_stocks.iterrows():
            code = row["Code"]; name = row["Name"]; mkt_label = row["market"]
            try:
                df = fdr.DataReader(code, from_dt, to_dt)
                if df is None or len(df) < needed:
                    continue

                # 거래일 기준 최근 days일 vs 직전 days일
                recent  = df.iloc[-days:]
                prev    = df.iloc[-(days*2):-days]
                if len(recent) < days or len(prev) < days:
                    continue

                recent_vol = float(recent["Volume"].mean())
                prev_vol   = float(prev["Volume"].mean())
                if prev_vol <= 0: continue

                vol_ratio = recent_vol / prev_vol
                if vol_ratio < min_ratio: continue

                # 양봉 비율 (Close >= Open = 매수 추정)
                recent_bull = recent.apply(lambda r: r["Close"] >= r["Open"], axis=1).mean() * 100

                # 현재가 + 등락률
                price  = float(df["Close"].iloc[-1])
                prev_p = float(df["Close"].iloc[-2]) if len(df)>=2 else price
                change = round((price-prev_p)/prev_p*100, 2) if prev_p else 0

                scanned += 1
                results.append({
                    "code":       code,
                    "name":       name,
                    "market":     mkt_label,
                    "price":      int(price),
                    "change":     change,
                    "vol_ratio":  round(vol_ratio, 2),
                    "recent_vol": round(recent_vol, 0),
                    "prev_vol":   round(prev_vol, 0),
                    "bull_pct":   round(recent_bull, 1),
                })

            except: continue

        # 거래량 증가율 내림차순 정렬
        results.sort(key=lambda x: x["vol_ratio"], reverse=True)

        return jsonify({
            "days":      days,
            "market":    market,
            "min_ratio": int((min_ratio-1)*100),
            "scanned":   scanned,
            "results":   results[:limit],
        })
    except Exception as e:
        return jsonify({"error": str(e)})


# ── /cross_scan ──────────────────────────────────────────
@app.route("/cross_scan", methods=["POST"])
@login_required
def cross_scan():
    params        = request.json
    types         = params.get("types", ["surge","high"])
    market        = params.get("market", "ALL")
    period        = int(params.get("period", 30))
    minpct        = float(params.get("minpct", 20))
    vol_days      = int(params.get("vol_days", 3))
    vol_min_ratio = float(params.get("vol_min_ratio", 1.3))

    try:
        # 1. 종목 리스트
        mkts = []
        if market in ("ALL","KOSPI"):  mkts.append(("KOSPI","코스피","KS"))
        if market in ("ALL","KOSDAQ"): mkts.append(("KOSDAQ","코스닥","KQ"))

        stock_rows = []
        for mkt_code, mkt_label, suffix in mkts:
            try:
                df_l = fdr.StockListing(mkt_code)[["Code","Name"]]
                df_l = df_l[df_l["Code"].str.match(r"^\d{6}$")]
                df_l["market"]  = mkt_label
                df_l["suffix"]  = suffix
                stock_rows.append(df_l)
            except: pass
        if not stock_rows:
            return jsonify({"error": "종목 리스트 수집 실패"})

        all_stocks = pd.concat(stock_rows, ignore_index=True)

        # 2. yfinance 배치 다운로드 (청크 단위)
        today = datetime.today()
        from_dt = (today - timedelta(days=max(period+5, vol_days*3+20))).strftime("%Y-%m-%d")
        to_dt   = today.strftime("%Y-%m-%d")

        # yfinance ticker 형식: XXXXXX.KS or XXXXXX.KQ
        all_stocks["yf_ticker"] = all_stocks["Code"] + "." + all_stocks["suffix"]
        tickers_yf = all_stocks["yf_ticker"].tolist()

        # 배치 다운로드 — 청크 50개, 청크 간 1.5초 딜레이 (야후 레이트리밋 방지)
        CHUNK = 50
        all_data = {}  # ticker -> df
        import time as _time

        for i in range(0, len(tickers_yf), CHUNK):
            chunk = tickers_yf[i:i+CHUNK]
            for attempt in range(2):   # 실패 시 1회 재시도
                try:
                    raw = yf.download(
                        chunk, start=from_dt, end=to_dt,
                        interval="1d", group_by="ticker",
                        auto_adjust=True, progress=False,
                        threads=False, timeout=20   # threads=False → 레이트리밋 방지
                    )
                    if raw is not None and not raw.empty:
                        if isinstance(raw.columns, pd.MultiIndex):
                            for tk in chunk:
                                try:
                                    df_tk = raw[tk].dropna(how="all")
                                    if not df_tk.empty and len(df_tk) > 3:
                                        all_data[tk] = df_tk
                                except: pass
                        else:
                            if len(chunk)==1 and not raw.empty:
                                all_data[chunk[0]] = raw.dropna(how="all")
                    break   # 성공 시 재시도 루프 탈출
                except Exception as e:
                    err_str = str(e).lower()
                    if "rate" in err_str or "429" in err_str or "too many" in err_str:
                        _time.sleep(5)   # 레이트리밋 → 5초 대기 후 재시도
                    elif attempt == 0:
                        _time.sleep(1)
                    continue
            _time.sleep(1.5)   # 청크 간 1.5초 간격

        # 3. 조건별 분석
        results = []
        ticker_map = dict(zip(all_stocks["yf_ticker"], zip(all_stocks["Code"], all_stocks["Name"], all_stocks["market"])))

        for yf_tk, (code, name, mkt_label) in ticker_map.items():
            df = all_data.get(yf_tk)
            if df is None or len(df) < max(8, vol_days*2+2):
                continue

            # 컬럼 정규화
            df = df.copy()
            df.columns = [c if isinstance(c,str) else c[0] for c in df.columns]
            if "Close" not in df.columns: continue

            try:
                price  = float(df["Close"].iloc[-1])
                prev_p = float(df["Close"].iloc[-2]) if len(df)>=2 else price
                change = round((price-prev_p)/prev_p*100,2) if prev_p else 0

                matched = []
                surge_pct = high_pct = vol_ratio_val = None

                # ── 급등 조건 ──
                if "surge" in types:
                    p_df = df.tail(period) if len(df)>=period else df
                    sp_ = float(p_df["Close"].iloc[0])
                    pct = (price-sp_)/sp_*100 if sp_ else 0
                    surge_pct = round(pct,2)
                    if pct >= minpct:
                        matched.append("surge")

                # ── 전고점 돌파 ──
                if "high" in types:
                    cutoff = today - timedelta(days=30)
                    df.index = pd.to_datetime(df.index).tz_localize(None)
                    cutoff = pd.Timestamp(cutoff).tz_localize(None)
                    before = df[df.index < cutoff]
                    recent = df[df.index >= cutoff]
                    if len(before)>=5 and len(recent)>=3 and "High" in df.columns:
                        ph = float(before["High"].max())
                        rh = float(recent["High"].max())
                        if ph>0 and rh>ph:
                            high_pct = round((rh-ph)/ph*100,2)
                            matched.append("high")

                # ── 매수거래량 증가 ──
                if "vol" in types and "Volume" in df.columns:
                    v = df["Volume"].astype(float)
                    if len(v) >= vol_days*2:
                        rv = float(v.iloc[-vol_days:].mean())
                        pv = float(v.iloc[-(vol_days*2):-vol_days].mean())
                        if pv>0:
                            vr = rv/pv
                            vol_ratio_val = round(vr,2)
                            if vr >= vol_min_ratio:
                                matched.append("vol")

                # 교집합 확인
                if all(t in matched for t in types):
                    results.append({
                        "code":          code,
                        "name":          name,
                        "market":        mkt_label,
                        "price":         int(price),
                        "change":        change,
                        "match_count":   len(matched),
                        "matched_types": matched,
                        "surge_pct":     surge_pct,
                        "high_pct":      high_pct,
                        "vol_ratio":     vol_ratio_val,
                    })
            except: continue

        results.sort(key=lambda x: (-x["match_count"], -(x["surge_pct"] or 0)))
        return jsonify({"types":types,"market":market,"total":len(results),"results":results[:100]})

    except Exception as e:
        return jsonify({"error": str(e)})

# ── /retracement ─────────────────────────────────────────
@app.route("/retracement", methods=["POST"])
@login_required
def retracement():
    params    = request.json
    ticker    = params.get("ticker","BTC-USD")
    is_crypto = params.get("is_crypto", True)
    tf        = params.get("tf","5m")
    ma_period = int(params.get("ma_period", 22))
    account   = float(params.get("account", 10000))
    risk_pct  = float(params.get("risk_pct", 1.0))

    # TF → yfinance/binance 설정
    TF_YF = {"1m":("1m","1d"),"5m":("5m","5d"),"15m":("15m","5d"),"1h":("1h","60d")}
    TF_BNB = {"1m":"1m","5m":"5m","15m":"15m","1h":"1h"}
    TF_LIM = {"1m":300,"5m":300,"15m":200,"1h":200}

    try:
        if is_crypto_ticker(ticker):
            # 바이낸스
            price, change, name = binance_price(ticker)
            bnb_tf = TF_BNB.get(tf, "5m")
            lim    = TF_LIM.get(tf, 200)
            df = binance_ohlcv(ticker, bnb_tf, limit=lim)
        else:
            stk = yf.Ticker(ticker)
            try:
                info   = stk.info
                price  = float(info.get("currentPrice") or info.get("regularMarketPrice") or info.get("previousClose") or 0)
                prev   = float(info.get("regularMarketPreviousClose") or info.get("previousClose") or price)
                change = round((price-prev)/prev*100,2) if prev else 0
                name   = info.get("longName") or info.get("shortName") or ticker.split(".")[0]
            except Exception:
                price, change, name = 0, 0, ticker.split(".")[0]
            yf_iv, yf_period = TF_YF.get(tf, ("5m","5d"))
            df = stk.history(period=yf_period, interval=yf_iv)

        if df is None or df.empty or len(df) < ma_period + 5:
            return jsonify({"error": f"데이터 부족 — {tf}봉 데이터를 충분히 가져올 수 없습니다"})

        c = df["Close"].astype(float)
        h = df["High"].astype(float)
        l = df["Low"].astype(float)

        if not price and len(c) > 0:
            price = float(c.iloc[-1])

        # MA 계산
        ma = float(c.rolling(ma_period).mean().iloc[-1])
        if ma <= 0:
            return jsonify({"error": "MA 계산 실패 — 데이터 부족"})

        # 괴리율 (음수 = 가격이 MA 아래 = 롱 기회)
        div_pct = round((price - ma) / ma * 100, 3)

        # 최근 스윙 고저점 (진입/손절 기준)
        recent_n = min(50, len(df) - 1)
        swing_high = float(h.iloc[-recent_n:].max())
        swing_low  = float(l.iloc[-recent_n:].min())
        move_size  = swing_high - swing_low   # 최근 급등락 폭

        # 신호 판단
        abs_div = abs(div_pct)
        if div_pct <= -3:
            sig = {"type":"LONG","strength":3,"title":"🔥 강한 롱 진입 신호","desc":f"이평선 아래 {abs_div:.1f}% 이격 — 되돌림 확률 매우 높음. 전략 A/B 모두 적극 활용"}
        elif div_pct <= -1.5:
            sig = {"type":"LONG","strength":2,"title":"📈 롱 진입 신호","desc":f"이평선 아래 {abs_div:.1f}% 이격 — 되돌림 발생 가능. 이격 3% 이상 기다리면 승률 ↑"}
        elif div_pct >= 3:
            sig = {"type":"SHORT","strength":3,"title":"🔥 강한 숏 진입 신호","desc":f"이평선 위 {abs_div:.1f}% 이격 — 과매수. 하락 되돌림 확률 높음"}
        elif div_pct >= 1.5:
            sig = {"type":"SHORT","strength":2,"title":"📉 숏 진입 신호","desc":f"이평선 위 {abs_div:.1f}% 이격 — 하락 조짐. 이격 3% 이상 확인 후 진입"}
        else:
            sig = {"type":"NEUTRAL","strength":0,"title":"⚖️ 대기 — 이격 부족","desc":f"현재 괴리 {abs_div:.1f}% — 이평선 근접. 급등락 후 3% 이상 이격 발생 시 진입 기회"}

        # ── 전략 A 타점 계산 ──
        # 진입: 현재가 (이격 발생 시점)
        entry_a = price
        # 롱이면 MA 방향으로 25% 되돌림 = entry + move * 0.25
        # 음수 div = 가격이 MA 아래 → 위로 25% 되돌림
        direction = 1 if div_pct < 0 else -1   # 1=롱, -1=숏
        tp_a   = round(entry_a + move_size * 0.25 * direction, 4)
        sl_a   = round(entry_a * (1 - 0.01 * direction), 4)
        tp_pct_a  = round(abs(tp_a - entry_a) / entry_a * 100, 3)
        sl_pct_a  = round(abs(sl_a - entry_a) / entry_a * 100, 3)
        risk_amt_a = account * risk_pct / 100
        shares_a   = risk_amt_a / max(abs(entry_a - sl_a), entry_a * 0.001)
        invest_a   = shares_a * entry_a

        strat_a = {
            "entry":   round(entry_a, 6),
            "tp":      round(tp_a, 6),
            "tp_pct":  tp_pct_a,
            "sl":      round(sl_a, 6),
            "sl_pct":  sl_pct_a,
            "shares":  round(shares_a, 4) if is_crypto else int(max(1, shares_a)),
            "invest":  round(invest_a, 2),
            "risk_amt":round(risk_amt_a, 2),
        }

        # ── 전략 B 타점 계산 ──
        tp1_b     = tp_a                              # 25% → 1차 익절
        tp1_pct_b = tp_pct_a
        reentry_b = round(entry_a + move_size * 0.15 * direction, 6)  # 15% 눌림목 재진입
        sl_b      = round(swing_low if direction==1 else swing_high, 6)  # 직전 스윙 저점/고점
        rr_move   = abs(reentry_b - sl_b)
        tp2_b     = round(reentry_b + rr_move * 2 * direction, 6)
        tp3_b     = round(reentry_b + rr_move * 3 * direction, 6)
        tp2_pct_b = round(abs(tp2_b - entry_a) / entry_a * 100, 3)
        tp3_pct_b = round(abs(tp3_b - entry_a) / entry_a * 100, 3)

        strat_b = {
            "tp1":      round(tp1_b, 6),
            "tp1_pct":  tp1_pct_b,
            "reentry":  round(reentry_b, 6),
            "tp2":      round(tp2_b, 6),
            "tp2_pct":  tp2_pct_b,
            "tp3":      round(tp3_b, 6),
            "tp3_pct":  tp3_pct_b,
            "sl":       round(sl_b, 6),
        }

        return jsonify({
            "ticker":     ticker, "name": name,
            "price":      round(price, 6),
            "change":     change,
            "is_crypto":  is_crypto,
            "tf":         tf,
            "ma_period":  ma_period,
            "ma_val":     round(ma, 6),
            "divergence": {"pct": div_pct, "abs": abs_div},
            "signal":     sig,
            "strategy_a": strat_a,
            "strategy_b": strat_b,
            "swing_high": round(swing_high, 6),
            "swing_low":  round(swing_low, 6),
            "move_size":  round(move_size, 6),
        })
    except Exception as e:
        return jsonify({"error": str(e)})


# ── /fvg_scan ─────────────────────────────────────────────
@app.route("/fvg_scan", methods=["POST"])
@login_required
def fvg_scan():
    params        = request.json
    market        = params.get("market", "ALL")
    tf            = params.get("tf", "1d")
    lookback      = int(params.get("lookback", 60))
    min_fvg_pct   = float(params.get("min_fvg_pct", 0.5))
    tolerance_pct = float(params.get("tolerance_pct", 10))
    limit         = int(params.get("limit", 50))

    TF_MAP = {"1d":("1d","1y"), "4h":("1h","60d"), "1h":("1h","60d")}

    try:
        # ── STEP 1: FDR로 거래량/시가총액 상위 종목만 추출 ──
        mkts = []
        if market in ("ALL","KOSPI"):  mkts.append(("KOSPI","코스피","KS"))
        if market in ("ALL","KOSDAQ"): mkts.append(("KOSDAQ","코스닥","KQ"))

        candidates = []
        for mkt_code, mkt_label, suffix in mkts:
            try:
                df_list = fdr.StockListing(mkt_code)
                df_list = df_list[df_list["Code"].str.match(r"^\d{6}$")]
                # 거래량 또는 시가총액 컬럼 있으면 상위만 선택
                vol_col = next((c for c in ["Volume","거래량","Marcap"] if c in df_list.columns), None)
                if vol_col:
                    df_list = df_list.nlargest(min(300, len(df_list)), vol_col)
                else:
                    df_list = df_list.head(300)
                df_list = df_list[["Code","Name"]].copy()
                df_list["market"]     = mkt_label
                df_list["suffix"]     = suffix
                df_list["yf_ticker"]  = df_list["Code"] + "." + suffix
                candidates.append(df_list)
            except: pass

        if not candidates:
            return jsonify({"error": "종목 리스트 수집 실패"})

        all_stocks = pd.concat(candidates, ignore_index=True)
        tickers_yf = all_stocks["yf_ticker"].tolist()

        # ── STEP 2: 배치 다운로드 (청크 50, 딜레이 1s) ──
        yf_iv, yf_period = TF_MAP.get(tf, ("1d","1y"))
        import time as _time
        CHUNK = 50
        all_data = {}

        for i in range(0, len(tickers_yf), CHUNK):
            chunk = tickers_yf[i:i+CHUNK]
            for attempt in range(2):
                try:
                    raw = yf.download(chunk, period=yf_period, interval=yf_iv,
                                      group_by="ticker", auto_adjust=True,
                                      progress=False, threads=False, timeout=20)
                    if raw is None or raw.empty: break
                    if isinstance(raw.columns, pd.MultiIndex):
                        for tk in chunk:
                            try:
                                df_tk = raw[tk].dropna(how="all")
                                if df_tk.empty or len(df_tk) < 10: continue
                                if tf == "4h":
                                    df_tk = df_tk.resample("4h").agg({"Open":"first","High":"max","Low":"min","Close":"last","Volume":"sum"}).dropna()
                                all_data[tk] = df_tk
                            except: pass
                    elif len(chunk) == 1 and not raw.empty:
                        df_tk = raw.dropna(how="all")
                        if tf == "4h":
                            df_tk = df_tk.resample("4h").agg({"Open":"first","High":"max","Low":"min","Close":"last","Volume":"sum"}).dropna()
                        all_data[chunk[0]] = df_tk
                    break
                except Exception as e:
                    err = str(e).lower()
                    _time.sleep(5 if "rate" in err or "429" in err else 1)
            _time.sleep(1.0)  # 1초 딜레이 (0.5초 단축)

        # ── STEP 3: FVG 탐지 ──
        def find_fvg_retest(df):
            if df is None or len(df) < 10: return None
            df = df.copy()
            df.columns = [c if isinstance(c,str) else c[0] for c in df.columns]
            if "Close" not in df.columns: return None
            c = df["Close"].astype(float).values
            h = df["High"].astype(float).values
            l = df["Low"].astype(float).values
            price = c[-1]
            if price <= 0: return None

            look = min(len(df)-2, lookback)
            best = None

            for i in range(2, look):
                try:
                    # 인덱스: -look+i-2 = 과거, -look+i = 현재 기준 3봉
                    i0 = -(look - i + 2)
                    i1 = -(look - i + 1)
                    i2 = -(look - i)
                    if abs(i2) > len(c): continue

                    h0 = h[i0]; l0 = l[i0]
                    h2 = h[i2]; l2 = l[i2]

                    # 상승 FVG: 봉0 고점 < 봉2 저점
                    if h0 < l2:
                        fvg_lo = h0; fvg_hi = l2
                        fvg_pct = (fvg_hi - fvg_lo) / fvg_lo * 100
                        if fvg_pct < min_fvg_pct: continue
                        if fvg_lo <= price <= fvg_hi:
                            # 나중에 완전히 채워졌는지 체크 (이후 저점이 fvg_lo 아래면 skip)
                            post_l = l[i2+1:] if i2+1 < 0 else l[-1:]
                            if len(post_l) > 1 and float(min(post_l[:-1])) < fvg_lo * 0.998:
                                continue
                            fill = (price - fvg_lo) / (fvg_hi - fvg_lo) * 100
                            cand = {"fvg_type":"bull","fvg_low":fvg_lo,"fvg_high":fvg_hi,
                                    "fvg_size_pct":round(fvg_pct,2),"fill_pct":round(fill,1),"age":i}
                            if best is None or fill < best["fill_pct"]: best = cand

                    # 하락 FVG: 봉0 저점 > 봉2 고점
                    elif l0 > h2:
                        fvg_lo = h2; fvg_hi = l0
                        fvg_pct = (fvg_hi - fvg_lo) / fvg_lo * 100
                        if fvg_pct < min_fvg_pct: continue
                        if fvg_lo <= price <= fvg_hi:
                            post_h = h[i2+1:] if i2+1 < 0 else h[-1:]
                            if len(post_h) > 1 and float(max(post_h[:-1])) > fvg_hi * 1.002:
                                continue
                            fill = (fvg_hi - price) / (fvg_hi - fvg_lo) * 100
                            cand = {"fvg_type":"bear","fvg_low":fvg_lo,"fvg_high":fvg_hi,
                                    "fvg_size_pct":round(fvg_pct,2),"fill_pct":round(fill,1),"age":i}
                            if best is None or fill < best["fill_pct"]: best = cand
                except: continue
            return best

        results = []
        ticker_map = dict(zip(all_stocks["yf_ticker"],
                              zip(all_stocks["Code"], all_stocks["Name"], all_stocks["market"])))

        for yf_tk, (code, name, mkt_label) in ticker_map.items():
            df = all_data.get(yf_tk)
            if df is None or len(df) < 10: continue
            try:
                price  = float(df["Close"].iloc[-1])
                prev_p = float(df["Close"].iloc[-2]) if len(df)>=2 else price
                change = round((price-prev_p)/prev_p*100,2) if prev_p else 0
                fvg = find_fvg_retest(df)
                if fvg is None: continue
                results.append({
                    "code": code, "name": name, "market": mkt_label,
                    "price": int(price), "change": change,
                    "fvg_type":     fvg["fvg_type"],
                    "fvg_low":      int(fvg["fvg_low"]),
                    "fvg_high":     int(fvg["fvg_high"]),
                    "fvg_size_pct": fvg["fvg_size_pct"],
                    "fill_pct":     fvg["fill_pct"],
                    "fvg_age":      fvg["age"],
                })
            except: continue

        results.sort(key=lambda x: (0 if x["fvg_type"]=="bull" else 1, x["fill_pct"]))
        return jsonify({"tf":tf,"market":market,"total":len(results),"results":results[:limit]})

    except Exception as e:
        return jsonify({"error": str(e)})

# ── /breakout_scan ────────────────────────────────────────
@app.route("/breakout_scan", methods=["POST"])
@login_required
def breakout_scan():
    params    = request.json
    market    = params.get("market", "ALL")
    tf        = params.get("tf", "1d")
    lookback  = int(params.get("lookback", 60))
    min_score = int(params.get("min_score", 4))
    limit     = int(params.get("limit", 50))

    TF_MAP = {"1d":("1d","1y"), "4h":("1h","60d"), "1h":("1h","60d")}

    try:
        # ── STEP 1: 거래량 상위 300종목 ──
        mkts = []
        if market in ("ALL","KOSPI"):  mkts.append(("KOSPI","코스피","KS"))
        if market in ("ALL","KOSDAQ"): mkts.append(("KOSDAQ","코스닥","KQ"))

        candidates = []
        for mkt_code, mkt_label, suffix in mkts:
            try:
                df_l = fdr.StockListing(mkt_code)
                df_l = df_l[df_l["Code"].str.match(r"^\d{6}$")]
                vol_col = next((c for c in ["Volume","거래량","Marcap"] if c in df_l.columns), None)
                df_l = df_l.nlargest(300, vol_col) if vol_col else df_l.head(300)
                df_l = df_l[["Code","Name"]].copy()
                df_l["market"]    = mkt_label
                df_l["suffix"]    = suffix
                df_l["yf_ticker"] = df_l["Code"] + "." + suffix
                candidates.append(df_l)
            except: pass

        if not candidates:
            return jsonify({"error": "종목 리스트 수집 실패"})

        all_stocks = pd.concat(candidates, ignore_index=True)
        tickers_yf = all_stocks["yf_ticker"].tolist()

        # ── STEP 2: 배치 다운로드 ──
        yf_iv, yf_period = TF_MAP.get(tf, ("1d","1y"))
        import time as _time
        CHUNK = 50; all_data = {}

        for i in range(0, len(tickers_yf), CHUNK):
            chunk = tickers_yf[i:i+CHUNK]
            for attempt in range(2):
                try:
                    raw = yf.download(chunk, period=yf_period, interval=yf_iv,
                                      group_by="ticker", auto_adjust=True,
                                      progress=False, threads=False, timeout=20)
                    if raw is None or raw.empty: break
                    if isinstance(raw.columns, pd.MultiIndex):
                        for tk in chunk:
                            try:
                                df_tk = raw[tk].dropna(how="all")
                                if df_tk.empty or len(df_tk) < 20: continue
                                if tf == "4h":
                                    df_tk = df_tk.resample("4h").agg({"Open":"first","High":"max","Low":"min","Close":"last","Volume":"sum"}).dropna()
                                all_data[tk] = df_tk
                            except: pass
                    elif len(chunk)==1 and not raw.empty:
                        df_tk = raw.dropna(how="all")
                        if tf=="4h":
                            df_tk=df_tk.resample("4h").agg({"Open":"first","High":"max","Low":"min","Close":"last","Volume":"sum"}).dropna()
                        all_data[chunk[0]]=df_tk
                    break
                except Exception as e:
                    err=str(e).lower()
                    _time.sleep(5 if "rate" in err or "429" in err else 1)
            _time.sleep(1.0)

        # ── STEP 3: 5가지 조건 채점 ──
        def analyze_breakout(df, n=60):
            if df is None or len(df) < 20: return None
            df = df.copy()
            df.columns = [c if isinstance(c,str) else c[0] for c in df.columns]
            if "Close" not in df.columns: return None

            c  = df["Close"].astype(float).values
            h  = df["High"].astype(float).values
            l  = df["Low"].astype(float).values
            v  = df["Volume"].astype(float).values if "Volume" in df.columns else None
            price = c[-1]; prev = c[-2] if len(c)>1 else price
            if price<=0: return None
            look = min(len(df)-2, n)

            score = 0; conditions = []

            # ── 조건1: FVG 2개 이상 누적 (근처 가격대) ──
            fvg_count = 0
            price_zone_lo = price * 0.97
            price_zone_hi = price * 1.03
            for i in range(2, look):
                try:
                    i0 = -(look-i+2); i2 = -(look-i)
                    if abs(i2)>len(c): continue
                    # 상승 FVG in zone
                    if h[i0] < l[i2]:
                        flo=h[i0]; fhi=l[i2]
                        if flo<=price_zone_hi and fhi>=price_zone_lo:
                            fvg_count+=1
                    # 하락 FVG (아래서 지지)
                    elif l[i0] > h[i2]:
                        flo=h[i2]; fhi=l[i0]
                        if flo<=price_zone_hi and fhi>=price_zone_lo:
                            fvg_count+=1
                except: continue
            if fvg_count >= 2: score+=1; conditions.append("fvg_stack")
            elif fvg_count == 1: score+=0.5

            # ── 조건2: RSI 30~55 (과매도→회복 구간) ──
            if len(c) >= 15:
                delta = pd.Series(c).diff()
                gain  = delta.clip(lower=0).rolling(14).mean()
                loss  = (-delta.clip(upper=0)).rolling(14).mean()
                rsi   = float(100 - (100/(1+gain.iloc[-1]/(loss.iloc[-1]+1e-9))))
                if 28 <= rsi <= 58: score+=1; conditions.append("rsi_ok")
            else:
                rsi = 50

            # ── 조건3: 저점 안정화 (최근 10봉 저점 > 이전 10봉 저점) ──
            if len(l) >= 20:
                recent_low = float(min(l[-10:]))
                prev_low   = float(min(l[-20:-10]))
                if recent_low >= prev_low * 0.995:  # 저점이 유지되거나 올라감
                    score+=1; conditions.append("low_stable")

            # ── 조건4: EMA(20) 수렴 (가격이 EMA 2% 이내) ──
            if len(c) >= 20:
                ema20 = float(pd.Series(c).ewm(span=20,adjust=False).mean().iloc[-1])
                ema_gap = (price-ema20)/ema20*100
                if abs(ema_gap) <= 2.5: score+=1; conditions.append("ema_near")
            else:
                ema20=price; ema_gap=0

            # ── 조건5: 거래량 감소 (눌림목 완성) ──
            if v is not None and len(v) >= 10:
                recent_vol = float(v[-5:].mean())
                prev_vol   = float(v[-20:-5].mean()) if len(v)>=20 else float(v[:-5].mean())
                if prev_vol > 0 and recent_vol < prev_vol * 0.85:
                    score+=1; conditions.append("vol_shrink")

            return {
                "score":      int(score),
                "conditions": conditions,
                "rsi":        round(rsi,1),
                "fvg_count":  fvg_count,
                "ema_gap":    round(ema_gap,2) if len(c)>=20 else 0,
                "change":     round((price-prev)/prev*100,2) if prev else 0,
            }

        results = []
        ticker_map = dict(zip(all_stocks["yf_ticker"],
                              zip(all_stocks["Code"],all_stocks["Name"],all_stocks["market"])))

        for yf_tk,(code,name,mkt_label) in ticker_map.items():
            df = all_data.get(yf_tk)
            if df is None or len(df)<20: continue
            try:
                res = analyze_breakout(df, lookback)
                if res is None or res["score"] < min_score: continue
                price = float(df["Close"].iloc[-1])
                results.append({
                    "code":       code,
                    "name":       name,
                    "market":     mkt_label,
                    "price":      int(price),
                    "change":     res["change"],
                    "score":      res["score"],
                    "conditions": res["conditions"],
                    "rsi":        res["rsi"],
                    "fvg_count":  int(res["fvg_count"]),
                    "ema_gap":    res["ema_gap"],
                })
            except: continue

        results.sort(key=lambda x: (-x["score"], x["rsi"]))
        return jsonify({"tf":tf,"market":market,"scanned":len(all_data),
                        "total":len(results),"results":results[:limit]})
    except Exception as e:
        return jsonify({"error": str(e)})

# ══════════════════════════════════════════════════════════
#   이메일 발송 + 가격 이력 추적 + APScheduler
# ══════════════════════════════════════════════════════════

PRICE_HISTORY_FILE = os.path.join(DATA_DIR, "price_history.json")

def load_price_history():
    if os.path.exists(PRICE_HISTORY_FILE):
        try:
            with open(PRICE_HISTORY_FILE,"r",encoding="utf-8") as f:
                return json.load(f)
        except: pass
    return {}

def save_price_history(hist):
    with open(PRICE_HISTORY_FILE,"w",encoding="utf-8") as f:
        json.dump(hist, f, ensure_ascii=False, indent=2)

def record_today_prices(favs):
    """오늘 가격을 이력에 기록 (하루 1번)"""
    today = datetime.now().strftime("%Y-%m-%d")
    hist  = load_price_history()
    for f in favs:
        code = f.get("code",""); market = f.get("market","코스닥")
        price, _ = get_current_price(code, market)
        if not price: continue
        if code not in hist:
            # 최초 등록 — saved_price와 날짜도 같이 기록
            hist[code] = {
                "name":          f.get("name",""),
                "initial_price": f.get("price", price),
                "initial_date":  f.get("savedAt","")[:10] if f.get("savedAt") else today,
                "history":       {}
            }
        hist[code]["history"][today] = round(price, 0)
    save_price_history(hist)

def get_history_price(hist_entry, days_ago):
    """days_ago일 전 가격 반환 (없으면 가장 가까운 날짜)"""
    target = (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d")
    h = hist_entry.get("history", {})
    if not h: return None
    # 정확히 있으면 반환
    if target in h: return h[target]
    # 없으면 target 이전에서 가장 가까운 날짜
    past = sorted(k for k in h if k <= target)
    return h[past[-1]] if past else None

def get_current_price(code, market):
    """yfinance로 현재가 조회"""
    suffix = "KS" if market=="코스피" else "KQ"
    try:
        stk = yf.Ticker(f"{code}.{suffix}")
        hist = stk.history(period="5d", interval="1d")
        if not hist.empty and len(hist)>=1:
            price   = float(hist["Close"].iloc[-1])
            prev    = float(hist["Close"].iloc[-2]) if len(hist)>1 else price
            chg_day = round((price-prev)/prev*100,2) if prev else 0
            return price, chg_day
    except: pass
    return None, None

def send_stock_email(is_test=False):
    cfg  = load_email_cfg()
    if not cfg.get("smtp_user") or not cfg.get("to_email"):
        print("❌ 이메일 설정 없음")
        raise ValueError("이메일 설정 없음 → localhost:5000/settings/email 에서 설정하세요")

    # 테스트가 아닐 때만 enabled/weekday 체크
    if not is_test:
        if not cfg.get("enabled"):
            print("⚠️  이메일 알림 비활성화 상태 — settings/email에서 '알림 활성화' 체크 필요")
            return
        now = datetime.now()
        if now.weekday() >= 5:
            print("⚠️  주말 — 발송 건너뜀")
            return

    now  = datetime.now()
    favs = load_server_favs()
    print(f"📊 server_favorites.json: {len(favs)}종목")

    if not favs and not is_test:
        print("⚠️  즐겨찾기 없음 — settings/email 에서 🔄 서버 동기화 클릭 필요")
        return

    # 오늘 가격 기록
    record_today_prices(favs)
    price_hist = load_price_history()

    slot_labels = {9:"🌅 장 시작", 13:"☀️ 점심 체크", 15:"🌆 장 마감"}
    slot = slot_labels.get(now.hour, f"📊 {now.strftime('%H:%M')} 수동 발송")

    # 종목별 가격 수집
    rows = []
    for f in favs:
        code   = f.get("code","")
        market = f.get("market","코스닥")
        cur_price, chg_day = get_current_price(code, market)
        he = price_hist.get(code, {})

        def pct(base, cp=cur_price):   # cp=cur_price 로 클로저 버그 방지
            if base and cp and base>0:
                return round((cp-base)/base*100, 2)
            return None

        init_price = he.get("initial_price") or f.get("price")
        init_date  = he.get("initial_date","—")
        p1w  = get_history_price(he, 7)
        p1m  = get_history_price(he, 30)
        p3m  = get_history_price(he, 90)

        rows.append({
            **f,
            "cur_price":   cur_price,
            "chg_day":     chg_day,
            "chg_init":    pct(init_price),
            "chg_1w":      pct(p1w),
            "chg_1m":      pct(p1m),
            "chg_3m":      pct(p3m),
            "init_price":  init_price,
            "init_date":   init_date,
        })

    up   = sum(1 for r in rows if (r["chg_day"] or 0) > 0)
    down = sum(1 for r in rows if (r["chg_day"] or 0) < 0)

    def clr(v):
        if v is None: return "#7d8590"
        return "#1d9e75" if v >= 0 else "#e24b4a"
    def fmt_pct(v):
        if v is None: return "—"
        return f"{'+'if v>=0 else ''}{v:.1f}%"
    def arrow(v):
        if v is None: return ""
        return "▲" if v>=0 else "▼"

    scan_lbl = {"surge":"🚀 급등","high":"🏆 전고점","volscan":"📈 거래량",
                "cross":"🔀 교집합","fvgscan":"🧲 FVG","breakout":"🌅 상승준비"}

    rows_html = ""
    for r in rows:
        cp  = r["cur_price"]
        rows_html += f"""
        <tr style="border-bottom:2px solid #ddd;background:#ffffff">
          <td style="padding:16px 20px;font-family:monospace;font-size:16px;font-weight:800;color:#333333">{r.get("code","")}</td>
          <td style="padding:16px 20px;font-weight:800;font-size:19px;color:#111111">{r.get("name","")}<br><span style="font-size:13px;color:#888888;font-weight:500">{r.get("market","")}</span></td>
          <td style="padding:16px 20px;text-align:right;font-family:monospace;font-weight:900;font-size:21px;color:#111111">{f"{int(cp):,}원" if cp else "—"}</td>
          <td style="padding:16px 20px;text-align:right;font-family:monospace;font-weight:900;font-size:20px;color:{"#1a7f3c" if (r["chg_day"] or 0)>=0 else "#cc0000"}">{arrow(r["chg_day"])} {fmt_pct(r["chg_day"])}</td>
          <td style="padding:16px 20px;text-align:right;font-family:monospace;font-size:18px;font-weight:700;color:{"#1a7f3c" if (r["chg_1w"] or 0)>=0 else "#cc0000"}">{fmt_pct(r["chg_1w"])}</td>
          <td style="padding:16px 20px;text-align:right;font-family:monospace;font-size:18px;font-weight:700;color:{"#1a7f3c" if (r["chg_1m"] or 0)>=0 else "#cc0000"}">{fmt_pct(r["chg_1m"])}</td>
          <td style="padding:16px 20px;text-align:right;font-family:monospace;font-size:20px;font-weight:900;color:{"#1a7f3c" if (r["chg_init"] or 0)>=0 else "#cc0000"};background:{"#f0fff4" if (r["chg_init"] or 0)>=0 else "#fff5f5"}">{fmt_pct(r["chg_init"])}</td>
          <td style="padding:16px 20px;text-align:right;font-family:monospace;font-size:16px;font-weight:600;color:#555555">{f"{int(r['init_price']):,}" if r["init_price"] else "—"}원<br><span style="font-size:13px;color:#999999">{r["init_date"]}</span></td>
          <td style="padding:16px 20px;font-size:15px;font-weight:800;color:#0066cc">{scan_lbl.get(r.get("scanType",""), r.get("scanType",""))}</td>
        </tr>"""

    html_body = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:'Apple SD Gothic Neo','Malgun Gothic','Noto Sans KR',Arial,sans-serif;
  background:#f0f2f5;color:#111111;padding:12px;font-size:16px}}
.wrap{{max-width:1200px;margin:0 auto;background:#ffffff;
  border:2px solid #dddddd;border-radius:14px;overflow:hidden;
  box-shadow:0 4px 20px rgba(0,0,0,0.1)}}

/* 헤더 */
.hdr{{background:linear-gradient(135deg,#0d2818 0%,#0d2233 100%);padding:30px 40px}}
.hdr-title{{font-size:40px;font-weight:900;color:#e6edf3;letter-spacing:4px;margin-bottom:8px}}
.hdr-slot{{font-size:22px;font-weight:700;color:#00ff88;margin-bottom:6px}}
.hdr-sub{{font-size:17px;color:#8b949e}}

/* 요약 통계 */
.stats{{display:flex;background:#f8f9fa;border-bottom:2px solid #dee2e6}}
.stat{{flex:1;padding:26px 16px;text-align:center;border-right:2px solid #dee2e6}}
.stat:last-child{{border-right:none}}
.sn{{font-size:44px;font-weight:900;font-family:monospace;line-height:1}}
.sl{{font-size:15px;color:#555555;margin-top:6px;font-weight:700;letter-spacing:1px;text-transform:uppercase}}

/* 테이블 */
table{{width:100%;border-collapse:collapse}}
thead tr{{background:#1a1a2e}}
th{{padding:16px 18px;font-size:15px;font-weight:800;color:#ffffff;
  letter-spacing:1px;text-transform:uppercase;border-bottom:2px solid #333366;
  white-space:nowrap}}
th:nth-child(n+3){{text-align:right}}
th:last-child{{text-align:left}}
td{{padding:16px 16px;font-size:15px;border-bottom:1px solid #21262d;vertical-align:middle}}
td:nth-child(3),td:nth-child(4),td:nth-child(5),td:nth-child(6),td:nth-child(7),td:nth-child(8){{
  text-align:right;font-family:monospace}}
tr:hover td{{background:inherit}}

/* 가격 수치 크기 */
.td-price{{font-size:17px;font-weight:800}}
.td-chg{{font-size:16px;font-weight:800}}
.td-pct{{font-size:15px;font-weight:700}}
.td-save{{font-size:14px;color:#8b949e}}
.td-date{{font-size:12px;color:#484f58}}

/* 배지 */
.badge{{display:inline-block;font-size:12px;font-weight:700;
  padding:4px 10px;border-radius:20px;white-space:nowrap}}

/* 구분선 라벨 */
.section-label{{padding:12px 20px;background:#1a1a2e;
  font-size:14px;color:#aaaacc;letter-spacing:2px;text-transform:uppercase;
  border-bottom:2px solid #333366;font-weight:700}}

/* 푸터 */
.footer{{padding:24px 30px;font-size:16px;color:#666666;
  text-align:center;border-top:2px solid #21262d;background:#0d1117;line-height:2}}
</style></head>
<body>
<div class="wrap">

<div class="hdr">
  <div class="hdr-slot">{slot}</div>
  <div class="hdr-title">KRSCAN</div>
  <div class="hdr-sub">{now.strftime('%Y년 %m월 %d일 (%A) %H:%M')} &nbsp;·&nbsp; 즐겨찾기 {len(rows)}종목</div>
</div>

<div class="stats">
  <div class="stat">
    <div class="sn" style="color:#111111">{len(rows)}</div>
    <div class="sl">모니터링</div>
  </div>
  <div class="stat">
    <div class="sn" style="color:#1d9e75">{up}</div>
    <div class="sl">상승 ▲</div>
  </div>
  <div class="stat">
    <div class="sn" style="color:#e24b4a">{down}</div>
    <div class="sl">하락 ▼</div>
  </div>
  <div class="stat">
    <div class="sn" style="color:#ffa502">{len(rows)-up-down}</div>
    <div class="sl">보합 —</div>
  </div>
</div>

<div class="section-label">종목별 상세 현황</div>

<table>
<thead><tr>
  <th style="text-align:left">코드</th>
  <th style="text-align:left">종목명</th>
  <th>현재가</th>
  <th>오늘 등락</th>
  <th>1주 수익</th>
  <th>1개월</th>
  <th>저장 후 총수익</th>
  <th>저장가 / 날짜</th>
  <th style="text-align:left">출처</th>
</tr></thead>
<tbody>{rows_html}</tbody>
</table>

<div class="footer">
  📊 KRSCAN 즐겨찾기 자동 알림<br>
  평일 09:05 장시작 · 13:00 점심 · 15:35 장마감<br>
  {'⚠️ 테스트 이메일' if is_test else '설정 변경: localhost:5000/settings/email'}
</div>

</div>
</body></html>"""

    msg_obj = MIMEMultipart("alternative")
    msg_obj["Subject"] = f"[KRSCAN] {slot} — {len(rows)}종목 | ▲{up} ▼{down} · {now.strftime('%m/%d %H:%M')}"
    msg_obj["From"]    = cfg["smtp_user"]
    msg_obj["To"]      = cfg["to_email"]
    msg_obj.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP(cfg.get("smtp_host","smtp.gmail.com"), int(cfg.get("smtp_port",587))) as s:
        s.ehlo(); s.starttls(); s.ehlo()
        s.login(cfg["smtp_user"], cfg["smtp_pass"])
        s.sendmail(cfg["smtp_user"], cfg["to_email"], msg_obj.as_string())

def start_scheduler():
    if not HAS_SCHEDULER:
        print("⚠️  APScheduler 미설치 → pip install apscheduler")
        return
    scheduler = BackgroundScheduler(timezone="Asia/Seoul")

    # 클로저 버그 방지: 함수로 감싸기
    def make_email_job():
        send_stock_email(is_test=False)

    def make_alert_job():
        check_and_send_alerts()

    for h, m in [(9,5),(13,0),(15,35)]:
        scheduler.add_job(
            make_email_job,
            CronTrigger(day_of_week="mon-fri", hour=h, minute=m),
            id=f"notify_{h}{m:02d}", replace_existing=True,
            misfire_grace_time=300
        )
        m2 = m+2 if m<=57 else 59
        scheduler.add_job(
            make_alert_job,
            CronTrigger(day_of_week="mon-fri", hour=h, minute=m2),
            id=f"alerts_{h}{m:02d}", replace_existing=True,
            misfire_grace_time=300
        )
    scheduler.start()
    print("✅ 이메일 스케줄러 시작 — 09:05 / 13:00 / 15:35 (평일)")
    print("✅ 조건 알림 체크 — 09:07 / 13:02 / 15:37 (평일)")

# ── /highprob_scan ───────────────────────────────────────
@app.route("/highprob_scan", methods=["POST"])
@login_required
def highprob_scan():
    params = request.json
    combo  = params.get("combo","a")
    market = params.get("market","ALL")
    limit  = int(params.get("limit",50))

    try:
        # 거래량 상위 300 종목 수집
        mkts = []
        if market in ("ALL","KOSPI"):  mkts.append(("KOSPI","코스피","KS"))
        if market in ("ALL","KOSDAQ"): mkts.append(("KOSDAQ","코스닥","KQ"))
        candidates = []
        for mkt_code, mkt_label, suffix in mkts:
            try:
                df_l = fdr.StockListing(mkt_code)
                df_l = df_l[df_l["Code"].str.match(r"^\d{6}$")]
                vc = next((c for c in ["Volume","거래량","Marcap"] if c in df_l.columns), None)
                df_l = df_l.nlargest(300, vc) if vc else df_l.head(300)
                df_l = df_l[["Code","Name"]].copy()
                df_l["market"]    = mkt_label
                df_l["suffix"]    = suffix
                df_l["yf_ticker"] = df_l["Code"] + "." + suffix
                candidates.append(df_l)
            except: pass
        if not candidates:
            return jsonify({"error":"종목 리스트 수집 실패"})
        all_stocks = pd.concat(candidates, ignore_index=True)

        # 배치 다운로드
        import time as _time
        CHUNK=50; all_data={}
        tickers_yf = all_stocks["yf_ticker"].tolist()
        for i in range(0, len(tickers_yf), CHUNK):
            chunk = tickers_yf[i:i+CHUNK]
            for attempt in range(2):
                try:
                    raw = yf.download(chunk, period="60d", interval="1d",
                                      group_by="ticker", auto_adjust=True,
                                      progress=False, threads=False, timeout=20)
                    if raw is None or raw.empty: break
                    if isinstance(raw.columns, pd.MultiIndex):
                        for tk in chunk:
                            try:
                                df_tk=raw[tk].dropna(how="all")
                                if not df_tk.empty and len(df_tk)>=20: all_data[tk]=df_tk
                            except: pass
                    elif len(chunk)==1 and not raw.empty:
                        all_data[chunk[0]]=raw.dropna(how="all")
                    break
                except Exception as e:
                    err=str(e).lower()
                    _time.sleep(5 if "rate" in err or "429" in err else 1)
            _time.sleep(1.0)

        results = []
        ticker_map = dict(zip(all_stocks["yf_ticker"],
                              zip(all_stocks["Code"],all_stocks["Name"],all_stocks["market"])))

        for yf_tk,(code,name,mkt_label) in ticker_map.items():
            df = all_data.get(yf_tk)
            if df is None or len(df)<20: continue
            try:
                df = df.copy()
                df.columns=[c if isinstance(c,str) else c[0] for c in df.columns]
                if "Close" not in df.columns: continue
                c=df["Close"].astype(float).values
                v=df["Volume"].astype(float).values if "Volume" in df.columns else None
                h=df["High"].astype(float).values if "High" in df.columns else c
                l=df["Low"].astype(float).values  if "Low"  in df.columns else c
                price=c[-1]; prev=c[-2] if len(c)>1 else price
                if price<=0: continue
                change=round((price-prev)/prev*100,2) if prev else 0

                # 공통 지표
                ema20=float(pd.Series(c).ewm(span=20,adjust=False).mean().iloc[-1])
                ema_gap=round((price-ema20)/ema20*100,2)
                delta=pd.Series(c).diff()
                gain=delta.clip(lower=0).rolling(14).mean()
                loss=(-delta.clip(upper=0)).rolling(14).mean()
                rsi=float(100-(100/(1+gain.iloc[-1]/(loss.iloc[-1]+1e-9))))
                recent_low=float(min(l[-10:])) if len(l)>=10 else price
                prev_low  =float(min(l[-20:-10])) if len(l)>=20 else price
                low_stable= recent_low >= prev_low*0.995
                recent_vol=float(v[-5:].mean()) if v is not None and len(v)>=5 else 0
                prev_vol  =float(v[-20:-5].mean()) if v is not None and len(v)>=20 else recent_vol
                vol_shrink= prev_vol>0 and recent_vol < prev_vol*0.85
                vol_surge = prev_vol>0 and recent_vol > prev_vol*1.5

                # FVG 재진입 여부
                fvg_retest=False
                for i in range(2,min(len(c)-2,40)):
                    i0=-(i+2); i2=-i
                    if abs(i2)>len(c): continue
                    try:
                        if h[i0]<l[i2] and h[i0]<=price<=l[i2]: fvg_retest=True; break
                        if l[i0]>h[i2] and h[i2]<=price<=l[i0]: fvg_retest=True; break
                    except: continue

                # 급등 여부 (20일 대비 5%+)
                p20=float(c[-20]) if len(c)>=20 else price
                surge = (price-p20)/p20*100 >= 5

                # 콤보별 점수 계산
                score=0; conditions=[]

                if combo=="a":
                    # 급등 + FVG재진입 + 매수거래량
                    if surge:       score+=1; conditions.append("surge")
                    if fvg_retest:  score+=1; conditions.append("fvg_retest")
                    if vol_surge:   score+=1; conditions.append("vol_surge")
                    if score<2: continue

                elif combo=="b":
                    # 상승준비(RSI+저점안정+EMA수렴+거래량감소) + FVG근접
                    if 28<=rsi<=58: score+=1; conditions.append("rsi_ok")
                    if low_stable:  score+=1; conditions.append("low_stable")
                    if abs(ema_gap)<=2.5: score+=1; conditions.append("ema_near")
                    if vol_shrink:  score+=1; conditions.append("vol_shrink")
                    if fvg_retest:  score+=1; conditions.append("fvg_retest")
                    if score<3: continue

                elif combo=="c":
                    # 22이평 이격 2.5%+ + 거래량 감소 + 저점 안정
                    if abs(ema_gap)>=2.5: score+=1; conditions.append("retracement")
                    if vol_shrink:        score+=1; conditions.append("vol_shrink")
                    if low_stable:        score+=1; conditions.append("low_stable")
                    if 28<=rsi<=55:       score+=1; conditions.append("rsi_ok")
                    if score<2: continue

                results.append({
                    "code":code,"name":name,"market":mkt_label,
                    "price":int(price),"change":change,
                    "score":score,"conditions":conditions,
                    "rsi":round(rsi,1),"ema_gap":ema_gap,
                })
            except: continue

        results.sort(key=lambda x:(-x["score"],x["rsi"] if combo in ("b","c") else -abs(x.get("ema_gap",0))))
        return jsonify({"combo":combo,"market":market,"scanned":len(all_data),
                        "total":len(results),"results":results[:limit]})
    except Exception as e:
        return jsonify({"error":str(e)})


# ── 조건 저장 알림 시스템 ─────────────────────────────────
ALERTS_FILE = os.path.join(DATA_DIR, "alerts.json")

def load_alerts():
    if os.path.exists(ALERTS_FILE):
        try:
            with open(ALERTS_FILE,"r",encoding="utf-8") as f: return json.load(f)
        except: pass
    return []

def save_alerts(alerts):
    with open(ALERTS_FILE,"w",encoding="utf-8") as f:
        json.dump(alerts, f, ensure_ascii=False, indent=2)

ALERTS_HTML = ""  # 별도 페이지 없음 — settings/email 에 통합됨

@app.route("/alerts")
@login_required
def alerts_page():
    from flask import redirect, url_for
    return redirect("/settings/email")

@app.route("/alerts/add", methods=["POST"])
@login_required
def alerts_add():
    import uuid
    data   = request.json
    code   = data.get("code","").strip().zfill(6)
    market_raw = data.get("market","KS")
    market = "코스피" if market_raw in ("KS","코스피") else "코스닥"
    try:
        suffix = "KS" if market=="코스피" else "KQ"
        stk  = yf.Ticker(f"{code}.{suffix}")
        info = stk.info
        name = info.get("longName") or info.get("shortName") or code
    except:
        name = code
    alert = {
        "id":      str(uuid.uuid4())[:8],
        "code":    code, "name": name,
        "market":  market,
        "active":  True,
        "created": datetime.now().strftime("%Y-%m-%d"),
        "auto_conditions": [
            {"type":"rsi_below",      "label":"RSI 과매도",  "value":32,  "icon":"📉"},
            {"type":"ma_gap_below",   "label":"이평 이격",   "value":3,   "icon":"📐"},
            {"type":"vol_surge",      "label":"거래량 급증", "value":2.0, "icon":"📈"},
            {"type":"surge_pct",      "label":"급등 포착",   "value":15,  "icon":"🚀"},
            {"type":"breakout_score", "label":"상승준비",    "value":4,   "icon":"🌅"},
        ]
    }
    alerts = load_alerts()
    if not any(a.get("code")==code for a in alerts):
        alerts.insert(0, alert)
        save_alerts(alerts)
    return jsonify({"ok": True})

@app.route("/alerts/del", methods=["POST"])
@login_required
def alerts_del():
    aid    = request.json.get("id","")
    alerts = [a for a in load_alerts() if a.get("id") != aid]
    save_alerts(alerts)
    return jsonify({"ok": True})

@app.route("/alerts/check_now", methods=["POST"])
@login_required
def alerts_check_now():
    try:
        check_and_send_alerts()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

def check_and_send_alerts():
    """자동 조건 체크 — 스케줄러에서 호출 (09:07/13:02/15:37)"""
    cfg    = load_email_cfg()
    alerts = load_alerts()
    if not cfg.get("smtp_user") or not cfg.get("to_email"): return
    if not alerts: return

    triggered = []   # {code, name, market, price, chg, met_conditions:[]}

    for a in alerts:
        if not a.get("active"): continue
        code   = a.get("code","")
        market = a.get("market","코스닥")
        price, chg = get_current_price(code, market)
        if not price: continue

        suffix = "KS" if market=="코스피" else "KQ"
        try:
            stk  = yf.Ticker(f"{code}.{suffix}")
            hist = stk.history(period="60d", interval="1d")
            if hist.empty: continue
            c = hist["Close"].astype(float)
            v = hist["Volume"].astype(float) if "Volume" in hist.columns else None

            # 지표 계산
            rsi = 50
            if len(c) >= 15:
                delta = c.diff()
                gain  = delta.clip(lower=0).rolling(14).mean()
                loss  = (-delta.clip(upper=0)).rolling(14).mean()
                rsi   = float(100-(100/(1+gain.iloc[-1]/(loss.iloc[-1]+1e-9))))

            ema22 = float(c.ewm(span=22,adjust=False).mean().iloc[-1])
            ema_gap = (price - ema22) / ema22 * 100

            vol_ratio = 1.0
            if v is not None and len(v) >= 20:
                rv = float(v.iloc[-3:].mean())
                pv = float(v.iloc[-20:-3].mean())
                vol_ratio = rv/pv if pv > 0 else 1.0

            surge_5d = 0.0
            if len(c) >= 5:
                p5 = float(c.iloc[-5])
                surge_5d = (price-p5)/p5*100 if p5 else 0

            # 상승준비 점수
            bo_score = 0
            if 28 <= rsi <= 58: bo_score += 1
            if abs(ema_gap) <= 2.5: bo_score += 1
            if len(c)>=20 and float(min(c.iloc[-10:]))>=float(min(c.iloc[-20:-10]))*0.995: bo_score+=1
            if v is not None and len(v)>=10 and float(v.iloc[-5:].mean())<float(v.iloc[-15:-5].mean())*0.85: bo_score+=1

            # 각 자동 조건 체크
            met_conds = []
            auto_conds = a.get("auto_conditions", [
                {"type":"rsi_below",      "label":"RSI 과매도",  "value":32,  "icon":"📉"},
                {"type":"ma_gap_below",   "label":"이평 이격",   "value":3,   "icon":"📐"},
                {"type":"vol_surge",      "label":"거래량 급증", "value":2.0, "icon":"📈"},
                {"type":"surge_pct",      "label":"급등 포착",   "value":15,  "icon":"🚀"},
                {"type":"breakout_score", "label":"상승준비",    "value":4,   "icon":"🌅"},
            ])
            for cond in auto_conds:
                ct  = cond.get("type","")
                val = float(cond.get("value",0))
                if   ct=="rsi_below"      and rsi < val:
                    met_conds.append({**cond, "actual":f"RSI {rsi:.1f}"})
                elif ct=="ma_gap_below"   and ema_gap < -val:
                    met_conds.append({**cond, "actual":f"이격 {ema_gap:.1f}%"})
                elif ct=="vol_surge"      and vol_ratio >= val:
                    met_conds.append({**cond, "actual":f"{vol_ratio:.1f}배"})
                elif ct=="surge_pct"      and surge_5d >= val:
                    met_conds.append({**cond, "actual":f"+{surge_5d:.1f}%"})
                elif ct=="breakout_score" and bo_score >= val:
                    met_conds.append({**cond, "actual":f"{bo_score}점/5점"})

            if met_conds:
                triggered.append({
                    "code":code,"name":a.get("name",""),"market":market,
                    "price":int(price),"chg":chg,"met":met_conds
                })
        except: continue

    if not triggered: return

    # 이메일 생성
    now = datetime.now()
    rows = ""
    for t in triggered:
        chg_color = "#1a7f3c" if (t["chg"] or 0)>=0 else "#cc0000"
        chg_str   = f"{'+' if (t['chg'] or 0)>=0 else ''}{t['chg']}%"
        cond_badges = "".join(
            f'<span style="display:inline-block;margin:2px;padding:3px 10px;border-radius:12px;'
            f'background:#eef;color:#0044cc;font-size:13px;font-weight:700">'
            f'{c["icon"]} {c["label"]} → {c["actual"]}</span>'
            for c in t["met"]
        )
        rows += f"""<tr style="border-bottom:2px solid #eee">
          <td style="padding:16px;font-family:monospace;font-size:15px;font-weight:800;color:#333">{t["code"]}</td>
          <td style="padding:16px;font-weight:800;font-size:18px;color:#111">{t["name"]}<br>
            <span style="font-size:13px;color:#888;font-weight:500">{t["market"]}</span></td>
          <td style="padding:16px;text-align:right;font-family:monospace;font-weight:900;font-size:20px">{t["price"]:,}원</td>
          <td style="padding:16px;text-align:right;font-family:monospace;font-weight:900;font-size:18px;color:{chg_color}">{chg_str}</td>
          <td style="padding:16px">{cond_badges}</td>
        </tr>"""

    html = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<style>
body{{font-family:'Apple SD Gothic Neo','Malgun Gothic',Arial,sans-serif;background:#f0f2f5;padding:10px;margin:0}}
.wrap{{max-width:1000px;margin:0 auto;background:#fff;border-radius:14px;border:2px solid #ddd;overflow:hidden;box-shadow:0 4px 20px rgba(0,0,0,.1)}}
.hdr{{background:linear-gradient(135deg,#1a0a3e,#2d1a6e);padding:26px 32px}}
.hdr h1{{font-size:32px;font-weight:900;color:#fff;margin:0 0 6px;letter-spacing:2px}}
.hdr p{{font-size:16px;color:#aab;margin:0}}
.stats{{display:flex;background:#f8f9fa;border-bottom:2px solid #eee}}
.stat{{flex:1;padding:18px;text-align:center;border-right:1px solid #eee}}
.stat:last-child{{border-right:none}}
.sn{{font-size:32px;font-weight:900;font-family:monospace}}
.sl{{font-size:13px;color:#888;margin-top:3px;font-weight:600;text-transform:uppercase;letter-spacing:1px}}
table{{width:100%;border-collapse:collapse}}
th{{padding:14px 16px;background:#1a0a3e;color:#fff;font-size:15px;font-weight:700;text-align:left}}
th:nth-child(3),th:nth-child(4){{text-align:right}}
tr:nth-child(even){{background:#fafbff}}
.footer{{padding:18px 24px;background:#f8f9fa;font-size:14px;color:#888;text-align:center;border-top:2px solid #eee;line-height:1.8}}
</style></head>
<body><div class="wrap">
<div class="hdr">
  <h1>🔔 KRSCAN 조건 알림</h1>
  <p>{now.strftime("%Y년 %m월 %d일 %H:%M")} &nbsp;·&nbsp; {len(triggered)}종목에서 신호 감지</p>
</div>
<div class="stats">
  <div class="stat"><div class="sn" style="color:#1a0a3e">{len(triggered)}</div><div class="sl">감지 종목</div></div>
  <div class="stat"><div class="sn" style="color:#1a7f3c">{sum(1 for t in triggered if (t["chg"] or 0)>0)}</div><div class="sl">상승 중</div></div>
  <div class="stat"><div class="sn" style="color:#cc0000">{sum(1 for t in triggered if (t["chg"] or 0)<0)}</div><div class="sl">하락 중</div></div>
</div>
<table>
<thead><tr>
  <th>코드</th><th>종목명</th>
  <th style="text-align:right">현재가</th>
  <th style="text-align:right">등락률</th>
  <th>감지된 신호</th>
</tr></thead>
<tbody>{rows}</tbody>
</table>
<div class="footer">
  KRSCAN 조건 알림 자동 발송 &nbsp;·&nbsp; 평일 09:07 / 13:02 / 15:37<br>
  설정 변경: localhost:5000/settings/email
</div>
</div></body></html>"""

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"[KRSCAN 🔔] {len(triggered)}종목 신호 감지 · {now.strftime('%m/%d %H:%M')}"
        msg["From"] = cfg["smtp_user"]; msg["To"] = cfg["to_email"]
        msg.attach(MIMEText(html,"html","utf-8"))
        with smtplib.SMTP(cfg.get("smtp_host","smtp.gmail.com"),int(cfg.get("smtp_port",587))) as s:
            s.ehlo(); s.starttls(); s.ehlo()
            s.login(cfg["smtp_user"],cfg["smtp_pass"])
            s.sendmail(cfg["smtp_user"],cfg["to_email"],msg.as_string())
        print(f"✅ 조건 알림 발송: {len(triggered)}종목")
    except Exception as e:
        print(f"❌ 조건 알림 발송 실패: {e}")

if __name__=="__main__":
    start_scheduler()
    print("\n"+"="*55)
    print("  🚀 KRSCAN v4 시작!")
    print("  브라우저: http://localhost:5000")
    print("  주식: 코스피/코스닥 | 코인: BTC/ETH/XRP")
    print("="*55+"\n")
    app.run(debug=False,port=5000,threaded=True)
