import argparse
import json
import os
import random
import re
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

# Force UTF-8 output on Windows terminals
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr.encoding and sys.stderr.encoding.lower() not in ("utf-8", "utf8"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import glob
import tempfile

try:
    import yt_dlp
    from tqdm import tqdm
except ImportError as e:
    print(f"[error] Missing dependency: {e}")
    print("Run: pip install yt-dlp tqdm")
    sys.exit(1)


_RATE_LIMITED = object()  # sentinel: отличаем rate limit от «нет субтитров»

_SESSION_LOG = Path("session_log.jsonl")
_TUNED_PARAMS = Path("tuned_params.json")


def _log(event: dict) -> None:
    """Дописывает JSON-событие в session_log.jsonl."""
    with _SESSION_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


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


def _parse_vtt(content: str) -> str:
    """Конвертирует VTT-субтитры в чистый текст, убирая дубли."""
    lines = []
    prev = ""
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        # Пропускаем заголовки и таймстемпы
        if (line.startswith("WEBVTT") or line.startswith("NOTE")
                or line.startswith("Kind:") or line.startswith("Language:")
                or "-->" in line or line.isdigit()):
            continue
        # Убираем HTML-теги (<c>, <00:00:01.280>, <c.color-white> и т.д.)
        clean = re.sub(r"<[^>]+>", "", line).strip()
        # Убираем дублирующиеся строки (YouTube auto-subs дублирует каждую фразу)
        if clean and clean != prev:
            lines.append(clean)
            prev = clean
    return " ".join(lines)


def fetch_transcript(video_id: str, lang: str, allow_auto: bool, cookies: str | None = None):
    """Скачивает субтитры через yt-dlp.
    Возвращает str (текст), None (нет субтитров), или _RATE_LIMITED."""
    # Приоритет языков: запрошенный → ru → en
    # НЕ используем "all" — это вызывает загрузку всех языковых дорожек подряд без пауз
    langs = []
    if lang not in langs:
        langs.append(lang)
    if "ru" not in langs:
        langs.append("ru")
    if "en" not in langs:
        langs.append("en")

    with tempfile.TemporaryDirectory() as tmpdir:
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "writesubtitles": True,
            "writeautomaticsub": allow_auto,
            "subtitleslangs": langs,
            "subtitlesformat": "vtt",
            "skip_download": True,
            "outtmpl": os.path.join(tmpdir, "%(id)s"),
            "retries": 2,
            "socket_timeout": 30,
        }
        if cookies:
            ydl_opts["cookiefile"] = cookies

        url = f"https://www.youtube.com/watch?v={video_id}"
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
        except yt_dlp.utils.DownloadError as e:
            msg = str(e).lower()
            if any(x in msg for x in ("429", "too many request", "sign in", "rate")):
                return _RATE_LIMITED
            return None
        except Exception:
            return None

        # Находим VTT-файлы; предпочитаем ручные (без .auto. в имени)
        all_vtt = glob.glob(os.path.join(tmpdir, "*.vtt"))
        if not all_vtt:
            return None

        # Приоритет: ручные субтитры > авто
        manual = [f for f in all_vtt if ".auto." not in os.path.basename(f)]
        # Из ручных — приоритет по языку
        def lang_priority(f):
            name = os.path.basename(f)
            for i, l in enumerate(langs[:-1]):  # skip "all"
                if f".{l}." in name:
                    return i
            return len(langs)

        chosen_pool = manual if manual else all_vtt
        chosen = sorted(chosen_pool, key=lang_priority)[0]

        content = Path(chosen).read_text(encoding="utf-8", errors="replace")
        text = _parse_vtt(content)
        return text if text else None


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


