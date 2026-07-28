"""Счётчики по записи (scan.py) на синтетике с ЗАРАНЕЕ известным ответом.

Смысл проверки: инструмент, который должен спасать от подгонки, сам обязан
считать правильно. Поэтому каждая запись здесь собрана так, что ответ виден
глазами до запуска.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scan  # noqa: E402


def write(tmp_path, rows) -> str:
    p = tmp_path / "rec.jsonl"
    with open(p, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return str(p)


def tick(t, slug="r1", left=100.0, ua=0.60, da=0.41, ub=0.59, db=0.40,
         price=65_005.0, pm=65_000.0, uas=100.0, das=100.0, pm_age=100):
    return {"t": t, "slug": slug, "left": left, "price": price, "pm": pm,
            "pm_age": pm_age, "ua": ua, "da": da, "ub": ub, "db": db,
            "uas": uas, "das": das}


def settle(slug, winner, resolved=True):
    return {"type": "settle", "slug": slug, "winner": winner,
            "resolved": resolved, "t": 999.0}


# ---------------------------------------------------------------------------
#  1. Арбитраж
# ---------------------------------------------------------------------------
class TestArb:
    def test_reports_nothing_when_book_never_offers_it(self, tmp_path, capsys):
        # 0.60 + 0.41 = 1.01 — пара дороже доллара, брать нечего
        rows = [tick(i * 0.1) for i in range(50)]
        scan.main([write(tmp_path, rows), "--arb"])
        out = capsys.readouterr().out
        assert "не встретилось ни разу" in out

    def test_counts_profit_limited_by_size(self, tmp_path, capsys):
        # 0.48 + 0.50 = 0.98 => 2¢ с пары, но взять можно только 10 пар
        rows = [tick(1.0, ua=0.48, da=0.50, uas=10.0, das=250.0)]
        scan.main([write(tmp_path, rows), "--arb"])
        out = capsys.readouterr().out
        assert "$0.20" in out            # 10 пар * 2¢, а не 250 * 2¢
        assert "0.9800" in out

    def test_nearby_ticks_are_one_episode(self, tmp_path, capsys):
        """Одна и та же возможность, увиденная 20 раз, — это один шанс."""
        rows = [tick(1.0 + i * 0.05, ua=0.48, da=0.50, uas=10.0, das=10.0)
                for i in range(20)]
        scan.main([write(tmp_path, rows), "--arb"])
        out = capsys.readouterr().out
        assert "тактов с арбитражем : 20" in out
        assert "отдельных эпизодов  : 1" in out


# ---------------------------------------------------------------------------
#  2. Премия за определённость
# ---------------------------------------------------------------------------
class TestCertain:
    def test_one_observation_per_round(self, tmp_path, capsys):
        """Соседние такты не независимы — на раунд берём один."""
        rows = []
        for n in range(3):
            slug = f"r{n}"
            for i in range(40):           # 40 тактов в хвосте одного раунда
                rows.append(tick(n * 100 + i, slug=slug, left=25.0 - i * 0.1,
                                 ua=0.98, da=0.02))
            rows.append(settle(slug, "Up"))
        scan.main([write(tmp_path, rows), "--certain"])
        out = capsys.readouterr().out
        assert "наблюдений          : 3" in out
        assert "выиграла эта сторона: 3" in out

    def test_unresolved_rounds_are_skipped(self, tmp_path, capsys):
        rows = [tick(1.0, slug="r1", left=10.0, ua=0.98, da=0.02),
                settle("r1", None, resolved=False)]
        scan.main([write(tmp_path, rows), "--certain"])
        out = capsys.readouterr().out
        assert "таких моментов в записи нет" in out

    def test_zero_losses_is_not_never(self, tmp_path, capsys):
        """Главная честность инструмента: 0 из 5 ещё ничего не доказывает."""
        rows = []
        for n in range(5):
            slug = f"r{n}"
            rows.append(tick(n * 10, slug=slug, left=10.0, ua=0.98, da=0.02))
            rows.append(settle(slug, "Up"))
        scan.main([write(tmp_path, rows), "--certain"])
        out = capsys.readouterr().out
        assert "Это НЕ «никогда»" in out
        assert "ДАННЫХ ПОКА НЕ ХВАТАЕТ" in out.upper()

    def test_a_single_loss_is_shown_in_context(self, tmp_path, capsys):
        rows = []
        for n in range(4):
            slug = f"r{n}"
            rows.append(tick(n * 10, slug=slug, left=10.0, ua=0.98, da=0.02))
            rows.append(settle(slug, "Up" if n < 3 else "Down"))
        scan.main([write(tmp_path, rows), "--certain"])
        out = capsys.readouterr().out
        assert "Проигрышей: 1" in out
        assert "съедает 49 выигрышей" in out


# ---------------------------------------------------------------------------
#  3. Лаг якоря
# ---------------------------------------------------------------------------
class TestLag:
    def test_needs_enough_observations(self, tmp_path, capsys):
        rows = [tick(i * 0.1) for i in range(20)]
        scan.main([write(tmp_path, rows), "--lag"])
        out = capsys.readouterr().out
        assert "мало, нужна запись подлиннее" in out

    def test_finds_a_planted_signal(self, tmp_path, capsys):
        """Лаг, который РЕАЛЬНО двигает книгу, должен найтись."""
        rows = []
        for i in range(400):
            lag = (i % 20) - 10.0                 # от -10 до +9
            mid_now = 0.50 + lag * 0.001          # книга поедет туда же
            rows.append(tick(i * 0.5, price=65_000.0 + lag, pm=65_000.0,
                             ub=mid_now - 0.005, ua=mid_now + 0.005))
        scan.main([write(tmp_path, rows), "--lag", "--horizon", "0.5"])
        out = capsys.readouterr().out
        assert "СИЛЬНО" in out

    def test_does_not_look_across_round_boundary(self, tmp_path, capsys):
        """Будущее из СЛЕДУЮЩЕГО раунда — это заглядывание вперёд."""
        rows = [tick(i * 0.5, slug="r1") for i in range(10)]
        rows += [tick(5.0 + i * 0.5, slug="r2") for i in range(10)]
        scan.main([write(tmp_path, rows), "--lag", "--horizon", "0.5"])
        out = capsys.readouterr().out
        # Наблюдений слишком мало — но главное, что не было падения и
        # соседний раунд не подмешался.
        assert "мало, нужна запись подлиннее" in out
