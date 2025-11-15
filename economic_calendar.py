import json
import os
import requests
from bs4 import BeautifulSoup
from datetime import datetime
import pytz
import time
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# ==================== 讀取 config ====================
def load_config():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(base_dir, 'config.json')
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"找不到 config.json！請在 {config_path} 建立檔案。")
    with open(config_path, 'r', encoding='utf-8') as f:
        return json.load(f)

config = load_config()
DISCORD_WEBHOOK_URL = config['discord_webhook']

# ==================== 讀取事件關鍵字（中英文對照） ====================
def load_event_keywords():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(base_dir, 'event_keywords.json')
    if not os.path.exists(path):
        raise FileNotFoundError(f"找不到 event_keywords.json！請在 {path} 建立檔案。")
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)
        currencies = set(data.get('currencies', []))
        event_dict = data.get('events', {})
        return currencies, event_dict

CURRENCIES, EVENT_DICT = load_event_keywords()

# ==================== 抓取事件（只抓當天）===================
def get_events():
    print("正在啟動 Microsoft Edge（離線模式）...")

    base_dir = os.path.dirname(os.path.abspath(__file__))
    driver_path = os.path.join(base_dir, 'msedgedriver.exe')
    if not os.path.exists(driver_path):
        raise FileNotFoundError(f"找不到 msedgedriver.exe！請放到 {driver_path}")

    options = Options()
    options.add_argument('--headless=new')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    options.add_argument('--disable-gpu')
    options.add_argument('--window-size=1920,1080')
    options.add_argument('--disable-blink-features=AutomationControlled')
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option('useAutomationExtension', False)
    options.add_argument('--user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36')

    service = Service()
    driver = webdriver.Chrome(service=service, options=options)
    driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => false});")

    url = "https://www.forexfactory.com/"
    print("正在載入 Forex Factory 主頁（只顯示當天）...")
    driver.get(url)
    time.sleep(15)

    driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
    time.sleep(5)

    print("正在展開所有折疊事件...")
    try:
        arrows = driver.find_elements(By.CSS_SELECTOR, "td.calendar__event span[title='Show Detail']")
        print(f"發現 {len(arrows)} 個可展開箭頭")
        for arrow in arrows:
            if arrow.is_displayed():
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", arrow)
                time.sleep(0.5)
                try:
                    arrow.click()
                    time.sleep(1.2)
                except:
                    pass
    except Exception as e:
        print(f"展開箭頭失敗: {e}")

    try:
        WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "span[title*='High Impact Expected']"))
        )
        print("高影響事件已載入！")
    except:
        print("等待高影響事件超時，強制繼續...")

    soup = BeautifulSoup(driver.page_source, 'html.parser')
    driver.quit()
    print("網頁載入完成，開始解析...")

    raw_events = []
    now_taipei = datetime.now(pytz.timezone('Asia/Taipei'))
    today_start = now_taipei.replace(hour=0, minute=0, second=0, microsecond=0)
    today_end = now_taipei.replace(hour=23, minute=59, second=59, microsecond=999999)

    rows = soup.select('tr.calendar__row')
    print(f"找到 {len(rows)} 筆候選列")

    for row in rows:
        is_high_impact = row.select_one('td.calendar__impact span[title*="High Impact Expected"]') is not None

        currency_elem = row.select_one('td.calendar__currency span')
        if not currency_elem:
            continue
        currency = currency_elem.text.strip()
        if currency not in CURRENCIES:
            continue

        title_elem = row.select_one('span.calendar__event-title')
        if not title_elem:
            continue
        english_title = title_elem.text.strip()

        time_elem = row.select_one('td.calendar__time span')
        time_str = time_elem.text.strip() if time_elem else ""
        if not time_str or 'All Day' in time_str or 'Tentative' in time_str:
            continue

        today_date = now_taipei.strftime("%Y-%m-%d")
        try:
            event_time_naive = datetime.strptime(f"{today_date} {time_str}", "%Y-%m-%d %I:%M%p")
            event_time_taipei = pytz.timezone('Asia/Taipei').localize(event_time_naive)
            if not (today_start <= event_time_taipei <= today_end):
                continue
        except:
            continue

        diff = event_time_taipei - now_taipei
        mins_left = int(diff.total_seconds() // 60) if diff.total_seconds() > 0 else 0
        countdown = (
            f"剩 {mins_left // 60}小時{mins_left % 60}分" if mins_left > 60 else
            f"剩 {mins_left}分" if mins_left > 0 else "已發布"
        )

        raw_events.append({
            'english_title': english_title,
            'chinese_title': EVENT_DICT.get(english_title, english_title),
            'currency': currency,
            'time': event_time_taipei.strftime("%m/%d %H:%M"),
            'countdown': countdown,
            'is_high_impact': is_high_impact,
            'forecast': row.select_one('td.calendar__forecast span').text.strip() if row.select_one('td.calendar__forecast span') else '—',
            'previous': row.select_one('td.calendar__previous span').text.strip() if row.select_one('td.calendar__previous span') else '—'
        })

    return raw_events

# ==================== 兩步驟分類 ====================
def classify_events(raw_events):
    results = {
        "不建議交易": [],
        "注意波動": [],
        "一般事件": [],
        "外匯/特殊影響力事件": []
    }

    for e in raw_events:
        title = e['english_title']
        lower = title.lower()

        # Step 2: 是否在事件字典中？（支援中英文）
        in_dictionary = any(
            eng.lower() in lower or (eng in EVENT_DICT and EVENT_DICT[eng].lower() in lower)
            for eng in EVENT_DICT.keys()
        )
        in_dictionary = in_dictionary or any(k in lower for k in ['fomc', 'fed', 'rate', 'nfp', 'nonfarm', 'cpi', 'gdp', 'ism', 'adp', 'pmi', 'ppi', 'retail', 'pce'])

        # 分類邏輯
        if e['is_high_impact'] and in_dictionary:
            if any(k in lower for k in ['fomc', 'fed', 'rate', 'nfp', 'nonfarm', 'cpi', 'gdp']):
                cat = "不建議交易"
                color = 0xE74C3C
            elif any(k in lower for k in ['ism', 'adp', 'markit', 'pmi', 'ppi', 'retail', 'pce']):
                cat = "注意波動"
                color = 0xF1C40F
            else:
                cat = "一般事件"
                color = 0x95A5A6
        elif e['is_high_impact'] and not in_dictionary:
            cat = "一般事件"
            color = 0x95A5A6
        elif not e['is_high_impact'] and in_dictionary:
            cat = "外匯/特殊影響力事件"
            color = 0x9B59B6
        else:
            continue

        results[cat].append({**e, 'category': cat, 'color': color})

    return results

# ==================== 發送 Discord ====================
def send_discord(classified):
    total = sum(len(v) for v in classified.values())
    if total == 0:
        payload = {
            "embeds": [{
                "title": "📰無高影響力事件（未來 24 小時）",
                "description": "這段時間相對平靜，可以安心交易！",
                "color": 0x2ECC71,
                "footer": {"text": f"更新時間: {datetime.now(pytz.timezone('Asia/Taipei')).strftime('%m/%d %H:%M')} • 台灣時間"}
            }]
        }
    else:
        summary_text = ""
        for cat, events in classified.items():
            icon = {"不建議交易": "❌", "注意波動": "⚠️", "一般事件": "📅", "外匯/特殊影響力事件": "🌍"}[cat]
            summary_text += f"{icon}**{len(events)}** {cat}\n"

        embed_summary = {
            "title": "🔔經濟日曆總覽（未來 24 小時）",
            "description": f"**總計 {total} 筆事件**\n\n{summary_text}",
            "color": 0xE74C3C if classified["不建議交易"] else 0xF1C40F if classified["注意波動"] else 0x2ECC71,
            "footer": {"text": f"更新時間: {datetime.now(pytz.timezone('Asia/Taipei')).strftime('%m/%d %H:%M')} • 台灣時間"}
        }

        embeds = [embed_summary]
        for cat, events in classified.items():
            for e in events:
                embeds.append({
                    "title": f"{cat} {e['chinese_title']}",
                    "description": f"**{e['currency']}** {e['english_title']}",
                    "color": e['color'],
                    "fields": [
                        {"name": "🕓時間", "value": f"{e['time']}\n{e['countdown']}", "inline": True},
                        {"name": "🪙預測", "value": e['forecast'], "inline": True},
                        {"name": "📊前值", "value": e['previous'], "inline": True}
                    ],
                    "footer": {"text": "台灣時間 (UTC+8)"}
                })

        payload = {"embeds": embeds[:10]}

    try:
        response = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
        print("✅Discord 通知發送成功！" if response.status_code in (200, 204) else f"❌Discord 失敗: {response.status_code}")
    except Exception as e:
        print(f"❌發送錯誤: {e}")

# ==================== 主程式 ====================
if __name__ == "__main__":
    print("🚀開始執行經濟日曆抓取...")
    try:
        raw_events = get_events()
        classified = classify_events(raw_events)
        send_discord(classified)
        print(f"✅完成！共處理 {sum(len(v) for v in classified.values())} 筆事件")
    except Exception as e:
        print(f"❌執行失敗: {e}")
        try:
            requests.post(DISCORD_WEBHOOK_URL, json={"content": f"🛑經濟日曆錯誤：{str(e)}"})
        except:

            pass
