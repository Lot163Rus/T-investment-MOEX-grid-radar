import asyncio
import os
import random
from dataclasses import dataclass
from datetime import timedelta
from math import log10
from statistics import stdev
from typing import List, Optional, Tuple

from tinkoff.invest import AsyncClient, CandleInterval, InstrumentStatus
from tinkoff.invest.utils import now

TOKEN = os.environ["INVEST_TOKEN"]

# MOEX классы: акции часто TQ**, фьючерсы SPBFUT
MOEX_CLASS_PREFIXES = ("TQ", "SPBFUT")

# -------------------- НАСТРОЙКИ --------------------
DAYS_BACK = 90
MIN_CANDLES = 40

# Параллелизм задач (общий). Держи 2-4, если не хочешь лимиты.
MAX_CONCURRENCY = 3

# Лимиты запросов/сек (мягкие). Можно поднимать, но осторожно.
CANDLES_QPS = 1.5        # свечи тяжелее
ORDERBOOK_QPS = 3.0      # стакан легче

# Фильтры ликвидности (средний дневной объём за 20 дней)
MIN_AVG_VOL_SHARES = 50_000
MIN_AVG_VOL_FUTURES = 2_000

# Спред фильтр
MAX_SPREAD_PCT = 0.25

# Волатильность (отсечь совсем сонные)
MIN_ATR_PCT = 0.10

TOP_N = 25
ORDERBOOK_DEPTH = 1

# Если хочешь тестить на маленьком наборе, заполни:
# ONLY_TICKERS = {"IMOEXF", "GAZP", "SBER", "VTBR"}
ONLY_TICKERS = None
# ---------------------------------------------------


@dataclass
class VolRow:
    inst_type: str
    ticker: str
    name: str
    class_code: str
    figi: str

    last_close: float
    atr_pct: float
    range_pct: float
    sigma_pct: float
    avg_vol20: float

    mid: float
    spread_pct: float

    grid_step_pct: float
    atr_band_low: float
    atr_band_high: float

    candles_used: int


def q_to_float(q) -> float:
    return float(q.units) + float(q.nano) / 1e9


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


class RateLimiter:
    """
    Простой rate limiter: не даёт делать чаще чем N запросов/сек.
    """
    def __init__(self, qps: float):
        self._interval = 1.0 / max(qps, 0.001)
        self._lock = asyncio.Lock()
        self._next = 0.0

    async def acquire(self):
        async with self._lock:
            loop = asyncio.get_running_loop()
            t = loop.time()
            if t < self._next:
                await asyncio.sleep(self._next - t)
            self._next = max(self._next + self._interval, t + self._interval)


def compute_metrics(ohlcv: List[Tuple[float, float, float, float, float]]) -> Optional[Tuple[float, float, float, float]]:
    """
    ohlcv: list of (open, high, low, close, volume) in chronological order
    returns: (last_close, atr_pct(14), range_pct(20), sigma_pct(20))
    """
    if len(ohlcv) < MIN_CANDLES:
        return None

    highs = [x[1] for x in ohlcv]
    lows = [x[2] for x in ohlcv]
    closes = [x[3] for x in ohlcv]

    last_close = closes[-1]
    if last_close <= 0:
        return None

    # True Range
    trs = []
    for i in range(1, len(ohlcv)):
        h, l, prev_c = highs[i], lows[i], closes[i - 1]
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        trs.append(tr)

    n = 14
    if len(trs) < n:
        return None

    atr = sum(trs[-n:]) / n
    atr_pct = 100.0 * atr / last_close

    # Avg daily range% last 20
    ranges = []
    for i in range(len(ohlcv)):
        c = closes[i]
        if c > 0:
            ranges.append((highs[i] - lows[i]) / c)
    last_n = ranges[-20:] if len(ranges) >= 20 else ranges
    range_pct = 100.0 * (sum(last_n) / len(last_n)) if last_n else 0.0

    # σ of daily returns% last 20
    rets = []
    for i in range(1, len(closes)):
        if closes[i - 1] > 0:
            rets.append((closes[i] / closes[i - 1]) - 1.0)
    last_r = rets[-20:] if len(rets) >= 20 else rets
    if len(last_r) < 10:
        return None
    sigma_pct = 100.0 * stdev(last_r)

    return last_close, atr_pct, range_pct, sigma_pct


