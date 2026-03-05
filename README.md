# T-investment-MOEX-grid-radar
Скрипт на Python, который через официальный T-Invest API (invest-python / tinkoff-investments) берёт свечи по инструментам MOEX, считает ATR%, дневной диапазон %, σ (стд. отклонение доходностей) и выводит TOP-N подходящих инструментов. 

Опирается на https://developer.tbank.ru/invest/api

Порядок установки и запуска на Windows 10:
1) Должен быть установлен Python не ниже 3.11
2) Создаем, переходим в рабочую папку, например cd C:\gridbot\tools
3) Создаем виртуально окружение python -m venv .venv
4) Активируем .\.venv\Scripts\Activate.ps1
5) Выполняем команды по порядку:
6) python -m pip install --upgrade pip
7) python -m pip install --upgrade pip setuptools wheel
8) pip install cachetools grpcio protobuf python-dateutil deprecation
9) pip install --no-deps "tinkoff-investments @ git+https://github.com/Tinkoff/invest-python.git"
10) pip install python-dotenv
11) проверяем: python -c "from tinkoff.invest import Client; print('OK')" Должно написать "ОК"
12) прописываем токен API Т-инвестиций $env:INVEST_TOKEN="ваш токен"
13) проверяем: echo $env:INVEST_TOKEN
14) Кладем файл: moex_grid_radar.py в рабочую папку
15) запускаем: python moex_grid_radar.py

Скрипт имеет встроенное органичение по запросам к API Т-инвестиций, чтобы не ловить блок по количеству одновременных запросов.
Отработка может занять некоторое время - до нескольких минут.

В итоге вы получите вывод по типу приведенного: 
TOP candidates for GridBot (MOEX): volatility + liquidity + tight spread
-----------------------------------------------------------------------
 1. FFH6       SPBFUT future | ATR%= 8.58 Range%= 8.80 σ%= 8.87 | avgVol20=206,710 spread%=0.009 mid≈56.08 | grid_step≈0.90%  atr_band≈[6.44..11.59] | TTF-3.26 Природный газ Датч ТТФ
 2. SVH6       SPBFUT future | ATR%= 5.65 Range%= 4.77 σ%= 4.65 | avgVol20=1,021,416 spread%=0.012 mid≈84.16 | grid_step≈0.85%  atr_band≈[4.24..7.62] | SILV-3.26 Серебро
 3. EHH6       SPBFUT future | ATR%= 5.25 Range%= 4.87 σ%= 3.95 | avgVol20=64,662 spread%=0.005 mid≈2113.05 | grid_step≈0.79%  atr_band≈[3.94..7.08] | ETH-3.26 Эфириум
 4. BTH6       SPBFUT future | ATR%= 3.87 Range%= 3.55 σ%= 3.13 | avgVol20=90,683 spread%=0.001 mid≈72500.50 | grid_step≈0.58%  atr_band≈[2.90..5.22] | BTC-3.26 Биткоин
 5. SVM6       SPBFUT future | ATR%= 5.41 Range%= 4.53 σ%= 4.59 | avgVol20=47,921 spread%=0.012 mid≈86.66 | grid_step≈0.81%  atr_band≈[4.06..7.30] | SILV-6.26 Серебро

В выводе:
Тикер инструмента;

SPBFUT = фьючерсы на срочном рынке MOEX;

Тип: share (акция) или future (фьючерс);

ATR%
Average True Range, усреднённая “амплитуда движения” за день (у нас ATR(14) по дневным свечам).
В процентах от цены:
ATR 1–2%: спокойный инструмент
2–4%: бодрый
4–7%: очень волатильный
8%+: уже “аттракцион” 🎢

Range%
Средний дневной диапазон в среднем за последние ~20 дней.
Если Range% заметно выше ATR%, значит бывают дни с большими хвостами.

σ% (сигма)
Стандартное отклонение дневных доходностей (close-to-close), тоже примерно за 20 дней.

Как читать вместе:
ATR% = внутри дня
Range% = размах дня (high-low)
σ% = насколько “скачут” закрытия (close-close)
Если все три высокие, инструмент реально качает и внутри дня, и по закрытиям.

avgVol20
Средний дневной объём за последние ~20 дневных свечей.

spread%
Спред в процентах;

mid≈
Средняя цена между bid/ask (условная “рыночная” в момент запроса стакана);


Далее, так как я в разработке грид-бота, идут Рекомендации под GridBot:

grid_step≈…%
Рекомендованный минимальный шаг сетки (в процентах).
Скрипт предлагает шаг так, чтобы:
он был не меньше 0.20% (иначе часто комиссия съедает хорошую часть, даже при трейдерских тарифах), рос с ATR (волатильнее инструмент → шире шаг), и не был “слишком мелким относительно спреда”.

atr_band≈[low..high]
“нормальная зона ATR%” для режима входов/перестроения.

Имя инструмента
человекочитаемое название.
