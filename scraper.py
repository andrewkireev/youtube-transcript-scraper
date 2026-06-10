import argparse
import os
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path

# Force UTF-8 output on Windows terminals
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr.encoding and sys.stderr.encoding.lower() not in ("utf-8", "utf8"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

try:
    import yt_dlp
    from youtube_transcript_api import YouTubeTranscriptApi
    from youtube_transcript_api._errors import RequestBlocked, IpBlocked, YouTubeRequestFailed
    from tqdm import tqdm
except ImportError as e:
    print(f"[error] Missing dependency: {e}")
    print("Run: pip install yt-dlp youtube-transcript-api tqdm")
    sys.exit(1)


_RATE_LIMITED = object()  # sentinel: отличаем rate limit от «нет субтитров»
_RATE_LIMIT_ERRORS = (RequestBlocked, IpBlocked, YouTubeRequestFailed)


def _normalize_channel_url(url: str) -> tuple[str, bool]:
    """Append /videos to channel URLs so yt-dlp returns videos, not tab list.
    Returns (normalized_url, is_channel) where is_channel signals to reverse order."""
    # Already pointing at a specific tab or playlist — leave as-is
    if any(x in url for x in ["/shorts", "/live", "/streams", "/playlist?", "watch?v="]):
        return url, False
    # /videos tab — it's a channel, reverse order
    if re.search(r"youtube\.com/(@[^/?]+|channel/[^/?]+|c/[^/?]+|user/[^/?]+)/videos", url):
        return url, True
    # Channel-style URLs: @handle, /channel/UC..., /c/name, /user/name
    if re.search(r"youtube\.com/(@[^/?]+|channel/[^/?]+|c/[^/?]+|user/[^/?]+)$", url):
        return url.rstrip("/") + "/videos", True
    return url, False


def get_video_list(url: str, lang: str = "ru") -> list[dict]:
    url, is_channel = _normalize_channel_url(url)
    ydl_opts = {
        "quiet": True,
        "extract_flat": True,
        "skip_download": True,
        "no_warnings": True,
        "extractor_args": {"youtube": {"lang": [lang]}},
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    if not info:
        return []

    if info.get("_type") == "playlist" or "entries" in info:
        entries = list(info.get("entries") or [])
        channel = info.get("channel") or info.get("uploader") or info.get("title") or "Unknown"
        videos = []
        for entry in entries:
            if not entry:
                continue
            video_id = entry.get("id") or entry.get("url", "").split("v=")[-1]
            title = entry.get("title") or video_id
            ch = entry.get("channel") or entry.get("uploader") or channel
            if video_id:
                videos.append({"id": video_id, "title": title, "channel": ch})
        # Channel videos come newest-first; reverse so 001 = oldest
        if is_channel:
            videos.reverse()
        return videos
    else:
        video_id = info.get("id")
        title = info.get("title") or video_id
        channel = info.get("channel") or info.get("uploader") or "Unknown"
        if video_id:
            return [{"id": video_id, "title": title, "channel": channel}]
        return []


def apply_range(
    videos: list[dict],
    limit: int | None,
    from_video: int | None,
    to_video: int | None,
) -> list[dict]:
    start = (from_video - 1) if from_video else 0
    end = to_video if to_video else len(videos)
    videos = videos[start:end]
    if limit:
        videos = videos[:limit]
    return videos


def _make_api(cookies: str | None) -> YouTubeTranscriptApi:
    if not cookies:
        return YouTubeTranscriptApi()
    import requests
    from http.cookiejar import MozillaCookieJar
    session = requests.Session()
    jar = MozillaCookieJar(cookies)
    jar.load(ignore_discard=True, ignore_expires=True)
    session.cookies = jar
    return YouTubeTranscriptApi(http_client=session)


def fetch_transcript(video_id: str, lang: str, allow_auto: bool, cookies: str | None = None):
    """Returns str (text), None (нет субтитров), или _RATE_LIMITED (заблокированы)."""
    api = _make_api(cookies)
    try:
        transcript_list = api.list(video_id)
    except _RATE_LIMIT_ERRORS:
        return _RATE_LIMITED
    except Exception:
        return None

    preferred = [lang, "en"]
    transcript = None

    try:
        transcript = transcript_list.find_manually_created_transcript(preferred)
    except Exception:
        pass

    if transcript is None and allow_auto:
        try:
            transcript = transcript_list.find_generated_transcript(preferred)
        except Exception:
            pass

    if transcript is None:
        try:
            all_transcripts = list(transcript_list)
            if all_transcripts:
                transcript = all_transcripts[0]
        except Exception:
            return None

    if transcript is None:
        return None

    try:
        fetched = transcript.fetch()
        return " ".join(
            snip.text.strip()
            for snip in fetched
            if getattr(snip, "text", None)
        )
    except _RATE_LIMIT_ERRORS:
        return _RATE_LIMITED
    except Exception:
        return None


def sanitize_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]', "", name)
    name = name.strip(". ")
    return name[:180] if name else "untitled"


def save_transcript(
    text: str,
    folder: Path,
    index: int,
    title: str,
    overwrite: bool,
) -> bool:
    filename = f"{index:03d} — {sanitize_filename(title)}.txt"
    filepath = folder / filename
    if filepath.exists() and not overwrite:
        return False
    filepath.write_text(text, encoding="utf-8")
    return True


def init_skipped_log(folder: Path) -> None:
    """Добавляет разделитель нового запуска в skipped.txt."""
    log_path = folder / "skipped.txt"
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    separator = f"\n# === Запуск {timestamp} ===\n"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(separator)


def log_skipped(folder: Path, video_id: str, title: str, reason: str) -> None:
    log_path = folder / "skipped.txt"
    line = f"{video_id} | {title} | {reason}\n"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line)


