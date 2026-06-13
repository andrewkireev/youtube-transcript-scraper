# /scrape — YouTube Transcript Scraper

Управление скрапером транскриптов YouTube.

## Что умеет этот скилл

При вызове `/scrape` без аргументов — показывает статус: сколько файлов скачано, текущие параметры из tuned_params.json, последние строки skipped.txt.

При вызове `/scrape <url>` — запускает скрапер для указанного канала/плейлиста/видео с текущими tuned-параметрами.

При вызове `/scrape status` — детальный отчёт: история сессий из session_log.jsonl, анализ tuner.py.

При вызове `/scrape tune` — запускает tuner.py вручную и показывает рекомендации по параметрам.

---

## Инструкция для Claude

**Шаг 1 — определи команду:**

- Нет аргументов или "status": показать статус
- URL (содержит youtube.com или youtu.be): запустить скрапер
- "tune": запустить tuner.py
- "report": запустить `python tuner.py --report`

**Шаг 2 — статус (без аргументов):**

```bash
# Считаем файлы по папкам
Get-ChildItem "output\" -Directory | ForEach-Object {
    $count = (Get-ChildItem $_.FullName -File -Filter "*.txt" | Where-Object { $_.Name -ne "skipped.txt" }).Count
    Write-Host "$($_.Name): $count файлов"
}

# Текущие параметры
Get-Content tuned_params.json | ConvertFrom-Json | Select-Object -ExpandProperty params

# Последние 5 строк skipped.txt (если есть)
Get-ChildItem output\ -Recurse -Filter skipped.txt | ForEach-Object { Get-Content $_.FullName -Tail 5 }
```

**Шаг 3 — запуск скрапера для URL:**

```bash
python scraper.py <URL> --cookies cookies.txt
```

Параметры delay/batch-size/batch-pause читаются автоматически из tuned_params.json — не нужно передавать вручную.

**Шаг 4 — после завершения:**

Запустить tuner.py для подстройки параметров следующего запуска:
```bash
python tuner.py --report
```

Затем закоммитить изменения если есть новые файлы в scraper.py / tuner.py.

---

## Важные правила

- Никогда не использовать `subtitleslangs: ["all"]` — вызывает burst запросы
- Не тестировать YouTube во время бана (скользящее окно!)
- Все задержки случайные — не вводить фиксированные паузы
- Если серийные скипы (>5 подряд) — остановить, проверить rate limiting
- cookies.txt не коммитить в git (в .gitignore)
