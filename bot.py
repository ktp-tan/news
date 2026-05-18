"""
Market News Bot — LINE Messaging API
- ส่งได้ 200 ข้อความ/เดือน จึงรวมข่าวหลายชิ้นเป็น 1 ข้อความเสมอ
- threshold 90 + financial relevance filter + topic dedup
- กรองเฉพาะข่าวที่กระทบตลาดจริงๆ
"""

import os
import json
import time
import requests
import re
import yfinance as yf
from datetime import datetime, timezone
from pathlib import Path
from deep_translator import GoogleTranslator

# ── Config ────────────────────────────────────────────────────────────────────
LINE_CHANNEL_TOKEN = os.environ["LINE_CHANNEL_TOKEN"]
LINE_USER_ID       = os.environ["LINE_USER_ID"]
FINNHUB_TOKEN      = os.environ["FINNHUB_TOKEN"]

STATE_FILE       = Path("state.json")
SCORE_THRESHOLD  = 90     # เพิ่มจาก 85 → 90 กรองเข้มขึ้น
PRICE_SPIKE_PCT  = 0.5    # % ขยับที่ถือว่าผิดปกติ
PRICE_WINDOW_MIN = 15
MAX_NEWS_PER_MSG = 3      # รวมกี่ข่าวต่อ 1 ข้อความ
MAX_PER_TOPIC    = 2      # จำกัดข่าวซ้ำหัวข้อเดียวกัน ≤ 2 ต่อรอบ


# ── Keyword Scoring ───────────────────────────────────────────────────────────
# คะแนนปรับใหม่: เน้นข่าวที่กระทบตลาดโดยตรง ลดข่าว geopolitics กว้างๆ
KEYWORD_RULES = [
    # Fed — กระทบตลาดโดยตรง คงคะแนนสูง
    ("federal reserve",     90, "Fed"),
    ("fed rate",            90, "Fed"),
    ("fomc",                90, "Fed"),
    ("interest rate hike",  90, "Fed"),
    ("interest rate cut",   90, "Fed"),
    ("rate hike",           85, "Fed"),
    ("rate cut",            85, "Fed"),
    ("powell",              75, "Fed"),
    ("quantitative easing", 70, "Fed"),
    ("quantitative tightening", 70, "Fed"),

    # Macro — ตัวเลขเศรษฐกิจสำคัญ
    ("cpi report",          90, "Macro"),
    ("cpi data",            90, "Macro"),
    ("cpi print",           90, "Macro"),
    ("cpi",                 85, "Macro"),
    ("ppi data",            85, "Macro"),
    ("ppi",                 75, "Macro"),
    ("inflation data",      85, "Macro"),
    ("inflation print",     85, "Macro"),
    ("inflation rate",      80, "Macro"),
    ("inflation",           60, "Macro"),    # คำกว้าง — ต้องมี source ช่วย
    ("recession",           80, "Macro"),
    ("gdp",                 75, "Macro"),
    ("unemployment",        70, "Macro"),
    ("nonfarm payroll",     90, "Macro"),
    ("nfp",                 90, "Macro"),
    ("jobs report",         85, "Macro"),
    ("jobs data",           85, "Macro"),

    # Crisis — วิกฤตที่ต้องรู้ทันที
    ("bank failure",        95, "Crisis"),
    ("bank run",            95, "Crisis"),
    ("market crash",        95, "Crisis"),
    ("financial crisis",    95, "Crisis"),
    ("flash crash",         90, "Crisis"),
    ("bankruptcy",          80, "Crisis"),
    ("default",             70, "Crisis"),
    ("collapse",            60, "Crisis"),    # ลด — คำกว้างเกินไป

    # Geo — สงคราม/ความขัดแย้งที่กระทบตลาด
    ("war",                 75, "Geo"),       # เพิ่มจาก 40 → 75 ข่าวสงครามกระทบตลาดจริง
    ("military strike",     85, "Geo"),       # เพิ่ม — โจมตีทางทหาร
    ("airstrike",           80, "Geo"),       # เพิ่ม — ทิ้งระเบิด
    ("airstrikes",          80, "Geo"),       # เพิ่ม
    ("missile strike",      85, "Geo"),       # เพิ่ม — ยิงขีปนาวุธ
    ("missile attack",      85, "Geo"),       # เพิ่ม
    ("invasion",            85, "Geo"),       # เพิ่ม — บุกรุก
    ("ceasefire",           80, "Geo"),       # เพิ่ม — หยุดยิง (ตลาดขึ้น)
    ("peace deal",          80, "Geo"),       # เพิ่ม — ข้อตกลงสันติภาพ
    ("peace talks",         75, "Geo"),       # เพิ่ม — เจรจาสันติภาพ
    ("escalation",          80, "Geo"),       # เพิ่ม — ความตึงเครียดเพิ่ม
    ("military",            60, "Geo"),       # เพิ่ม — ทหาร (คำกว้าง)
    ("conflict",            65, "Geo"),       # เพิ่ม — ความขัดแย้ง
    ("nuclear",             80, "Geo"),       # เพิ่ม — นิวเคลียร์
    ("nato",                70, "Geo"),       # เพิ่ม — NATO
    ("sanctions",           65, "Geo"),       # เพิ่มจาก 45 → 65
    ("tariff",              60, "Geo"),
    ("trade war",           75, "Geo"),
    ("trade deal",          70, "Geo"),
    ("opec",                80, "Geo"),       # ข่าว OPEC กระทบน้ำมันโดยตรง
    ("opec cut",            85, "Geo"),
    ("oil output",          80, "Geo"),       # ผลผลิตน้ำมัน
    ("oil supply",          80, "Geo"),       # อุปทานน้ำมัน
    ("oil demand",          75, "Geo"),       # อุปสงค์น้ำมัน
    ("oil embargo",         85, "Geo"),
    ("hormuz",              70, "Geo"),

    # Crypto — ข่าวที่กระทบ crypto โดยตรง
    ("bitcoin etf",         80, "Crypto"),
    ("sec crypto",          80, "Crypto"),
    ("crypto ban",          85, "Crypto"),
    ("exchange hack",       85, "Crypto"),
    ("crypto regulation",   75, "Crypto"),

    # Market Moves — ข่าวที่ตลาดขยับแรง
    ("market rally",        80, "Market"),
    ("market selloff",      85, "Market"),
    ("markets retreat",     75, "Market"),    # เพิ่ม — ตลาดถอย
    ("markets fall",        80, "Market"),    # เพิ่ม — ตลาดร่วง
    ("markets drop",        80, "Market"),    # เพิ่ม
    ("markets falter",      75, "Market"),    # เพิ่ม
    ("wall st falls",       85, "Market"),    # เพิ่ม — Wall St ร่วง
    ("wall st drops",       85, "Market"),    # เพิ่ม
    ("sell-off",            75, "Market"),
    ("all-time low",        80, "Market"),    # เพิ่ม — ทำ ATL
    ("all-time high",       75, "Market"),    # เพิ่ม — ทำ ATH
    ("record high",         75, "Market"),    # เพิ่ม
    ("record low",          80, "Market"),    # เพิ่ม
    ("circuit breaker",     95, "Market"),
    ("trading halt",        90, "Market"),
    ("black swan",          90, "Market"),

    # Commodity/Currency Market — ราคาสินค้าโภคภัณฑ์/สกุลเงิน
    ("gold price",          70, "Commodity"),
    ("gold rally",          75, "Commodity"),
    ("gold falls",          75, "Commodity"),
    ("gold extends",        70, "Commodity"),
    ("oil price",           75, "Commodity"),
    ("crude price",         75, "Commodity"),
]

