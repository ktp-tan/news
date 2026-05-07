"""
Market News Bot — LINE Messaging API
- ส่งได้ 200 ข้อความ/เดือน จึงรวมข่าวหลายชิ้นเป็น 1 ข้อความเสมอ
- threshold สูงขึ้น (85) เพื่อกรองเฉพาะข่าวสำคัญจริงๆ
"""

import os
import json
import time
import requests
import yfinance as yf
from datetime import datetime, timezone
from pathlib import Path
from deep_translator import GoogleTranslator

# ── Config ────────────────────────────────────────────────────────────────────
LINE_CHANNEL_TOKEN = os.environ["LINE_CHANNEL_TOKEN"]
LINE_USER_ID       = os.environ["LINE_USER_ID"]
FINNHUB_TOKEN      = os.environ["FINNHUB_TOKEN"]

STATE_FILE       = Path("state.json")
SCORE_THRESHOLD  = 85     # สูงกว่าเดิม — เฉพาะข่าวสำคัญมาก (ประหยัด quota)
PRICE_SPIKE_PCT  = 0.5    # % ขยับที่ถือว่าผิดปกติ
PRICE_WINDOW_MIN = 15
MAX_NEWS_PER_MSG = 3      # รวมกี่ข่าวต่อ 1 ข้อความ


# ── Keyword Scoring ───────────────────────────────────────────────────────────
KEYWORD_RULES = [
    ("federal reserve",     90, "Fed"),
    ("fed rate",            90, "Fed"),
    ("fomc",                90, "Fed"),
    ("interest rate hike",  85, "Fed"),
    ("interest rate cut",   85, "Fed"),
    ("powell",              70, "Fed"),
    ("quantitative",        65, "Fed"),
    ("cpi",                 85, "Macro"),
    ("inflation",           75, "Macro"),
    ("recession",           80, "Macro"),
    ("gdp",                 70, "Macro"),
    ("unemployment",        70, "Macro"),
    ("nonfarm payroll",     85, "Macro"),
    ("nfp",                 85, "Macro"),
    ("jobs report",         80, "Macro"),
    ("bank failure",        95, "Crisis"),
    ("bank run",            95, "Crisis"),
    ("bankruptcy",          80, "Crisis"),
    ("default",             80, "Crisis"),
    ("emergency",           75, "Crisis"),
    ("collapse",            80, "Crisis"),
    ("war",                 75, "Geo"),
    ("sanctions",           70, "Geo"),
    ("tariff",              70, "Geo"),
    ("trade war",           80, "Geo"),
    ("opec",                75, "Geo"),
    ("bitcoin etf",         80, "Crypto"),
    ("sec crypto",          80, "Crypto"),
    ("crypto ban",          85, "Crypto"),
    ("exchange hack",       85, "Crypto"),
]

SOURCE_BONUS = {
    "reuters":         30,
    "bloomberg":       30,
    "federal reserve": 40,
    "sec.gov":         40,
    "wsj":             25,
    "ft.com":          25,
    "cnbc":            15,
    "marketwatch":     10,
}


def score_news(headline: str, summary: str, source: str) -> tuple[int, str]:
    text = (headline + " " + summary).lower()
    score, category = 0, "General"
    for keyword, pts, cat in KEYWORD_RULES:
        if keyword in text and pts > score:
            score, category = pts, cat
    src_lower = source.lower()
    for src, bonus in SOURCE_BONUS.items():
        if src in src_lower:
            score = min(100, score + bonus)
            break
    return score, category


# ── Price Spike Detection ─────────────────────────────────────────────────────
WATCH_TICKERS = {
    "^GSPC":   "S&P 500",
    "^IXIC":   "Nasdaq",
    "^DJI":    "Dow Jones",
    "BTC-USD": "Bitcoin",
    "ETH-USD": "Ethereum",
}


def get_price_snapshot() -> dict:
    snapshot = {}
    for ticker, name in WATCH_TICKERS.items():
        try:
            data = yf.download(ticker, period="1d",
                               interval=f"{PRICE_WINDOW_MIN}m",
                               progress=False, auto_adjust=True)
            if len(data) >= 2:
                prev = float(data["Close"].iloc[-2])
                curr = float(data["Close"].iloc[-1])
                pct  = (curr - prev) / prev * 100
                snapshot[ticker] = {"name": name, "price": curr,
                                    "prev": prev, "pct": pct}
        except Exception:
            pass
    return snapshot


def detect_spikes(old_snap: dict, new_snap: dict) -> list[dict]:
    spikes = []
    for ticker, d in new_snap.items():
        if ticker not in old_snap:
            continue
        if abs(d["pct"]) >= PRICE_SPIKE_PCT:
            spikes.append({"ticker": ticker, "name": d["name"],
                           "pct": d["pct"], "price": d["price"]})
    return spikes


# ── Finnhub News ──────────────────────────────────────────────────────────────
def fetch_all_news() -> list[dict]:
    news = []
    for cat in ["general", "crypto"]:
        try:
            url = (f"https://finnhub.io/api/v1/news"
                   f"?category={cat}&token={FINNHUB_TOKEN}")
            r = requests.get(url, timeout=10)
            r.raise_for_status()
            news += r.json()
        except Exception as e:
            print(f"[warn] Finnhub {cat}: {e}")
    seen, unique = set(), []
    for n in news:
        nid = str(n.get("id", n.get("headline", "")))
        if nid not in seen:
            seen.add(nid)
            unique.append(n)
    return unique


# ── State ─────────────────────────────────────────────────────────────────────
def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"seen_ids": [], "last_price": {}, "month_count": 0, "month_key": ""}


def save_state(state: dict):
    state["seen_ids"] = state["seen_ids"][-500:]
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False))


