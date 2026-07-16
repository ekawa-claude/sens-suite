# Sens Suite

*[English version below](#sens-suite-english)*

Два инструмента для мышки в одном наборе (Windows):

- **Sens Finder** — aim-тренажёр, который *подбирает* тебе сенсу для Marvel Rivals:
  слепые прогоны на разных cm/360, статистика по фликам и трекингу, A/B-дуэли,
  бенчмарк с прогрессом.
- **RawAccel Studio** — удобный интерфейс для драйвера
  [Raw Accel](https://github.com/a1xd/rawaccel): кривые ускорения с живым графиком,
  профили, трей, автозапуск. Умеет строить кривую из результатов Sens Finder
  (если твой оптимум для фликов и трекинга различается).

## Установка

1. Скачай `SensSuite-*-win64.zip` из [Releases](../../releases), распакуй куда угодно.
2. Запусти `SensSuite.exe` (Studio + трей). Только тренажёр — `SensFinder.exe`.
3. Windows SmartScreen при первом запуске скажет «Защитил ваш ПК» — это норма для
   неподписанных программ: **Подробнее → Выполнить в любом случае**.

### Raw Accel (нужен только для Studio)

Studio управляет драйвером Raw Accel, поставь его один раз:
[скачай отсюда](https://github.com/a1xd/rawaccel/releases), запусти `installer.exe`,
перезагрузись. При первом запуске Studio укажи папку, куда распакован Raw Accel.
Sens Finder работает и без драйвера.

## Первый запуск Sens Finder

1. В настройках укажи **свой DPI** — от него зависят все расчёты.
2. (Опционально, для точного пересчёта в игровую сенсу) измерь **cm/360 при sens 1.0**:
   в игре поставь чувствительность 1.0, плавно проведи мышью ровно один полный
   оборот (360°) и замерь линейкой, сколько сантиметров прошла мышь. Впиши число
   в «cm/360 at sens 1.0». Без этого вердикты остаются верными в cm/360.
3. Дальше — «Find my sens» и слушай статистику, а не ощущения.

## Данные

Конфиги, история и профили живут в `%LOCALAPPDATA%\SensSuite` — обновление
(замена папки программы) ничего не трогает. Portable-режим: создай папку `data`
рядом с `SensSuite.exe`, и всё будет храниться в ней.

## Сборка из исходников

Нужен Python 3.12+ и зависимости: `pip install -r requirements.txt`.
Запуск в dev-режиме — `SensSuite-dev.bat`, сборка exe — `build\build.bat`
(результат в `dist\SensSuite\`).

---

<a id="sens-suite-english"></a>
## Sens Suite (English)

*[Русская версия выше](#sens-suite)*

Two mouse tools in one bundle (Windows):

- **Sens Finder** — an aim-training benchmark that *finds* your best sensitivity
  for Marvel Rivals: blind runs at different cm/360, flick and tracking stats,
  A/B duels, a progress-tracked benchmark.
- **RawAccel Studio** — a friendly UI for the
  [Raw Accel](https://github.com/a1xd/rawaccel) driver: acceleration curves with
  a live graph, profiles, tray icon, autostart. Can build a curve straight from
  your Sens Finder results (if your flick and tracking optimum differ).

### Installation

1. Download `SensSuite-*-win64.zip` from [Releases](../../releases) and unzip it anywhere.
2. Run `SensSuite.exe` (Studio + tray). For just the trainer, run `SensFinder.exe`.
3. Windows SmartScreen will show "Windows protected your PC" on first launch —
   that's expected for unsigned apps: **More info → Run anyway**.

#### Raw Accel (only needed for Studio)

Studio controls the Raw Accel driver, so install it once:
[download here](https://github.com/a1xd/rawaccel/releases), run `installer.exe`,
reboot. On first launch, point Studio to the folder where Raw Accel was unpacked.
Sens Finder works fine without the driver.

### First run of Sens Finder

1. In settings, set **your DPI** — every calculation depends on it.
2. (Optional, for accurate conversion to in-game sensitivity) measure **cm/360
   at sens 1.0**: in-game, set sensitivity to 1.0, smoothly turn your mouse
   exactly one full rotation (360°) and measure how many centimeters the mouse
   traveled with a ruler. Enter that number as "cm/360 at sens 1.0". Without it,
   verdicts stay correct in cm/360 terms.
3. Then hit "Find my sens" and trust the stats, not the feel.

### Data

Configs, history, and profiles live in `%LOCALAPPDATA%\SensSuite` — updating
(replacing the program folder) doesn't touch them. Portable mode: create a
`data` folder next to `SensSuite.exe` and everything will be stored there instead.

### Building from source

Needs Python 3.12+ and dependencies: `pip install -r requirements.txt`.
Dev-mode run — `SensSuite-dev.bat`, exe build — `build\build.bat`
(output in `dist\SensSuite\`).