# Source bonus — ลดลงเพื่อไม่ให้ข่าวไม่เกี่ยวข้องผ่าน threshold
SOURCE_BONUS = {
    "reuters":         15,    # ลดจาก 30 → 15
    "bloomberg":       15,    # ลดจาก 30 → 15
    "federal reserve": 40,
    "sec.gov":         40,
    "wsj":             20,
    "ft.com":          20,
    "cnbc":            10,
    "marketwatch":     10,
}


# ── Financial Relevance Filter ────────────────────────────────────────────────
# ข่าวต้องมีคำเกี่ยวกับการเงิน/ตลาดอย่างน้อย 1 คำ ถึงจะถือว่าเกี่ยวข้อง
FINANCIAL_TERMS = {
    # ตลาดและสินทรัพย์
    "stock", "stocks", "market", "markets", "shares", "equity", "equities",
    "bond", "bonds", "yield", "yields", "treasury", "treasuries",
    "index", "indices", "dow", "nasdaq", "s&p", "s&p 500", "nikkei",
    "ftse", "dax", "hang seng",
    # สินค้าโภคภัณฑ์
    "oil", "crude", "brent", "wti", "gold", "silver", "copper",
    "commodity", "commodities", "lng", "natural gas", "energy",
    # สกุลเงิน
    "currency", "currencies", "forex", "dollar", "euro", "yen", "yuan",
    "rupee", "sterling", "pound",
    # คริปโต
    "bitcoin", "btc", "crypto", "ethereum", "eth",
    # เศรษฐกิจ
    "economy", "economic", "gdp", "inflation", "deflation",
    "rate", "rates", "interest rate", "monetary", "fiscal",
    "cpi", "ppi", "fomc", "fed", "ecb", "boj", "pboc",
    "imf", "world bank", "central bank",
    # การเงิน
    "investor", "investors", "fund", "funds", "hedge fund",
    "profit", "revenue", "earnings", "quarterly", "forecast",
    "trade", "trading", "export", "import", "supply chain",
    "rally", "selloff", "sell-off", "bull", "bear", "volatility",
    "futures", "options", "derivatives", "debt", "credit", "loan",
    "bank", "banking", "financial",
    # ตัวชี้วัดตลาด
    "wall street", "wall st", "price", "prices", "demand", "supply",
    "surplus", "deficit", "stimulus", "bailout", "liquidity",
    "hedge", "hedging", "portfolio", "asset", "assets",
    "opec", "iea", "eia",
    # สงคราม/ความขัดแย้งที่กระทบตลาด
    "war", "conflict", "military", "invasion", "airstrike", "missile",
    "ceasefire", "sanctions", "escalation", "nato", "nuclear",
    "geopolitical", "defense", "arms",
}

