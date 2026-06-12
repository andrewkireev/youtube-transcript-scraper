"""
Ждёт разблокировки IP YouTube, затем запускает парсинг.

Согласно исследованию:
- Бан YouTube на timedtext API длится 24-48 часов
- Безопасные параметры: 4-8 секунд между запросами, пауза 60с после каждых 10 видео
- НЕ использовать "all" в subtitleslangs — это вызывает пачку запросов без пауз
- Запуск с домашнего IP (не облачного сервера) критически важен

Проверяет каждые 30 минут, до 60 попыток (30 часов).
"""
import glob
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime

# Force UTF-8
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import yt_dlp

# Видео с субтитрами для теста (первое видео канала)
TEST_VIDEO_ID = "0zKM6d24X_Y"
COOKIES_FILE = "cookies.txt"
CHECK_INTERVAL_SEC = 172800  # 48 часов — ждём ОДИН раз и не тыкаемся
MAX_ATTEMPTS = 1             # одна попытка после паузы

# Безопасные параметры запуска парсера (на основе исследования):
# - --delay 5: базовая задержка 5с → реальная случайная 2.5–7.5с (±50%)
# - --batch-size 10: пауза после каждых 10 API-запросов
# - --batch-pause 60: 60 секунд между порциями (исследование: 30-60с безопасно)
SCRAPER_ARGS = [
    "--cookies", COOKIES_FILE,
    "--batch-size", "10",
    "--batch-pause", "60",   # 60с между порциями по 10 видео
    "--delay", "5",          # базовая задержка 5с → ~2.5-7.5с случайно
]


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def is_unblocked() -> bool:
    """Пробует скачать один субтитр — если успешно, IP разблокирован."""
    with tempfile.TemporaryDirectory() as tmpdir:
        opts = {
            "quiet": True,
            "no_warnings": True,
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": ["ru", "en"],  # только 2 языка — не "all"
            "subtitlesformat": "vtt",
            "skip_download": True,
            "outtmpl": os.path.join(tmpdir, "%(id)s"),
            "retries": 1,
            "socket_timeout": 20,
        }
        if os.path.exists(COOKIES_FILE):
            opts["cookiefile"] = COOKIES_FILE
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([f"https://www.youtube.com/watch?v={TEST_VIDEO_ID}"])
            files = glob.glob(os.path.join(tmpdir, "*.vtt"))
            return bool(files)
        except yt_dlp.utils.DownloadError as e:
            msg = str(e).lower()
            if "429" in msg or "too many" in msg or "sorry" in msg:
                return False
            # Другая ошибка (видео недоступно и т.д.) — пробуем другое видео
            return False
        except Exception:
            return False


def run_scraper(url: str) -> None:
    cmd = [sys.executable, "scraper.py", url] + SCRAPER_ARGS
    log(f"Запускаю: {' '.join(cmd)}")
    subprocess.run(cmd)


def main() -> None:
    url = "https://www.youtube.com/@grebenukm"

    log("=" * 60)
    log("Ожидание разблокировки IP YouTube")
    log(f"Бан обычно длится 24-48 часов.")
    log(f"Стратегия: НЕ тыкаемся в YouTube во время бана (скользящее окно!).")
    log(f"Ждём ровно 48 часов, потом запускаем парсинг.")
    log(f"Безопасные настройки парсера: {' '.join(SCRAPER_ARGS)}")
    log("=" * 60)

    hours = CHECK_INTERVAL_SEC / 3600
    log(f"Старт ожидания. Запуск парсинга через {hours:.0f} часов.")
    log(f"За это время — никаких запросов к YouTube.")
    time.sleep(CHECK_INTERVAL_SEC)

    log("48 часов прошло. Запускаю парсинг канала.")
    log(f"URL: {url}")
    run_scraper(url)
    log("Парсинг завершён.")


if __name__ == "__main__":
    main()