# ── LINE Messaging API ────────────────────────────────────────────────────────
def send_line(text: str):
    """Push ข้อความหา user (นับ 1 ต่อการเรียก)"""
    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Authorization": f"Bearer {LINE_CHANNEL_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "to": LINE_USER_ID,
        "messages": [{"type": "text", "text": text}],
    }
    r = requests.post(url, headers=headers, json=payload, timeout=10)
    r.raise_for_status()


def format_news_block(news: dict, score: int, category: str) -> str:
    """สร้างข้อความข่าวชิ้นเดียว (ไม่มี header รวม)"""
    bar = "🔴" if score >= 85 else "🟠"
    ts = datetime.fromtimestamp(
        news.get("datetime", time.time()), tz=timezone.utc)
    ts_str = ts.strftime("%H:%M UTC")

    lines = [
        f"{bar} [{category}] {score}/100",
        news.get("headline", ""),
        f"{news.get('source','?')} | {ts_str}",
    ]
    
    summary_en = news.get("summary", "")
    summary_th = ""
    if summary_en:
        try:
            summary_th = GoogleTranslator(source='auto', target='th').translate(summary_en)
            if len(summary_th) > 200:
                summary_th = summary_th[:200] + "…"
        except Exception as e:
            print(f"[warn] translate error: {e}")
            summary_th = summary_en[:150] + ("…" if len(summary_en) > 150 else "")
            
    if summary_th:
        lines.append(f"🇹🇭 {summary_th}")

    url = news.get("url", "")
    short_url = ""
    if url:
        try:
            r = requests.get("https://is.gd/create.php", params={"format": "simple", "url": url}, timeout=5)
            if r.status_code == 200:
                short_url = r.text.strip()
            else:
                short_url = url
        except Exception:
            short_url = url

    if short_url:
        lines.append(f"🔗 {short_url}")
        
    return "\n".join(lines)


def build_batch_message(news_blocks: list[str],
                        spikes: list[dict],
                        month_count: int) -> str:
    """รวมหลาย block ข่าวเป็น 1 ข้อความเดียว"""
    now_str = datetime.now(timezone.utc).strftime("%d %b %Y %H:%M UTC")
    parts = [f"📊 Market Alert | {now_str}",
             f"Quota ที่ใช้เดือนนี้: {month_count}/200"]

    if spikes:
        spike_lines = ["", "⚡ ตลาดขยับผิดปกติ:"]
        for sp in spikes:
            d = "▲" if sp["pct"] > 0 else "▼"
            spike_lines.append(
                f"  {sp['name']} {d}{abs(sp['pct']):.2f}% "
                f"({sp['price']:,.0f})"
            )
        parts.append("\n".join(spike_lines))

    parts.append("")   # บรรทัดว่าง
    for i, block in enumerate(news_blocks):
        if i > 0:
            parts.append("─" * 25)
        parts.append(block)

    return "\n".join(parts)


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    state      = load_state()
    seen_ids   = state["seen_ids"]
    last_price = state["last_price"]

    # ติดตาม quota รายเดือน
    month_key = datetime.now(timezone.utc).strftime("%Y-%m")
    if state.get("month_key") != month_key:
        state["month_count"] = 0
        state["month_key"]   = month_key
    month_count = state.get("month_count", 0)

    if month_count >= 195:
        print(f"[warn] Quota ใกล้หมด ({month_count}/200) — หยุดส่ง")
        return

    # 1) ราคา + spikes
    print("[*] Fetching prices…")
    new_price = get_price_snapshot()
    spikes    = detect_spikes(last_price, new_price) if last_price else []
    if spikes:
        print(f"[!] Spikes: {[s['ticker'] for s in spikes]}")

    # 2) ข่าว
    print("[*] Fetching news…")
    all_news    = fetch_all_news()
    cutoff      = time.time() - 24 * 60 * 60
    recent_news = [n for n in all_news if n.get("datetime", 0) >= cutoff]
    print(f"[*] Recent: {len(recent_news)} items")

    news_blocks = []
    for news in recent_news:
        nid = str(news.get("id", news.get("headline", "")))
        if nid in seen_ids:
            continue
        seen_ids.append(nid)

        score, category = score_news(
            news.get("headline", ""),
            news.get("summary", ""),
            news.get("source", ""),
        )
        if score < SCORE_THRESHOLD:
            continue

        news_blocks.append(
            format_news_block(news, score, category)
        )
        print(f"[+] Queued score={score} | {news.get('headline','')[:60]}")

    # 3) ส่ง — รวมทุกอย่างเป็น 1 ข้อความ (ถ้ามีข่าวหรือ spike)
    if news_blocks or spikes:
        # แบ่งข่าวเป็น batch ละ MAX_NEWS_PER_MSG ชิ้น
        for i in range(0, max(len(news_blocks), 1), MAX_NEWS_PER_MSG):
            batch = news_blocks[i:i + MAX_NEWS_PER_MSG]
            sp    = spikes if i == 0 else []   # spike แค่ batch แรก
            if not batch and not sp:
                continue
            msg = build_batch_message(batch, sp, month_count + 1)
            try:
                send_line(msg)
                month_count += 1
                state["month_count"] = month_count
                print(f"[*] Sent batch {i//MAX_NEWS_PER_MSG + 1} "
                      f"(quota: {month_count}/200)")
                time.sleep(1)
            except Exception as e:
                print(f"[err] LINE: {e}")
    else:
        print("[*] ไม่มีข่าวสำคัญในรอบนี้")

    state["seen_ids"]   = seen_ids
    state["last_price"] = new_price
    save_state(state)


if __name__ == "__main__":
    main()