def _human_delay(base: float) -> float:
    """Случайная пауза без предсказуемых паттернов.

    Никаких фиксированных периодов (каждые N видео). Только вероятности:
    - Базовый диапазон: 60–180% от base
    - 18% шанс — «задумался»: пауза в 1.8–3.5x длиннее
    - 7% шанс — «отвлёкся»: +10–35 сек сверху
    - 2% шанс — «пошёл на кухню»: +40–90 сек
    """
    delay = random.uniform(base * 0.6, base * 1.8)

    r = random.random()
    if r < 0.02:
        delay += random.uniform(40.0, 90.0)   # редкая длинная пауза
    elif r < 0.09:
        delay += random.uniform(10.0, 35.0)   # «отвлёкся»
    elif r < 0.27:
        delay *= random.uniform(1.8, 3.5)     # «задумался»

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

    # Читаем параметры от тюнера (если есть и не переданы явно через CLI)
    if _TUNED_PARAMS.exists():
        try:
            tuned = json.loads(_TUNED_PARAMS.read_text(encoding="utf-8"))
            tp = tuned.get("params", {})
            # Применяем только если пользователь не передал свои значения
            # (argparse не даёт узнать это напрямую, используем значение по умолчанию как маркер)
            if args.delay == 1.0 and "delay" in tp:
                args.delay = tp["delay"]
            if args.batch_size == 25 and "batch_size" in tp:
                args.batch_size = int(tp["batch_size"])
            if args.batch_pause == 600 and "batch_pause" in tp:
                args.batch_pause = float(tp["batch_pause"])
            print(f"[tuner] Параметры от тюнера: delay={args.delay}с | "
                  f"batch_size={args.batch_size} | batch_pause={args.batch_pause}с")
        except Exception:
            pass

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
    batch_pause = args.batch_pause

    api_calls = 0  # считаем только реальные запросы к API (не уже существующие)

    # Логируем старт сессии для тюнера
    session_id = str(uuid.uuid4())[:8]
    _log({
        "type": "session_start",
        "session_id": session_id,
        "ts": time.time(),
        "params": {"delay": args.delay, "batch_size": args.batch_size, "batch_pause": args.batch_pause},
    })

    # Первый размер порции — случайный от половины до полутора --batch-size
    _next_batch_at = random.randint(
        max(3, args.batch_size // 2),
        args.batch_size + args.batch_size // 2,
    )

    with tqdm(total=total, desc="Транскрипты", unit="vid") as pbar:
        for i, video in enumerate(all_videos, start=1):
            global_index = offset + i
            video_id = video["id"]
            title = video["title"]

            # Проверяем файл ДО любых запросов — экономим лимит API
            if not args.overwrite:
                expected_file = output_root / f"{global_index:03d} — {sanitize_filename(title)}.txt"
                if expected_file.exists():
                    tqdm.write(f"[skip] уже существует: {expected_file.name}")
                    skipped_exists += 1
                    pbar.update(1)
                    continue

            # Пауза между порциями — размер порции и длительность паузы каждый раз разные
            if api_calls > 0 and api_calls >= _next_batch_at:
                # Пауза: базовая × случайный коэффициент 0.7–1.6
                pause = batch_pause * random.uniform(0.7, 1.6)
                tqdm.write(f"\n[batch] {api_calls} запросов сделано. Пауза {pause:.0f}с...")
                time.sleep(pause)
                # Следующая порция — снова случайный размер
                _next_batch_at = api_calls + random.randint(
                    max(3, args.batch_size // 2),
                    args.batch_size + args.batch_size // 2,
                )

            time.sleep(_human_delay(args.delay))

            api_calls += 1
            call_ts = time.time()
            result = fetch_transcript(video_id, args.lang, allow_auto, cookies=cookies_file)

            if result is _RATE_LIMITED:
                _log({"type": "api_call", "session_id": session_id, "ts": call_ts,
                      "video_index": global_index, "result": "rate_limited"})
                # Одна попытка восстановиться: случайная пауза 30–70 сек
                retry_wait = random.uniform(30, 70)
                tqdm.write(f"[rate limit] Заблокированы на видео {global_index}. Ждём {retry_wait:.0f}с...")
                time.sleep(retry_wait)
                result = fetch_transcript(video_id, args.lang, allow_auto, cookies=cookies_file)

                if result is _RATE_LIMITED:
                    # Всё равно заблокированы — случайная большая пауза и идём дальше
                    extra_pause = batch_pause * random.uniform(1.2, 2.0)
                    tqdm.write(f"[rate limit] Всё ещё заблокированы. Пауза {extra_pause:.0f}с, пропускаем.")
                    log_skipped(output_root, video_id, title, "rate limited after retries")
                    skipped_no_transcript += 1
                    pbar.update(1)
                    time.sleep(extra_pause)
                    api_calls = 0  # сброс — следующий batch начнётся свежим
                    continue

            if result is None:
                _log({"type": "api_call", "session_id": session_id, "ts": call_ts,
                      "video_index": global_index, "result": "no_transcript"})
                tqdm.write(f"[skip] нет транскрипта: {title}")
                log_skipped(output_root, video_id, title, "no transcript available")
                skipped_no_transcript += 1
                pbar.update(1)
                continue

            _log({"type": "api_call", "session_id": session_id, "ts": call_ts,
                  "video_index": global_index, "result": "ok"})
            written = save_transcript(result, output_root, global_index, title, args.overwrite)
            if written:
                tqdm.write(f"[ok] сохранено: {global_index:03d} — {sanitize_filename(title)}.txt")
                saved += 1
            else:
                skipped_exists += 1
            pbar.update(1)

    # Логируем конец сессии
    _log({
        "type": "session_end",
        "session_id": session_id,
        "ts": time.time(),
        "summary": {"saved": saved, "skipped_no_transcript": skipped_no_transcript,
                    "skipped_exists": skipped_exists, "api_calls": api_calls},
    })

    print(f"\n[done] Сохранено: {saved} | Нет субтитров: {skipped_no_transcript} | Уже было: {skipped_exists}")
    print(f"[done] Файлы в: {output_root.resolve()}")

    # Запускаем тюнер — он проанализирует сессию и обновит параметры
    print("\n[tuner] Анализирую сессию и подстраиваю лимиты...")
    try:
        import tuner as _tuner
        _tuner.run(verbose=True)
    except Exception as e:
        print(f"[tuner] Ошибка: {e}")


if __name__ == "__main__":
    main()
