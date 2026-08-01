"""Balance + order placement.

Two implementations with the same interface (``get_balance``, ``buy``,
``setup_allowances``):

* :class:`LiveTrader` — real orders via ``py-clob-client``.
* :class:`DryRunTrader` — simulates orders/balance so the whole pipeline can be
  exercised with no risk and (optionally) no credentials.

``py-clob-client`` is imported lazily inside the functions that need it, so the
bot can run fully in dry-run with only ``requests`` installed.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Optional

from .config import Config
from .util import floor2, whole_shares, with_retry


# ---------------------------------------------------------------------------
#  py-clob-client wiring
# ---------------------------------------------------------------------------
def build_clob_client(cfg: Config):
    """Construct an authenticated CLOB client (needs PRIVATE_KEY).

    Uses py-clob-client-v2, which supports the newer Polymarket deposit-wallet
    signature type 3 (POLY_1271). The old py-clob-client (v1) rejects type-3
    orders locally with "Invalid order inputs", so v2 is required for accounts
    created via the email/Magic deposit-wallet flow.
    """
    from py_clob_client_v2.client import ClobClient
    from py_clob_client_v2.clob_types import ApiCreds

    client = ClobClient(
        cfg.clob_host,
        key=cfg.private_key,
        chain_id=cfg.chain_id,
        signature_type=cfg.signature_type,
        funder=cfg.funder,
    )
    if cfg.api_key and cfg.api_secret and cfg.api_passphrase:
        client.set_api_creds(
            ApiCreds(cfg.api_key, cfg.api_secret, cfg.api_passphrase)
        )
    else:
        # Derive (or create) the L2 API credentials from the private key.
        client.set_api_creds(client.create_or_derive_api_key())
    return client


# ---------------------------------------------------------------------------
#  Live trader
# ---------------------------------------------------------------------------
class LiveTrader:
    def __init__(self, client, cfg: Config, logger):
        self.client = client
        self.cfg = cfg
        self.log = logger
        # Предподписанные BUY-ордера: (token, цена_2зн, размер) -> signed.
        # Подпись (create_order) — самая медленная часть отправки (~5мс
        # ECDSA + при первом ордере на токен ещё и сетевые запросы neg_risk
        # /tick_size на ~100-300мс). Готовим их в фоне на старте окна, чтобы
        # в момент решения осталась только мгновенная post_order (отправка).
        self._presigned = {}
        self._presign_lock = threading.Lock()
        self._start_http_keepalive()

    # -- предподписание -----------------------------------------------------
    def presign_window(self, tokens, price_min, price_max, trade_size):
        """Фоново заготовить подписанные BUY-ордера на все цены полосы для
        токенов текущего окна. FAK-ордера с expiration=0 не протухают, так
        что подпись в начале окна годна до самого конца.

        ЗАЧЕМ ЭТО ИЗМЕРЕНО, А НЕ ПРЕДПОЛОЖЕНО. Зонд `pingorder.py` на боевом
        сервере показал: первый ордер на новый токен стоит на 98 мс дороже
        остальных — `py_clob_client_v2` при первой подписи ходит в сеть за
        `neg_risk` и `tick_size`. Токены меняются каждые 5 минут, поэтому без
        предподписания эти 98 мс платит ПЕРВЫЙ ордер КАЖДОГО раунда, то есть
        первая ступень лестницы — самая важная.

        `trade_size` — доллары на ордер. Принимает и одно число, и список:
        у лестницы ступени разного размера (10 и 20), и подписать надо все,
        иначе ключ не совпадёт и заготовка не пригодится.
        """
        if not getattr(self.cfg, "presign_enabled", True):
            return
        try:
            sizes = ([float(trade_size)] if isinstance(trade_size, (int, float))
                     else [float(x) for x in trade_size])
        except (TypeError, ValueError):
            return
        sizes = sorted({s for s in sizes if s > 0})
        if not sizes:
            return
        cents = range(int(round(price_min * 100)), int(round(price_max * 100)) + 1)
        jobs = [(str(tok), c / 100.0, usd) for tok in tokens if tok
                for c in cents for usd in sizes]

        def _work():
            fresh = {}
            for token_id, price, usd in jobs:
                # РАЗМЕР СЧИТАЕТСЯ ТАК ЖЕ, КАК В БОЮ (`whole_shares`), иначе
                # ключ заготовки не совпадёт с ключом реального ордера и вся
                # эта работа пропадёт молча.
                size = whole_shares(usd, price,
                                    getattr(self.cfg, "min_order_usdc", 0.0))
                if size <= 0:
                    continue
                try:
                    signed = self._build_signed(token_id, price, size, "BUY")
                except Exception as exc:  # noqa: BLE001 - не мешать торговле
                    self.log.debug("presign %s@%.2f failed: %s",
                                   token_id[:8], price, exc)
                    continue
                fresh[(token_id, round(price, 2), size)] = signed
            with self._presign_lock:
                self._presigned = fresh
            if fresh:
                self.log.info("Pre-signed %d BUY orders for this window "
                              "(order send is now just a POST).", len(fresh))

        threading.Thread(target=_work, name="presign", daemon=True).start()

    def _build_signed(self, token_id, price, size, side):
        from py_clob_client_v2.clob_types import OrderArgsV2
        order = OrderArgsV2(token_id=token_id, price=round(price, 2),
                            size=round(size, 2), side=side)
        return self.client.create_order(order)

    def _start_http_keepalive(self) -> None:
        """Keep the CLOB HTTP/2 connection pool warm.

        py-clob-client-v2 reuses one httpx pool, but an idle TLS connection
        gets dropped by the server/NAT after tens of seconds — and then the
        FIRST order after a quiet stretch pays a fresh TLS handshake
        (+100-300 ms exactly when speed matters). A tiny GET through the
        same pool every 10s keeps the socket hot so every order goes out on
        an established connection.
        """
        try:
            from py_clob_client_v2.http_helpers import helpers as _h
            http_client = _h._http_client
        except Exception:  # noqa: BLE001 - internals changed: skip silently
            return

        host = self.cfg.clob_host.rstrip("/")

        def _keepalive():
            while True:
                try:
                    http_client.get(host + "/", timeout=3)
                except Exception:  # noqa: BLE001 - never disturb trading
                    pass
                time.sleep(10)

        threading.Thread(target=_keepalive, name="clob-keepalive",
                         daemon=True).start()
        self.log.info("CLOB keep-alive ON: orders go out on a warm "
                      "connection (no TLS handshake before an order).")

    def position(self, token_id: str):
        """Сколько шэров этого токена РЕАЛЬНО лежит на кошельке.

        Возвращает сырой ответ биржи; разбирает его flowbot.positions. Нужен
        для сверки: собственный учёт бота расходится с биржей при таймаутах,
        частичных филлах и неверно разобранных ответах, а без сверки такое
        расхождение живёт вечно.
        """
        from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams

        def _do():
            return self.client.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL,
                                       token_id=str(token_id),
                                       signature_type=self.cfg.signature_type)
            )

        return with_retry(
            _do,
            retries=self.cfg.max_retries,
            base=self.cfg.backoff_base_seconds,
            max_backoff=self.cfg.max_backoff_seconds,
            what="position",
            logger=self.log,
        )

    def get_balance(self) -> float:
        """USDC collateral balance, in dollars."""
        from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams

        def _do():
            return self.client.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            )

        resp = with_retry(
            _do,
            retries=self.cfg.max_retries,
            base=self.cfg.backoff_base_seconds,
            max_backoff=self.cfg.max_backoff_seconds,
            what="get_balance_allowance",
            logger=self.log,
        )
        raw = resp.get("balance") if isinstance(resp, dict) else None
        # USDC has 6 decimals; the API returns base units as a string.
        return float(raw) / 1e6 if raw is not None else 0.0

    def buy(self, token_id: str, price: float, size: float, resting: bool = False):
        """Place a BUY order capped at ``price``.

        ``resting=False`` (default): marketable limit Fill-And-Kill — fills
        immediately against resting liquidity up to the limit and cancels any
        remainder, so nothing rests on the book and we never pay more than
        ``price``. ``resting=True``: Good-Till-Cancelled — the order rests on
        the book at ``price`` and fills later if the market trades down to it
        (used for the lottery hedge).
        """
        from py_clob_client_v2.clob_types import OrderArgsV2, OrderType

        order_type = OrderType.GTC if resting else OrderType.FAK

        # Быстрый путь: заранее подписанный FAK-ордер на ровно эти
        # (токен, цена, размер) — отправляем без подписи. Только точное
        # совпадение; иначе (и для resting) — обычная подпись на месте.
        if not resting:
            key = (str(token_id), round(price, 2), float(int(round(size))))
            with self._presign_lock:
                signed = self._presigned.pop(key, None)  # разово: salt не повтор
            if signed is not None:
                return self.client.post_order(signed, order_type)

        order = OrderArgsV2(
            token_id=token_id,
            price=round(price, 2),
            size=round(size, 2),
            side="BUY",
        )
        signed = self.client.create_order(order)
        return self.client.post_order(signed, order_type)

    def sell(self, token_id: str, price: float, size: float, resting: bool = False):
        """Place a SELL order with a floor of ``price`` (used to exit/cut losses).

        ``resting=False`` (default): marketable limit Fill-And-Kill — sells
        immediately against resting bids at/above ``price`` and cancels any
        remainder, so we never sell below ``price``. ``resting=True``: a
        Good-Till-Cancelled order that rests on the book at ``price``.
        """
        from py_clob_client_v2.clob_types import OrderArgsV2, OrderType

        # РАЗМЕР ПРОДАЖИ УСЕКАЕТСЯ ВНИЗ, НИКОГДА НЕ ОКРУГЛЯЕТСЯ.
        #
        # Здесь стояло round(size, 2), и это неверно при любых входных
        # данных: продать больше, чем лежит на кошельке, физически нельзя.
        # Позиция в 0.067795 шэра превращалась в 0.07 -> ордер на 70000
        # сырых единиц против баланса 67795 -> 400 "not enough balance",
        # и так на каждом такте, потому что выход паузой не придерживается.
        # Вверх округлять можно цену (это в нашу пользу), размер — нет.
        size = floor2(size)
        if size <= 0:
            raise ValueError(
                f"размер продажи после усечения = 0 (было {size!r}); "
                "продавать нечего — остаток меньше сотой шэра")

        order = OrderArgsV2(
            token_id=token_id,
            price=round(price, 2),
            size=size,
            side="SELL",
        )
        signed = self.client.create_order(order)
        order_type = OrderType.GTC if resting else OrderType.FAK
        return self.client.post_order(signed, order_type)

    def setup_allowances(self):
        """Set the USDC allowance for trading (run once if needed)."""
        from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams

        return self.client.update_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )

    def settle(self, payout: float) -> None:
        """No-op: on the real platform Polymarket credits the payout to your
        balance automatically when the market resolves. Nothing to do here."""
        return None


# ---------------------------------------------------------------------------
#  Dry-run trader
# ---------------------------------------------------------------------------
class DryRunTrader:
    """Simulates trading. Balance can optionally be backed by a real provider
    (so you can watch your *actual* balance while not risking it)."""

    def __init__(
        self,
        cfg: Config,
        logger,
        balance_provider: Optional[Callable[[], float]] = None,
    ):
        self.cfg = cfg
        self.log = logger
        self._provider = balance_provider
        self._start = cfg.dry_run_balance
        self._spent = 0.0       # USDC paid out on (simulated) buys
        self._credited = 0.0    # USDC received from (simulated) winning settlements

    def position(self, token_id: str):
        """В симуляции сверять не с чем: биржа о наших ордерах не знает."""
        return None

    def get_balance(self) -> float:
        base = self._provider() if self._provider else self._start
        return base - self._spent + self._credited

    def buy(self, token_id: str, price: float, size: float, resting: bool = False):
        cost = round(price * size, 4)
        self._spent += cost
        return {
            "dry_run": True,
            "status": "simulated_fill",
            "token_id": token_id,
            "price": price,
            "size": size,
            "cost": cost,
            "resting": resting,
        }

    def settle(self, payout: float) -> None:
        """Credit a simulated settlement: $1 per winning share (0 if it lost)."""
        self._credited += round(payout, 4)

    def sell(self, token_id: str, price: float, size: float, resting: bool = False):
        """Simulate selling shares. The cash proceeds are credited by the
        caller via ``settle(proceeds)`` (same path winning settlements use),
        so balance bookkeeping stays in one place."""
        proceeds = round(price * size, 4)
        return {
            "dry_run": True,
            "status": "simulated_sell",
            "token_id": token_id,
            "price": price,
            "size": size,
            "proceeds": proceeds,
            "resting": resting,
        }

    def setup_allowances(self):
        return {"dry_run": True, "status": "noop"}

    def presign_window(self, *args, **kwargs):
        return None   # в dry-run подписывать нечего


# ---------------------------------------------------------------------------
#  Factory
# ---------------------------------------------------------------------------
def build_trader(cfg: Config, logger):
    """Pick the right trader for the run mode and available credentials."""
    if not cfg.dry_run:
        logger.info("LIVE mode: building authenticated CLOB client.")
        return LiveTrader(build_clob_client(cfg), cfg, logger)

    # Dry-run. If credentials are present, show the *real* balance.
    provider: Optional[Callable[[], float]] = None
    if cfg.private_key:
        try:
            live = LiveTrader(build_clob_client(cfg), cfg, logger)
            live.get_balance()  # probe once to confirm it works
            provider = live.get_balance
            logger.info("Dry-run: displaying your REAL balance (no orders sent).")
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Dry-run: could not read real balance (%s). "
                "Using simulated balance $%.2f.",
                exc, cfg.dry_run_balance,
            )
    else:
        logger.info(
            "Dry-run: no credentials — using simulated balance $%.2f.",
            cfg.dry_run_balance,
        )
    return DryRunTrader(cfg, logger, balance_provider=provider)
