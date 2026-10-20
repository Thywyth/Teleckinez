# CLAUDE.md — Teleckinez

Інструкції для Claude Code. Читай цей файл на початку кожної сесії, далі —
[docs/ROADMAP.md](docs/ROADMAP.md), щоб зрозуміти, на якому етапі ми зараз.

## Що це за проєкт

**Teleckinez** — курсова робота (спец. 122 «Комп'ютерні науки», кафедра
радіоелектронних і комп'ютерних систем, ЛНУ ім. Франка) на тему
**«Розробка архітектури голосового асистента для диспетчеризації та контролю
станів виконавчих пристроїв IoT»**.

Результат складається з трьох частин:
1. **Програмний прототип** (цей репозиторій): повністю локальний голосовий
   асистент для розумного будинку + симулятор IoT-пристроїв + веб-панель.
2. **Пояснювальна записка** (~35–45 с., .odt → PDF → друк), тексти пишемо в
   `docs/report/*.md`, план — [docs/REPORT_PLAN.md](docs/REPORT_PLAN.md).
3. **Презентація** для захисту.

Головна ідея роботи: асистент не просто «відправляє команду і забуває»
(fire-and-forget), а **диспетчеризує** її до потрібного пристрою і
**контролює виконання** — чекає підтвердження, звіряє фактичний стан з
бажаним, виявляє збої (пристрій офлайн, немає відповіді, команда не дала
ефекту) і повідомляє про результат голосом.

## Карта документації

| Файл | Що там |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Загальна архітектура, компоненти, потоки даних, структура репо |
| [docs/ROADMAP.md](docs/ROADMAP.md) | Етапи, задачі з чекбоксами, пріоритети, графік |
| [docs/DECISIONS.md](docs/DECISIONS.md) | Архітектурні рішення (ADR) з обґрунтуванням, джерело для розділу 2 |
| [docs/specs/protocol.md](docs/specs/protocol.md) | MQTT-контракт: топіки, формати повідомлень |
| [docs/specs/devices.md](docs/specs/devices.md) | Модель пристроїв, `devices.yaml`, синоніми, симулятор, ін'єкція збоїв |
| [docs/specs/dispatcher.md](docs/specs/dispatcher.md) | Диспетчер, життєвий цикл команди, контроль станів, health-моніторинг |
| [docs/specs/voice.md](docs/specs/voice.md) | Аудіо, wake word / push-to-talk, VAD, STT, TTS |
| [docs/specs/nlu.md](docs/specs/nlu.md) | Розбір команд: правила + fuzzy, опційно локальна LLM |
| [docs/specs/web.md](docs/specs/web.md) | Веб-панель: API, WebSocket, екрани |
| [docs/specs/metrics.md](docs/specs/metrics.md) | Бенчмарки та метрики для звіту |
| [docs/REPORT_PLAN.md](docs/REPORT_PLAN.md) | Структура записки, вимоги до оформлення, правила написання текстів |

Якщо реалізація відходить від специфікації — **спершу онови специфікацію**
(або запиши рішення в DECISIONS.md), потім код. Документація = джерело правди
і для коду, і для тексту записки.

## Стек

- Python **3.13** (якщо якась залежність не стане — переходимо на 3.12, записати в DECISIONS.md)
- asyncio скрізь; MQTT — `aiomqtt`; брокер — Mosquitto (Docker) або вбудований fallback
- Веб — FastAPI + uvicorn + WebSocket, фронтенд — чистий HTML/CSS/JS без збірки
- STT — Vosk (Kaldi) та faster-whisper (CUDA, RTX 3060 6 GB)
- Wake word — Vosk keyword spotting (власне слово) та openWakeWord; або push-to-talk
- TTS — Piper (локально, є український голос), fallback — Windows SAPI (pyttsx3)
- NLU — правила + `rapidfuzz`; опційно Ollama (локальна LLM) зі structured output
- Зберігання — YAML (конфігурація пристроїв) + SQLite (історія станів, журнал команд, метрики)
- Тести — pytest + pytest-asyncio; лінтер/форматер — ruff
- Діаграми — PlantUML (для записки, Java є) і Mermaid (для README/документації)

## Середовище розробника

- Windows 11, PowerShell/Git Bash, 16 ГБ RAM, Ryzen 7 6800H, RTX 3060 Laptop.
- Встановлено: Python 3.13, Git, Docker Desktop, Node.js, Java, LibreOffice.
- Немає: pandoc (ставимо через `pypandoc_binary`), ffmpeg, Ollama, gh CLI.
- Автор знайомий з Python/Docker/git на базовому рівні → **команди запуску
  пиши повністю, з поясненням**, нічого «очевидного» не пропускай.

## Правила роботи

1. **Мови.** Код, ідентифікатори, коментарі, commit-повідомлення — англійською.
   Документація в `docs/`, тексти записки, UI веб-панелі (за замовчуванням) і
   голосові відповіді — українською. Голосові команди — українська та англійська.
2. **Маленькі кроки.** Один пункт ROADMAP = одна логічна зміна = один коміт.
   Після виконання пункту постав `[x]` у ROADMAP.md.
3. **Коміти** — Conventional Commits: `feat(dispatcher): add ack timeout`,
   `docs(report): draft chapter 1`, `test(nlu): ...`. Не пушити без запиту автора.
4. **Перевіряй роботу.** Після змін запускай `pytest` і `ruff check`. Для
   голосових/аудіо модулів, які не можна протестувати автоматично, завжди
   залишай текстовий шлях (`--text` режим), щоб логіку можна було перевірити
   без мікрофона.
5. **Абстракції через інтерфейси.** Кожен рушій (STT, TTS, wake word, NLU,
   транспорт до пристроїв) — реалізація базового `Protocol`/ABC, вибирається
   в `config/settings.yaml`. Це ключовий архітектурний аргумент у записці.
6. **Без хмари.** Жодних зовнішніх API під час роботи асистента. Моделі
   завантажуються один раз скриптом `scripts/download_models.py` у `models/`
   (папка в .gitignore).
7. **Секрети** не комітимо (у минулій курсовій API-ключ потрапив у додаток —
   не повторюємо). Конфіг із паролями → `.env`, у репо лише `.env.example`.
8. **Метрики — справжні.** Цифри у звіті беруться лише з реальних запусків
   `benchmarks/`; результати зберігаються у `benchmarks/results/` (CSV + PNG).
   Не вигадувати і не «підправляти» числа.
9. **Тексти записки** пиши за правилами з REPORT_PLAN.md (розділ
   «Стиль і оригінальність»): власні формулювання, без води, з посиланнями на
   реальні джерела, які можна перевірити.

## Команди (заповнюємо по мірі появи)

```bash
# одноразово
py -3.13 -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev,voice,cuda,broker]"   # cuda — лише для NVIDIA GPU (faster-whisper)
python scripts/check_deps.py       # перевірка, що все імпортується
python scripts/download_models.py          # ~0.8 ГБ у models/ (--all: + великі моделі)
python scripts/download_models.py --verify # завантажити кожну модель і прогнати один раз
python scripts/audio_check.py              # мікрофон/динамік: запис 3 с, рівень, відтворення

# брокер
docker compose up -d mosquitto       # основний (Mosquitto у Docker), порт 1883
python -m teleckinez.broker          # запасний без Docker (pip install -e ".[broker]")

# запуск (у різних терміналах)
python -m simulator              # симулятор пристроїв
python -m teleckinez             # асистент + веб-панель (http://localhost:8000)
python -m teleckinez --text      # без мікрофона, команди з клавіатури

# перевірки
pytest                           # інтеграційні тести потребують брокера, без нього — skip
pytest -m "not integration"      # лише юніт-тести
ruff check . && ruff format .
```
