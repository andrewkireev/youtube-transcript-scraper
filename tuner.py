"""
Адаптивный тюнер лимитов YouTube-парсера.

Читает session_log.jsonl, анализирует блокировки (когда, почему, при каком
темпе), и вычисляет оптимальные параметры для следующего запуска.
Результат пишется в tuned_params.json — scraper.py читает его при старте.

Запускается автоматически scraper.py после каждой сессии.
Можно запустить вручную: python tuner.py [--report]
"""
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LOG_FILE = Path("session_log.jsonl")
PARAMS_FILE = Path("tuned_params.json")

# Стартовые параметры если истории нет
DEFAULT_PARAMS = {"delay": 10.0, "batch_size": 10, "batch_pause": 300.0}

# Границы — не даём зайти за них ни в какую сторону
LIMITS = {
    "delay":       (3.0,  30.0),
    "batch_size":  (3,    20),
    "batch_pause": (60.0, 900.0),
}


# ─────────────────────────── чтение лога ────────────────────────────────────

def load_sessions() -> list[dict]:
    """Парсит session_log.jsonl → список сессий с событиями внутри."""
    if not LOG_FILE.exists():
        return []

    raw: list[dict] = []
    for line in LOG_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                raw.append(json.loads(line))
            except json.JSONDecodeError:
                pass

    sessions: list[dict] = []
    cur: dict | None = None

    for ev in raw:
        t = ev.get("type")
        if t == "session_start":
            cur = {
                "id":      ev.get("session_id"),
                "params":  ev.get("params", {}),
                "started": ev.get("ts"),
                "ended":   None,
                "calls":   [],   # каждый API-запрос
                "summary": {},
            }
        elif t == "api_call" and cur is not None:
            cur["calls"].append(ev)
        elif t == "session_end" and cur is not None:
            cur["ended"]   = ev.get("ts")
            cur["summary"] = ev.get("summary", {})
            sessions.append(cur)
            cur = None

    return sessions


# ─────────────────────────── анализ сессии ──────────────────────────────────

def analyze_session(s: dict) -> dict:
    """Извлекает ключевые метрики из одной сессии."""
    calls = s["calls"]
    if not calls:
        return {"api_calls": 0, "bans": 0, "calls_before_first_ban": None,
                "rate_at_ban": None, "avg_interval": None}

    ban_indices = [i for i, c in enumerate(calls) if c.get("result") == "rate_limited"]
    bans = len(ban_indices)

    calls_before_first_ban = ban_indices[0] if ban_indices else len(calls)

    # Средний интервал между запросами в N вызовах перед первым баном
    rate_at_ban = None
    if ban_indices:
        window = calls[max(0, ban_indices[0] - 8): ban_indices[0]]
        if len(window) >= 2:
            intervals = []
            for j in range(1, len(window)):
                dt = window[j]["ts"] - window[j-1]["ts"]
                if dt > 0:
                    intervals.append(dt)
            if intervals:
                avg_iv = sum(intervals) / len(intervals)
                rate_at_ban = round(60 / avg_iv, 2)  # запросов в минуту

    # Общий средний интервал по всей сессии
    avg_interval = None
    if len(calls) >= 2:
        total_elapsed = calls[-1]["ts"] - calls[0]["ts"]
        avg_interval = round(total_elapsed / (len(calls) - 1), 1) if total_elapsed > 0 else None

    return {
        "api_calls":              len(calls),
        "bans":                   bans,
        "calls_before_first_ban": calls_before_first_ban,
        "rate_at_ban":            rate_at_ban,   # req/min
        "avg_interval":           avg_interval,  # сек
    }


# ─────────────────────────── алгоритм тюнинга ───────────────────────────────

