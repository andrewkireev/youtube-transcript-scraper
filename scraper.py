import argparse
import os
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
    from youtube_transcript_api._errors import TranscriptsDisabled, NoTranscriptFound
    from tqdm import tqdm
except ImportError as e:
    print(f"[error] Missing dependency: {e}")
    print("Run: pip install yt-dlp youtube-transcript-api tqdm")
    sys.exit(1)


def get_video_list(url: str) -> list[dict]:
    ydl_opts = {
        "quiet": True,
        "extract_flat": True,
        "skip_download": True,
        "no_warnings": True,
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


def fetch_transcript(video_id: str, lang: str, allow_auto: bool) -> str | None:
    try:
        transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
    except (TranscriptsDisabled, NoTranscriptFound, Exception):
        return None

    preferred = [lang, "en"]
    transcript = None

    # Manual transcripts first
    for code in preferred:
        try:
            transcript = transcript_list.find_manually_created_transcript([code])
            break
        except Exception:
            continue

    # Auto-generated fallback
    if transcript is None and allow_auto:
        for code in preferred:
            try:
                transcript = transcript_list.find_generated_transcript([code])
                break
            except Exception:
                continue

    # Any available
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
        entries = transcript.fetch()
        return " ".join(e["text"].strip() for e in entries if e.get("text"))
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


def log_skipped(folder: Path, video_id: str, title: str, reason: str) -> None:
    log_path = folder / "skipped.txt"
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    header = f"# Пропущенные видео — {timestamp}\n"
    line = f"{video_id} | {title} | {reason}\n"
    if not log_path.exists():
        log_path.write_text(header + line, encoding="utf-8")
    else:
        with log_path.open("a", encoding="utf-8") as f:
            f.write(line)


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
    parser.add_argument("--output", default="output", metavar="DIR", help="Папка вывода (по умолч. ./output)")

    args = parser.parse_args()
    allow_auto = not args.no_auto

    all_videos: list[dict] = []

    print(f"[info] Получаю список видео...")
    for url in args.urls:
        try:
            videos = get_video_list(url)
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

    saved = 0
    skipped_no_transcript = 0
    skipped_exists = 0

    offset = (args.from_video - 1) if args.from_video else 0

    for i, video in enumerate(tqdm(all_videos, desc="Транскрипты", unit="vid"), start=1):
        global_index = offset + i
        video_id = video["id"]
        title = video["title"]

        for attempt in range(3):
            text = fetch_transcript(video_id, args.lang, allow_auto)
            if text is not None:
                break
            if attempt < 2:
                time.sleep(2)

        if text is None:
            tqdm.write(f"[skip] нет транскрипта: {title}")
            log_skipped(output_root, video_id, title, "no transcript available")
            skipped_no_transcript += 1
            continue

        written = save_transcript(text, output_root, global_index, title, args.overwrite)
        if written:
            saved += 1
        else:
            tqdm.write(f"[skip] уже существует: {global_index:03d} — {sanitize_filename(title)}.txt")
            skipped_exists += 1

    print(f"\n[done] Сохранено: {saved} | Пропущено (нет субтитров): {skipped_no_transcript} | Уже было: {skipped_exists}")
    print(f"[done] Файлы в: {output_root.resolve()}")


if __name__ == "__main__":
    main()
