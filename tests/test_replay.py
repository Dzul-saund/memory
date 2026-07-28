"""Тесты записи рынка и проигрывания.

Смысл связки: движок пишет ровно то, что видит стратегия, а replay.py
прогоняет ту же стратегию по записи. Значит на одних и тех же настройках
проигрывание обязано дать тот же результат, что и живой прогон — иначе
подбор порогов по записи ничего не стоит.
"""
import asyncio
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import replay as R
from flowbot.config import FlowConfig
from test_jump_engine import _book, _drain, _engine, _set_jump, _set_target


def test_records_one_line_per_tick(tmp_path):
    async def scenario():
        rec = tmp_path / "m.jsonl"
        eng, cfg = _engine(record_path=str(rec))
        _set_target(65_000.0)
        _book(eng, 0.59, 0.60, 0.40, 0.41)
        _set_jump(eng, price=65_010.0, dprice=8.0, horizon=cfg.jump_window_s)
        eng._tick()
        await _drain(eng)
        eng._tick()
        eng._rec.flush()
        lines = [json.loads(x)
                 for x in rec.read_text(encoding="utf-8").splitlines()]
        assert len(lines) == 2
        r = lines[0]
        assert r["price"] == pytest.approx(65_010.0)
        assert r["target"] == pytest.approx(65_000.0)
        assert r["ua"] == pytest.approx(0.60)
        assert r["db"] == pytest.approx(0.40)
    asyncio.run(scenario())


def test_settle_line_carries_the_winner(tmp_path):
    async def scenario():
        rec = tmp_path / "m.jsonl"
        eng, cfg = _engine(record_path=str(rec))
        _set_target(65_000.0)
        _book(eng, 0.59, 0.60, 0.40, 0.41)
        _set_jump(eng, price=65_010.0, dprice=8.0, horizon=cfg.jump_window_s)
        eng._tick()
        await _drain(eng)
        _book(eng, 0.99, 1.00, 0.00, 0.01)
        eng._tick()
        eng._settle_open_position("тест")
        eng._rec.flush()
        rows = [json.loads(x)
                for x in rec.read_text(encoding="utf-8").splitlines()]
        settle = [r for r in rows if r.get("type") == "settle"]
        assert len(settle) == 1
        assert settle[0]["winner"] == "Up"
        assert settle[0]["resolved"] is True
    asyncio.run(scenario())


def _write(path, rows):
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _ticks(n, price0, dprice, ua, ub, da, db, t0=1000.0, left=200.0):
    """Серия тактов с плавным ростом цены (движение от дна)."""
    out = []
    for i in range(n):
        out.append({"t": t0 + i * 0.5, "slug": "s1", "left": left - i * 0.5,
                    "price": price0 + dprice * i / max(1, n - 1),
                    "target": price0, "jump": dprice * i / max(1, n - 1),
                    "sigma": 0.9, "ua": ua, "ub": ub, "da": da, "db": db,
                    "uf": 0.0, "df": 0.0})
    return out


def _cfg(**over):
    c = FlowConfig()
    c.jump_adaptive = False
    c.jump_min_shift_cents = 0.0
    c.jump_min_edge_cents = 0.0
    c.jump_manage_enabled = False
    c.jump_ladder_grace_s = 0.0
    for k, v in over.items():
        setattr(c, k, v)
    return c


def test_replay_enters_and_settles_a_win(tmp_path):
    p = tmp_path / "r.jsonl"
    rows = _ticks(8, 65_000.0, 9.0, ua=0.60, ub=0.59, da=0.41, db=0.40)
    rows.append({"type": "settle", "slug": "s1", "winner": "Up",
                 "resolved": True, "t": 1100.0})
    _write(p, rows)
    res = R.replay(str(p), _cfg())
    assert res.entries == 1
    assert res.rounds == 1
    # вход $1 по 0.60 => 1.66 шэра, выплата $1.66
    assert res.pnl == pytest.approx(0.66, abs=0.02)