def _human_delay(base: float, index: int) -> float:
    """Случайная пауза, имитирующая поведение человека.

    - Базовый разброс ±50% от base
    - Каждые ~10 видео — более длинная пауза (как будто отвлёкся)
    - Редкие длинные паузы (5% шанс) — «читаю страницу»
    """
    delay = random.uniform(base * 0.5, base * 1.5)

    # Каждые ~10 видео — пауза 2–5x длиннее
    if index % random.randint(8, 12) == 0:
        delay *= random.uniform(2.0, 5.0)

    # 5% шанс на совсем длинную паузу (как будто переключился на другое)
    if random.random() < 0.05:
        delay += random.uniform(5.0, 15.0)

    return delay


def _resolve_cookies(args) -> str | None:
    """Возвращает путь к cookies.txt или None."""
    if getattr(args, "cookies", None):
        p = Path(args.cookies)
        if not p.exists():
            print(f"[warn] Файл cookies не найден: {p}")
            return None
        print(f"[info] Используем cookies из файла: {p}")
        return str(p)

    browser = getattr(args, "cookies_from_browser", None)
    if browser:
        cookies_path = Path("cookies.txt")
        print(f"[info] Извлекаем cookies из браузера: {browser}...")
        try:
            import subprocess
            result = subprocess.run(
                ["yt-dlp", "--cookies-from-browser", browser,
                 "--cookies", str(cookies_path),
                 "--flat-playlist", "--skip-download",
                 "https://www.youtube.com/watch?v=dQw4w9WgXcQ"],
                capture_output=True, text=True, timeout=30
            )
            if cookies_path.exists() and cookies_path.stat().st_size > 0:
                print(f"[info] Cookies сохранены в {cookies_path}")
                return str(cookies_path)
            else:
                print(f"[warn] Не удалось извлечь cookies из {browser}")
        except Exception as e:
            print(f"[warn] Ошибка при извлечении cookies: {e}")
    return None


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Скачивает транскрипты YouTube-видео в текстовые файлы.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Примеры:
  python scraper.py https://youtube.com/@channelname
  python scraper.py https://youtube.com/playlist?list=PL... --limit 20
  python scraper.py https://youtu.be/abc https://youtu.be/xyz
  python scraper.py https://youtube.com/@channel --from-video 5 --to-video 25
  python scraper.py https://youtube.com/@channel --limit 20 --lang en
        """,
    )
    parser.add_argument("urls", nargs="+", help="URL канала, плейлиста или видео")
    parser.add_argument("--limit", type=int, metavar="N", help="Взять первые N видео")
    parser.add_argument("--from-video", type=int, metavar="N", help="Начать с N-го видео (1-based)")
    parser.add_argument("--to-video", type=int, metavar="N", help="Закончить на N-м видео")
    parser.add_argument("--lang", default="ru", metavar="LANG", help="Предпочитаемый язык (по умолч. ru)")
    parser.add_argument("--no-auto", action="store_true", help="Не использовать авто-субтитры")
    parser.add_argument("--overwrite", action="store_true", help="Перезаписывать существующие файлы")
    parser.add_argument("--delay", type=float, default=1.0, metavar="SEC", help="Базовая пауза между запросами (по умолч. 1.0 сек, реальная — случайная ±50%%)")
    parser.add_argument("--batch-size", type=int, default=25, metavar="N", help="Видео за одну сессию до паузы (по умолч. 25)")
    parser.add_argument("--batch-pause", type=float, default=600, metavar="SEC", help="Пауза между порциями в секундах (по умолч. 600 = 10 мин)")
    parser.add_argument("--cookies", metavar="FILE", help="Путь к файлу cookies.txt (Netscape формат) для аутентификации")
    parser.add_argument("--cookies-from-browser", metavar="BROWSER", help="Взять cookies из браузера: chrome, firefox, edge")
    parser.add_argument("--output", default="output", metavar="DIR", help="Папка вывода (по умолч. ./output)")

    args = parser.parse_args()
    allow_auto = not args.no_auto

    # Подготовка cookies
    cookies_file = _resolve_cookies(args)

    all_videos: list[dict] = []

    print(f"[info] Получаю список видео...")
    for url in args.urls:
        try:
            videos = get_video_list(url, lang=args.lang)
            if not videos:
                print(f"[warn] Видео не найдены для: {url}")
            else:
                all_videos.extend(videos)
                print(f"[info] Найдено {len(videos)} видео из {url}")
        except Exception as e:
            print(f"[error] Не удалось получить видео из {url}: {e}")

    if not all_videos:
        print("[error] Нет видео для обработки.")
        sys.exit(1)

    all_videos = apply_range(all_videos, args.limit, args.from_video, args.to_video)
    print(f"[info] Всего к обработке: {len(all_videos)} видео")

    # Group by channel for folder naming; use first video's channel as primary
    channel_name = sanitize_filename(all_videos[0]["channel"]) if all_videos else "Unknown"
    output_root = Path(args.output) / channel_name
    output_root.mkdir(parents=True, exist_ok=True)
    init_skipped_log(output_root)

    saved = 0
    skipped_no_transcript = 0
    skipped_exists = 0

    offset = (args.from_video - 1) if args.from_video else 0
    total = len(all_videos)
    batch_size = args.batch_size
    batch_pause = args.batch_pause

    with tqdm(total=total, desc="Транскрипты", unit="vid") as pbar:
        for i, video in enumerate(all_videos, start=1):
            global_index = offset + i
            video_id = video["id"]
            title = video["title"]

            # Пауза между порциями
            if i > 1 and (i - 1) % batch_size == 0:
                batch_num = (i - 1) // batch_size
                total_batches = (total + batch_size - 1) // batch_size
                pause = batch_pause + random.uniform(-30, 30)
                tqdm.write(
                    f"\n[batch] Порция {batch_num}/{total_batches} завершена. "
                    f"Пауза {pause:.0f}с чтобы не словить бан..."
                )
                time.sleep(pause)

            time.sleep(_human_delay(args.delay, i))

            result = None
            for attempt in range(5):
                result = fetch_transcript(video_id, args.lang, allow_auto, cookies=cookies_file)
                if result is _RATE_LIMITED:
                    wait = 60 * (2 ** attempt)
                    tqdm.write(f"[rate limit] Заблокированы. Ждём {wait}с (попытка {attempt+1}/5)...")
                    time.sleep(wait)
                    continue
                break

            if result is _RATE_LIMITED or result is None:
                reason = "rate limited after retries" if result is _RATE_LIMITED else "no transcript available"
                tqdm.write(f"[skip] нет транскрипта: {title}")
                log_skipped(output_root, video_id, title, reason)
                skipped_no_transcript += 1
                pbar.update(1)
                continue

            written = save_transcript(result, output_root, global_index, title, args.overwrite)
            if written:
                saved += 1
            else:
                tqdm.write(f"[skip] уже существует: {global_index:03d} — {sanitize_filename(title)}.txt")
                skipped_exists += 1
            pbar.update(1)

    print(f"\n[done] Сохранено: {saved} | Пропущено (нет субтитров): {skipped_no_transcript} | Уже было: {skipped_exists}")
    print(f"[done] Файлы в: {output_root.resolve()}")


if __name__ == "__main__":
    main()
