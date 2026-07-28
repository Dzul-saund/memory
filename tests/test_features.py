"""Тесты измерителя признаков.

Главное требование: инструмент должен находить сигнал там, где он ЕСТЬ,
и не находить там, где его НЕТ. Иначе он будет уверенно показывать
корреляции на шуме — а это хуже, чем не иметь инструмента вовсе.
"""
import json
import math
import os
import random
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import features as F


def _write(path, rows):
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _round(slug, n, price_fn, mid_fn, t0=0.0, dt=0.25):
    """Один раунд: цена и середина книги задаются функциями от шага."""
    out = []
    for i in range(n):
        p = price_fn(i)
        m = mid_fn(i)
        out.append({"t": t0 + i * dt, "slug": slug, "left": 300 - i * dt,
                    "price": p, "target": 65_000.0, "sigma": 3.0,
                    "ub": round(m - 0.005, 4), "ua": round(m + 0.005, 4),
                    "db": round(1 - m - 0.005, 4), "da": round(1 - m + 0.005, 4),
                    "uf": 0.0, "df": 0.0, "pm": p - 5.0,
                    "ubs": 100, "uas": 100, "tb": 0, "tsl": 0})
    return out


class TestSpearman:
    def test_perfect_positive(self):
        x = list(range(100))
        assert F.spearman(x, x) == pytest.approx(1.0, abs=1e-9)

    def test_perfect_negative(self):
        x = list(range(100))
        assert F.spearman(x, x[::-1]) == pytest.approx(-1.0, abs=1e-9)

    def test_pure_noise_is_near_zero(self):
        random.seed(1)
        x = [random.random() for _ in range(3000)]
        y = [random.random() for _ in range(3000)]
        assert abs(F.spearman(x, y)) < 0.06

    def test_monotone_but_nonlinear_still_one(self):
        """Ранговая корреляция не должна зависеть от формы, только от порядка."""
        x = list(range(1, 200))
        y = [v ** 3 for v in x]
        assert F.spearman(x, y) == pytest.approx(1.0, abs=1e-9)

    def test_outlier_does_not_fake_correlation(self):
        """Одна аномалия не должна рисовать корреляцию — ради этого ранги."""
        random.seed(2)
        x = [random.random() for _ in range(500)] + [1e9]
        y = [random.random() for _ in range(500)] + [1e9]
        assert abs(F.spearman(x, y)) < 0.15

    def test_too_few_points_returns_none(self):
        assert F.spearman([1, 2, 3], [1, 2, 3]) is None


class TestSignalDetection:
    def test_finds_a_planted_signal(self, tmp_path):
        """Книга ОТСТАЁТ от цены: mid повторяет цену с задержкой.

        Цена — случайное блуждание (иначе скорость постоянна и корреляцию
        считать не от чего). Середина книги следует за ценой, отставая на
        LAG шагов. Значит скорость цены обязана предсказывать будущий сдвиг
        mid — если инструмент этого не видит, он бесполезен.
        """
        random.seed(11)
        LAG = 4
        p = tmp_path / "r.jsonl"
        rows = []
        for k in range(6):
            n = 200
            walk = [0.0]
            for _ in range(n + LAG):
                walk.append(walk[-1] + random.gauss(0, 3.0))
            prices = [65_000 + walk[i + LAG] for i in range(n)]
            mids = [min(0.95, max(0.05, 0.5 + walk[i] * 0.01)) for i in range(n)]
            rows += _round(f"s{k}", n,
                           price_fn=lambda i, pr=prices: pr[i],
                           mid_fn=lambda i, md=mids: md[i],
                           t0=k * 10_000)
        _write(p, rows)
        feats, fwd = F.build(F.read(str(p)), horizon=1.0)
        assert len(fwd) > 500
        xs = [f["скорость $/с"] for f in feats if "скорость $/с" in f]
        ys = [y for f, y in zip(feats, fwd) if "скорость $/с" in f]
        ic = F.spearman(xs, ys)
        assert ic is not None and ic > 0.1, f"сигнал не найден: IC={ic}"

    def test_finds_nothing_in_pure_noise(self, tmp_path):
        """Цена и книга независимы — все IC обязаны быть около нуля."""
        random.seed(3)
        p = tmp_path / "n.jsonl"
        rows = []
        for k in range(6):
            rows += _round(
                f"s{k}", 120,
                price_fn=lambda i: 65_000 + random.gauss(0, 5),
                mid_fn=lambda i: min(0.95, max(0.05, 0.5 + random.gauss(0, 0.05))),
                t0=k * 1000)
        _write(p, rows)
        feats, fwd = F.build(F.read(str(p)), horizon=1.0)
        for nm in ("скорость $/с", "дисбаланс книги"):
            xs = [f[nm] for f in feats if nm in f]
            ys = [y for f, y in zip(feats, fwd) if nm in f]
            ic = F.spearman(xs, ys)
            if ic is not None:
                assert abs(ic) < 0.12, f"{nm}: нашёл сигнал в шуме, IC={ic}"


class TestNoLookahead:
    def test_future_never_crosses_a_round_boundary(self, tmp_path):
        """Раунд заканчивается сбросом к 0/1. Если заглянуть за границу,
        получится гигантский ложный сигнал — этого быть не должно."""
        p = tmp_path / "b.jsonl"
        rows = _round("s1", 40, lambda i: 65_000 + i, lambda i: 0.40)
        rows.append({"type": "settle", "slug": "s1", "winner": "Up",
                     "resolved": True, "t": 999.0})
        rows += _round("s2", 40, lambda i: 65_000 + i, lambda i: 0.95,
                       t0=1000.0)
        _write(p, rows)
        feats, fwd = F.build(F.read(str(p)), horizon=1.0)
        # внутри раунда середина постоянна -> будущее изменение ровно 0
        assert fwd, "наблюдения должны быть"
        assert max(abs(y) for y in fwd) < 0.01, (
            "изменение mid просочилось через границу раунда")

    def test_horizon_is_respected(self, tmp_path):
        p = tmp_path / "h.jsonl"
        rows = _round("s1", 100, lambda i: 65_000.0,
                      lambda i: 0.30 + i * 0.001, dt=0.25)
        _write(p, rows)
        feats, fwd = F.build(F.read(str(p)), horizon=2.0)
        # за 2с проходит 8 шагов по 0.001 -> ~0.008
        assert st_med(fwd) == pytest.approx(0.008, abs=0.002)


def st_med(v):
    s = sorted(v)
    return s[len(s) // 2]


def test_empty_recording_is_handled(tmp_path):
    p = tmp_path / "e.jsonl"
    p.write_text("", encoding="utf-8")
    feats, fwd = F.build(F.read(str(p)), horizon=1.0)
    assert feats == [] and fwd == []


def test_broken_lines_are_skipped(tmp_path):
    p = tmp_path / "x.jsonl"
    with open(p, "w", encoding="utf-8") as f:
        f.write("мусор\n")
        for r in _round("s1", 40, lambda i: 65_000 + i, lambda i: 0.40):
            f.write(json.dumps(r) + "\n")
        f.write('{"обрыв\n')
    feats, fwd = F.build(F.read(str(p)), horizon=1.0)
    assert len(fwd) > 10