def avg_volume_20(ohlcv: List[Tuple[float, float, float, float, float]]) -> float:
    vols = [v for *_, v in ohlcv if v is not None]
    last_v = vols[-20:] if len(vols) >= 20 else vols
    return sum(last_v) / len(last_v) if last_v else 0.0


def is_moex_like(class_code: str) -> bool:
    return class_code.startswith(MOEX_CLASS_PREFIXES)


async def call_with_backoff(coro_factory, *, retries: int = 6, base_delay: float = 0.6, cap: float = 8.0):
    """
    Повторяем запрос при RESOURCE_EXHAUSTED с экспоненциальной паузой + джиттером.
    coro_factory: функция без аргументов, возвращает awaitable.
    """
    for attempt in range(retries + 1):
        try:
            return await coro_factory()
        except Exception as e:
            msg = str(e)
            if "RESOURCE_EXHAUSTED" not in msg and "Too Many Requests" not in msg:
                raise
            if attempt == retries:
                raise
            # backoff
            delay = min(cap, base_delay * (2 ** attempt))
            delay *= random.uniform(0.85, 1.25)
            await asyncio.sleep(delay)


async def fetch_daily_ohlcv_one_call(
    client: AsyncClient,
    figi: str,
    candles_limiter: RateLimiter,
    days: int = DAYS_BACK,
) -> List[Tuple[float, float, float, float, float]]:
    """
    Ключевая оптимизация: вместо get_all_candles (много запросов)
    делаем один get_candles на период.
    """
    from_ = now() - timedelta(days=days)
    to_ = now()

    async def _do():
        await candles_limiter.acquire()
        resp = await client.market_data.get_candles(
            figi=figi,
            from_=from_,
            to=to_,
            interval=CandleInterval.CANDLE_INTERVAL_DAY,
        )
        rows = []
        for c in resp.candles:
            rows.append(
                (
                    q_to_float(c.open),
                    q_to_float(c.high),
                    q_to_float(c.low),
                    q_to_float(c.close),
                    float(c.volume),
                )
            )
        return rows

    return await call_with_backoff(_do)


async def fetch_spread(
    client: AsyncClient,
    figi: str,
    fallback_price: float,
    orderbook_limiter: RateLimiter,
) -> Optional[Tuple[float, float]]:
    """
    returns: (mid, spread_pct)
    """
    async def _do():
        await orderbook_limiter.acquire()
        return await client.market_data.get_order_book(figi=figi, depth=ORDERBOOK_DEPTH)

    try:
        ob = await call_with_backoff(_do, retries=4, base_delay=0.3, cap=3.0)
        bids = ob.bids
        asks = ob.asks
        if not bids or not asks:
            return None
        best_bid = q_to_float(bids[0].price)
        best_ask = q_to_float(asks[0].price)
        mid = (best_bid + best_ask) / 2.0 if (best_bid > 0 and best_ask > 0) else fallback_price
        if mid <= 0:
            return None
        spread_pct = 100.0 * (best_ask - best_bid) / mid
        return mid, spread_pct
    except Exception:
        return None


def recommend_grid_params(atr_pct: float, spread_pct: float) -> Tuple[float, float, float]:
    """
    Рекомендации под грид:

    1) Минимум 0.20% (как твой комфорт на IMOEXF).
    2) Шаг зависит от ATR: чем выше ATR, тем шире шаг.
    3) Шаг не должен быть близок к спреду: берём минимум ~4*spread (на практике приятнее).
    """
    # Базовая зависимость от ATR:
    # atr 1.5% -> 0.225%, atr 2.5% -> 0.375%, atr 4% -> 0.60%
    step_from_atr = atr_pct * 0.15

    # Защита от спреда
    step_from_spread = max(0.0, spread_pct * 4.0)

    # Итоговый шаг
    step = max(0.20, step_from_atr, step_from_spread)

    # Ограничим сверху, чтобы сетка не становилась слишком редкой на турбо-инструментах
    step = clamp(step, 0.20, 0.90)

    band_low = atr_pct * 0.75
    band_high = atr_pct * 1.35
    return step, band_low, band_high


def score(r: VolRow) -> float:
    """
    Ранжирование:
    - хотим волатильность (atr/range/sigma),
    - хотим маленький спред,
    - хотим ликвидность (volume).
    """
    vol_core = r.atr_pct + 0.4 * r.range_pct + 0.4 * r.sigma_pct
    liquidity_boost = log10(r.avg_vol20 + 1.0)
    spread_penalty = (r.spread_pct + 0.02)
    return (vol_core * liquidity_boost) / spread_penalty