# ── Negative Keywords ─────────────────────────────────────────────────────────
# หักคะแนนข่าวที่เป็น lifestyle / human interest / ไม่ใช่ข่าวการเงิน
NEGATIVE_KEYWORDS = [
    ("holiday",        -30),
    ("holidays",       -30),
    ("vacation",       -30),
    ("travel",         -25),
    ("tourism",        -25),
    ("tourist",        -25),
    ("passenger",      -20),
    ("passengers",     -20),
    ("oldest",         -40),
    ("doctor",         -30),
    ("lifestyle",      -30),
    ("recipe",         -40),
    ("weather",        -30),
    ("sport",          -30),
    ("celebrity",      -30),
    ("entertainment",  -25),
    ("movie",          -25),
    ("film",           -25),
    ("music",          -20),
    ("fashion",        -30),
    ("restaurant",     -25),
    ("seafarer",       -20),
    ("seafarers",      -20),
    ("ordeal",         -20),
    ("livelihoods",    -20),
    ("summer",         -15),
    ("world cup",      -30),
    ("olympic",        -30),
]


def has_financial_relevance(text: str) -> bool:
    """ตรวจว่าข่าวเกี่ยวกับการเงิน/ตลาดจริงไหม"""
    text_lower = text.lower()
    for term in FINANCIAL_TERMS:
        if term in text_lower:
            return True
    return False


def score_news(headline: str, summary: str, source: str) -> tuple[int, str]:
    """ให้คะแนนข่าว — ยิ่งสูงยิ่งสำคัญต่อตลาด"""
    text = (headline + " " + summary).lower()
    score, category = 0, "General"

    # 1) Keyword scoring — ใช้คะแนนสูงสุดที่ match
    for keyword, pts, cat in KEYWORD_RULES:
        pattern = r'\b' + re.escape(keyword) + r'\b'
        if re.search(pattern, text) and pts > score:
            score, category = pts, cat

    # 2) Source bonus
    src_lower = source.lower()
    for src, bonus in SOURCE_BONUS.items():
        if src in src_lower:
            score = min(100, score + bonus)
            break

    # 3) Negative keywords — หักคะแนนข่าว lifestyle/ไม่เกี่ยว
    for neg_kw, penalty in NEGATIVE_KEYWORDS:
        if neg_kw in text:
            score = max(0, score + penalty)

    # 4) Financial relevance check — ถ้าไม่เกี่ยวการเงินเลย หักหนัก
    if score > 0 and not has_financial_relevance(headline + " " + summary):
        score = max(0, score - 40)

    return score, category


# ── Topic Deduplication ───────────────────────────────────────────────────────
def get_topic_key(headline: str) -> str:
    """สร้าง topic key สำหรับจำกัดข่าวซ้ำหัวข้อเดียวกัน"""
    h = headline.lower()

    if any(w in h for w in ["iran", "hormuz", "tehran", "irgc"]):
        return "iran_conflict"
    if any(w in h for w in ["fed ", "federal reserve", "fomc", "rate hike", "rate cut"]):
        return "fed_policy"
    if any(w in h for w in ["cpi", "inflation data", "inflation print"]):
        return "inflation_data"
    if any(w in h for w in ["bitcoin", "crypto", "ethereum"]):
        return "crypto"
    if any(w in h for w in ["oil price", "crude", "opec", "lng", "brent"]):
        return "energy"
    if any(w in h for w in ["tariff", "trade war", "trade deal"]):
        return "trade"
    if any(w in h for w in ["sanctions"]) and "iran" not in h:
        return "sanctions_other"
    if any(w in h for w in ["china", "xi jinping"]):
        return "china"

    # default: ใช้ headline (ไม่ dedup)
    return f"_unique_{headline[:80]}"


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
    bar = "🔴" if score >= 90 else "🟠"
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

    news_blocks   = []
    topic_counts  = {}   # นับจำนวนข่าวต่อ topic สำหรับ dedup

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
            if score > 0:
                print(f"[skip] score={score} < {SCORE_THRESHOLD} | "
                      f"{news.get('headline','')[:60]}")
            continue

        # Topic dedup — จำกัดข่าวซ้ำหัวข้อเดียวกัน
        topic = get_topic_key(news.get("headline", ""))
        if topic_counts.get(topic, 0) >= MAX_PER_TOPIC:
            print(f"[skip] topic dedup ({topic}) | "
                  f"{news.get('headline','')[:60]}")
            continue
        topic_counts[topic] = topic_counts.get(topic, 0) + 1

        news_blocks.append(
            format_news_block(news, score, category)
        )
        print(f"[+] Queued score={score} topic={topic} | "
              f"{news.get('headline','')[:60]}")

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