def test_replay_settles_a_loss(tmp_path):
    p = tmp_path / "r.jsonl"
    rows = _ticks(8, 65_000.0, 9.0, ua=0.60, ub=0.59, da=0.41, db=0.40)
    rows.append({"type": "settle", "slug": "s1", "winner": "Down",
                 "resolved": True, "t": 1100.0})
    _write(p, rows)
    res = R.replay(str(p), _cfg())
    assert res.pnl == pytest.approx(-1.0, abs=0.02)


def test_replay_respects_max_legs(tmp_path):
    """Тот же файл при разных лимитах даёт разное число доборов."""
    p = tmp_path / "r.jsonl"
    rows = _ticks(6, 65_000.0, 9.0, ua=0.60, ub=0.59, da=0.41, db=0.40)
    # цена развернулась: Up просел, Down подорожал -> повод для добора
    rows += [{"t": 1010.0 + i, "slug": "s1", "left": 150.0 - i,
              "price": 65_000.0, "target": 65_000.0, "jump": 0.0, "sigma": 0.9,
              "ua": 0.41, "ub": 0.40, "da": 0.60, "db": 0.59,
              "uf": 0.0, "df": 0.0} for i in range(6)]
    rows.append({"type": "settle", "slug": "s1", "winner": "Down",
                 "resolved": True, "t": 1100.0})
    _write(p, rows)
    no_ladder = R.replay(str(p), _cfg(jump_max_ladder_legs=0))
    with_ladder = R.replay(str(p), _cfg(jump_max_ladder_legs=2))
    assert no_ladder.ladders == 0
    assert with_ladder.ladders >= 1
    assert with_ladder.pnl > no_ladder.pnl, "добор должен был спасти раунд"


def test_replay_unresolved_round_marks_to_market(tmp_path):
    p = tmp_path / "r.jsonl"
    rows = _ticks(8, 65_000.0, 9.0, ua=0.60, ub=0.59, da=0.41, db=0.40)
    rows.append({"type": "settle", "slug": "s1", "winner": None,
                 "resolved": False, "t": 1100.0})
    _write(p, rows)
    res = R.replay(str(p), _cfg())
    assert res.unresolved == 1
    # закрыто по последнему биду 0.59, а не как выигрыш $1/шэр
    assert res.pnl == pytest.approx(1.66 * 0.59 - 1.0, abs=0.02)


def test_replay_skips_broken_lines(tmp_path):
    p = tmp_path / "r.jsonl"
    with open(p, "w", encoding="utf-8") as f:
        f.write("не json\n")
        for r in _ticks(8, 65_000.0, 9.0, 0.60, 0.59, 0.41, 0.40):
            f.write(json.dumps(r) + "\n")
        f.write('{"обрыв\n')
        f.write(json.dumps({"type": "settle", "slug": "s1", "winner": "Up",
                            "resolved": True, "t": 1100.0}) + "\n")
    res = R.replay(str(p), _cfg())
    assert res.entries == 1 and res.rounds == 1


def test_replay_of_engine_recording_matches_live_run(tmp_path):
    """Главная проверка: запись живого прогона, проигранная теми же
    настройками, даёт тот же P&L. Иначе подбор по записи бессмыслен."""
    async def scenario():
        rec = tmp_path / "live.jsonl"
        eng, cfg = _engine(record_path=str(rec))
        _set_target(65_000.0)
        _book(eng, 0.59, 0.60, 0.40, 0.41)
        _set_jump(eng, price=65_010.0, dprice=8.0, horizon=cfg.jump_window_s)
        eng._tick()
        await _drain(eng)
        _book(eng, 0.99, 1.00, 0.00, 0.01)
        eng._tick()
        eng._settle_open_position("тест")
        eng._rec.flush()
        live_pnl = eng.realized_pnl

        rcfg = _cfg()
        rcfg.jump_stake_usdc = cfg.jump_stake_usdc
        res = R.replay(str(rec), rcfg)
        assert res.pnl == pytest.approx(live_pnl, abs=0.02), (
            f"проигрывание {res.pnl:+.2f} != живой прогон {live_pnl:+.2f}")
    asyncio.run(scenario())