async def build_universe(client: AsyncClient, include_futures=True, include_shares=True):
    universe = []

    if include_shares:
        shares = await client.instruments.shares(instrument_status=InstrumentStatus.INSTRUMENT_STATUS_BASE)
        for s in shares.instruments:
            if is_moex_like(s.class_code):
                if ONLY_TICKERS and s.ticker not in ONLY_TICKERS:
                    continue
                universe.append(("share", s))

    if include_futures:
        futures = await client.instruments.futures(instrument_status=InstrumentStatus.INSTRUMENT_STATUS_BASE)
        for f in futures.instruments:
            if is_moex_like(f.class_code):
                if ONLY_TICKERS and f.ticker not in ONLY_TICKERS:
                    continue
                universe.append(("future", f))

    return universe


async def main(top_n: int = TOP_N):
    candles_limiter = RateLimiter(CANDLES_QPS)
    orderbook_limiter = RateLimiter(ORDERBOOK_QPS)

    async with AsyncClient(TOKEN) as client:
        universe = await build_universe(client, include_futures=True, include_shares=True)

        rows: List[VolRow] = []
        sem = asyncio.Semaphore(MAX_CONCURRENCY)

        async def process(inst_type: str, inst):
            async with sem:
                try:
                    ohlcv = await fetch_daily_ohlcv_one_call(client, inst.figi, candles_limiter, days=DAYS_BACK)
                    metrics = compute_metrics(ohlcv)
                    if not metrics:
                        return

                    last_close, atr_pct, range_pct, sigma_pct = metrics
                    if atr_pct < MIN_ATR_PCT:
                        return

                    avgv = avg_volume_20(ohlcv)
                    if inst_type == "share" and avgv < MIN_AVG_VOL_SHARES:
                        return
                    if inst_type == "future" and avgv < MIN_AVG_VOL_FUTURES:
                        return

                    spread_info = await fetch_spread(client, inst.figi, last_close, orderbook_limiter)
                    if not spread_info:
                        return
                    mid, spread_pct = spread_info
                    if spread_pct > MAX_SPREAD_PCT:
                        return

                    grid_step_pct, band_low, band_high = recommend_grid_params(atr_pct, spread_pct)

                    rows.append(
                        VolRow(
                            inst_type=inst_type,
                            ticker=inst.ticker,
                            name=getattr(inst, "name", inst.ticker),
                            class_code=inst.class_code,
                            figi=inst.figi,
                            last_close=last_close,
                            atr_pct=atr_pct,
                            range_pct=range_pct,
                            sigma_pct=sigma_pct,
                            avg_vol20=avgv,
                            mid=mid,
                            spread_pct=spread_pct,
                            grid_step_pct=grid_step_pct,
                            atr_band_low=band_low,
                            atr_band_high=band_high,
                            candles_used=len(ohlcv),
                        )
                    )
                except Exception as e:
                    # Не спамим логом: один инструмент упал — идём дальше
                    return

        await asyncio.gather(*(process(t, i) for t, i in universe))

        rows.sort(key=score, reverse=True)

        print("\nTOP candidates for GridBot (MOEX): volatility + liquidity + tight spread")
        print("-----------------------------------------------------------------------")
        for i, r in enumerate(rows[:top_n], 1):
            vol_line = f"ATR%={r.atr_pct:>5.2f} Range%={r.range_pct:>5.2f} σ%={r.sigma_pct:>5.2f}"
            liq_line = f"avgVol20={r.avg_vol20:,.0f} spread%={r.spread_pct:>5.3f} mid≈{r.mid:.2f}"
            bot_line = f"grid_step≈{r.grid_step_pct:.2f}%  atr_band≈[{r.atr_band_low:.2f}..{r.atr_band_high:.2f}]"
            print(f"{i:>2}. {r.ticker:<10} {r.class_code:<6} {r.inst_type:<6} | {vol_line} | {liq_line} | {bot_line} | {r.name}")

        if not rows:
            print("\n(Ничего не прошло фильтры. Попробуй ослабить MAX_SPREAD_PCT или MIN_AVG_VOL_*.)")


if __name__ == "__main__":
    asyncio.run(main())