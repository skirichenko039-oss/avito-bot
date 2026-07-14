"""
Avito Deal Finder — версия с ScraperAPI (обход блокировки датацентров)

Вместо headless-браузера (Playwright) используем ScraperAPI — сервис,
который прогоняет запрос через обычные "домашние" IP-адреса, чтобы
Авито не видел в запросе бота с сервера GitHub.
"""

import json
import os
import re
import statistics
import urllib.request
import urllib.parse
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from bs4 import BeautifulSoup

# ============================== ПУТИ ==============================

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
DATA_PATH = BASE_DIR / "data" / "listings.json"

# ============================== СЕКРЕТЫ ==============================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
SCRAPERAPI_KEY = os.getenv("SCRAPERAPI_KEY", "")


# ============================== КОНФИГ ==============================


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        default = {
            "keywords": ["iphone 13 128gb", "playstation 5"],
            "discount_threshold": 0.25,
            "min_samples_for_median": 5,
            "city_slug": "",
        }
        CONFIG_PATH.write_text(json.dumps(default, indent=2, ensure_ascii=False), encoding="utf-8")
        return default
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def load_data() -> dict:
    DATA_PATH.parent.mkdir(exist_ok=True)
    if not DATA_PATH.exists():
        return {"listings": {}}
    return json.loads(DATA_PATH.read_text(encoding="utf-8"))


def save_data(data: dict) -> None:
    DATA_PATH.parent.mkdir(exist_ok=True)
    DATA_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# ============================== МОДЕЛИ ==============================


@dataclass
class Listing:
    external_id: str
    keyword: str
    title: str
    price: int
    url: str


# ============================== TELEGRAM ==============================


def send_telegram_notification(listing: Listing, median_price: float, discount_percent: int):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[!] Telegram не настроен.")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        message = (
            f"🔥 <b>ВЫГОДНОЕ ПРЕДЛОЖЕНИЕ НАЙДЕНО!</b>\n\n"
            f"<b>{listing.title}</b>\n\n"
            f"💰 <b>Цена:</b> {listing.price:,} ₽\n"
            f"📊 <b>Рыночная цена:</b> ~{int(median_price):,} ₽\n"
            f"📉 <b>Скидка:</b> <i>-{discount_percent}%</i> 🎉\n\n"
            f"🏷️ <b>Поиск:</b> {listing.keyword}\n"
            f"⏰ <b>Время:</b> {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}\n\n"
            f"<a href='{listing.url}'>👉 Перейти на Авито</a>"
        ).replace(",", " ")
        data = urllib.parse.urlencode({
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }).encode()
        response = urllib.request.urlopen(url, data=data, timeout=15)
        result = json.loads(response.read().decode())
        if result.get("ok"):
            print(f"[✓] Telegram: сообщение отправлено! ({listing.title[:40]})")
        else:
            print(f"[!] Ошибка Telegram: {result.get('description')}")
    except Exception as e:
        print(f"[!] Ошибка при отправке в Telegram: {e}")


# ============================== ПАРСИНГ ЧЕРЕЗ SCRAPERAPI ==============================


def build_search_url(keyword: str, city_slug: str) -> str:
    query = keyword.replace(" ", "+")
    if city_slug:
        return f"https://www.avito.ru/{city_slug}?q={query}&s=104"
    return f"https://www.avito.ru/all?q={query}&s=104"


def parse_price(raw: str) -> Optional[int]:
    digits = re.sub(r"[^\d]", "", raw)
    return int(digits) if digits else None


def fetch_via_scraperapi(target_url: str) -> Optional[str]:
    """Получает HTML страницы через ScraperAPI (обход блокировки)."""
    if not SCRAPERAPI_KEY:
        print("[!] SCRAPERAPI_KEY не задан. Проверь секреты в GitHub.")
        return None

    params = {
        "api_key": SCRAPERAPI_KEY,
        "url": target_url,
        "render": "true",
        "country_code": "ru",
    }
    api_url = "https://api.scraperapi.com/?" + urllib.parse.urlencode(params)

    try:
        req = urllib.request.Request(api_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=70) as response:
            return response.read().decode("utf-8", errors="ignore")
    except Exception as e:
        print(f"[!] Ошибка ScraperAPI: {e}")
        return None


def fetch_listings_for_keyword(keyword: str, city_slug: str) -> list[Listing]:
    url = build_search_url(keyword, city_slug)
    html = fetch_via_scraperapi(url)
    listings: list[Listing] = []

    if not html:
        return listings

    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select('[data-marker="item"]')

    for card in cards:
        try:
            link_el = card.select_one('a[data-marker="item-title"]')
            price_el = card.select_one('[data-marker="item-price"]')
            if not link_el or not price_el:
                continue

            title = link_el.get_text(strip=True)
            href = link_el.get("href", "")
            price_raw = price_el.get_text(strip=True)
            price = parse_price(price_raw)

            if not href or price is None:
                continue

            full_url = href if href.startswith("http") else f"https://www.avito.ru{href}"
            id_match = re.search(r"_(\d+)(?:\?|$)", href)
            external_id = id_match.group(1) if id_match else full_url

            listings.append(Listing(external_id, keyword, title, price, full_url))
        except Exception as e:
            print(f"  [!] Ошибка парсинга карточки: {e}")
            continue

    return listings


# ============================== ОСНОВНАЯ ЛОГИКА ==============================


def run_check():
    config = load_config()
    data = load_data()

    keywords = config.get("keywords", [])
    threshold = config.get("discount_threshold", 0.25)
    min_samples = config.get("min_samples_for_median", 5)
    city_slug = config.get("city_slug", "")

    if not keywords:
        print("[!] Список ключевых слов пуст.")
        return

    print(f"[i] Проверяю {len(keywords)} ключевых слов через ScraperAPI...")
    new_deals_count = 0

    for keyword in keywords:
        print(f"\n[i] Ищу: '{keyword}'")
        listings = fetch_listings_for_keyword(keyword, city_slug)
        print(f"    Найдено на странице: {len(listings)}")

        keyword_history = data["listings"].setdefault(keyword, {})
        known_prices = [item["price"] for item in keyword_history.values()]

        median = None
        if len(known_prices) >= min_samples:
            median = statistics.median(known_prices)

        for listing in listings:
            if listing.external_id in keyword_history:
                continue

            is_deal = False
            if median is not None and listing.price <= median * (1 - threshold):
                is_deal = True

            keyword_history[listing.external_id] = {
                "title": listing.title,
                "price": listing.price,
                "url": listing.url,
                "first_seen": datetime.utcnow().isoformat(),
                "is_deal": is_deal,
            }

            if is_deal and median is not None:
                discount_pct = round((1 - listing.price / median) * 100)
                print(f"    🔥 ВЫГОДНАЯ СДЕЛКА: {listing.title[:50]} — {listing.price}₽ (-{discount_pct}%)")
                send_telegram_notification(listing, median, discount_pct)
                new_deals_count += 1
            else:
                status = "ждём данных для медианы" if median is None else "не выгодно"
                print(f"    + новое: {listing.title[:50]} — {listing.price}₽ ({status})")

    save_data(data)
    print(f"\n[i] Готово! Новых выгодных сделок: {new_deals_count}")


if __name__ == "__main__":
    run_check()