def tune(sessions: list[dict]) -> tuple[dict, str]:
    """
    Вычисляет новые параметры на основе истории сессий.
    Возвращает (params_dict, объяснение).
    """
    if not sessions:
        return DEFAULT_PARAMS.copy(), "Нет истории — используем стартовые параметры."

    analyses = [(s, analyze_session(s)) for s in sessions]
    recent   = analyses[-6:]   # последние 6 сессий для анализа

    last_s, last_a = recent[-1]
    cur_params = last_s["params"]

    # Базируемся на текущих params (или defaults если не записаны)
    delay       = float(cur_params.get("delay",       DEFAULT_PARAMS["delay"]))
    batch_size  = int(  cur_params.get("batch_size",  DEFAULT_PARAMS["batch_size"]))
    batch_pause = float(cur_params.get("batch_pause", DEFAULT_PARAMS["batch_pause"]))

    lines = []   # объяснение решения

    # ── Случай 1: последняя сессия была заблокирована ──
    if last_a["bans"] > 0:
        cbfb = last_a["calls_before_first_ban"]
        rab  = last_a["rate_at_ban"]

        lines.append(f"⛔  Последняя сессия: БАН на запросе #{cbfb} из {last_a['api_calls']}.")
        if rab:
            lines.append(f"   Темп перед баном: {rab:.1f} req/min. Это слишком быстро.")

        # Если бан пришёл быстро (≤15 запросов) — IP ещё горячий или слишком
        # агрессивный темп в самом начале → замедляемся сильнее
        if cbfb is not None and cbfb <= 15:
            factor_delay  = 1.45
            factor_batch  = 0.70
            factor_pause  = 1.40
            lines.append("   Бан случился очень быстро — замедляемся значительно.")
        else:
            factor_delay  = 1.20
            factor_batch  = 0.85
            factor_pause  = 1.20
            lines.append("   Бан случился позже — замедляемся умеренно.")

        # Если несколько сессий подряд с банами — удваиваем агрессию торможения
        consecutive_bans = sum(1 for _, a in reversed(recent) if a["bans"] > 0)
        if consecutive_bans >= 3:
            factor_delay  = min(factor_delay  * 1.3, 2.0)
            factor_batch  = max(factor_batch  * 0.8, 0.5)
            factor_pause  = min(factor_pause  * 1.3, 2.0)
            lines.append(f"   {consecutive_bans} сессии подряд с банами — торможение ×1.3.")

        delay       = delay       * factor_delay
        batch_size  = max(LIMITS["batch_size"][0],  int(batch_size * factor_batch))
        batch_pause = batch_pause * factor_pause

    # ── Случай 2: последняя сессия прошла чисто ──
    else:
        lines.append(f"✅  Последняя сессия без банов ({last_a['api_calls']} запросов).")

        # Сколько подряд чистых сессий?
        clean_streak = 0
        for _, a in reversed(recent):
            if a["bans"] == 0:
                clean_streak += 1
            else:
                break

        lines.append(f"   Чистых сессий подряд: {clean_streak}.")

        if clean_streak >= 4:
            # Уверенно ускоряемся
            factor_delay  = 0.80
            factor_batch  = 1.25
            factor_pause  = 0.80
            lines.append("   4+ чистых подряд — ускоряемся заметно.")
        elif clean_streak >= 2:
            factor_delay  = 0.88
            factor_batch  = 1.15
            factor_pause  = 0.88
            lines.append("   2+ чистых подряд — ускоряемся аккуратно.")
        else:
            factor_delay  = 0.93
            factor_batch  = 1.10
            factor_pause  = 0.93
            lines.append("   1 чистая сессия — ускоряемся умеренно.")

        delay       = delay       * factor_delay
        batch_size  = min(LIMITS["batch_size"][1],  int(math.ceil(batch_size * factor_batch)))
        batch_pause = batch_pause * factor_pause

    # Применяем ограничения
    delay       = round(max(LIMITS["delay"][0],       min(LIMITS["delay"][1],       delay)),       1)
    batch_size  = max(LIMITS["batch_size"][0],        min(LIMITS["batch_size"][1],  batch_size))
    batch_pause = round(max(LIMITS["batch_pause"][0], min(LIMITS["batch_pause"][1], batch_pause)), 0)

    # Теоретическая скорость
    batch_duration = batch_size * delay
    cycle_sec      = batch_duration + batch_pause
    rate_hr        = round(batch_size / cycle_sec * 3600)
    lines.append(f"📊  Новые параметры: delay={delay}с | batch={batch_size} | pause={batch_pause}с")
    lines.append(f"   Ожидаемая скорость: ~{rate_hr} видео/час (без учёта already-exists пропусков).")

    explanation = "\n".join(lines)
    params = {"delay": delay, "batch_size": batch_size, "batch_pause": batch_pause}
    return params, explanation


# ─────────────────────────── точка входа ────────────────────────────────────

def run(verbose: bool = True) -> dict:
    sessions  = load_sessions()
    params, explanation = tune(sessions)

    # Сохраняем
    PARAMS_FILE.write_text(
        json.dumps({
            "params": params,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "sessions_analyzed": len(sessions),
            "explanation": explanation,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    if verbose:
        print("\n" + "─" * 56)
        print("  🤖  TUNER — анализ и подстройка параметров")
        print("─" * 56)
        if sessions:
            print(f"  Сессий в истории: {len(sessions)}")
            # Краткая таблица последних сессий
            print(f"\n  {'Дата':<17} {'Calls':>6} {'Сохр':>5} {'Баны':>5} {'#бан':>6} {'req/min':>8}")
            print(f"  {'─'*17} {'─'*6} {'─'*5} {'─'*5} {'─'*6} {'─'*8}")
            for s, a in [(s, analyze_session(s)) for s in sessions[-6:]]:
                ts   = datetime.fromtimestamp(s["started"]).strftime("%m-%d %H:%M") if s["started"] else "?"
                saved = s["summary"].get("saved", "?")
                bans  = a["bans"]
                cbfb  = a["calls_before_first_ban"] if a["bans"] else "—"
                rab   = f"{a['rate_at_ban']:.1f}" if a.get("rate_at_ban") else "—"
                print(f"  {ts:<17} {a['api_calls']:>6} {str(saved):>5} {bans:>5} {str(cbfb):>6} {rab:>8}")
        else:
            print("  Истории нет — первый запуск.")

        print()
        for line in explanation.split("\n"):
            print(f"  {line}")
        print("─" * 56 + "\n")

    return params


if __name__ == "__main__":
    verbose = "--report" in sys.argv or True
    run(verbose=verbose)
