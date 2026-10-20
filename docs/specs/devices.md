# Специфікація: модель пристроїв, конфігурація, симулятор

## 1. Модель

- **Room** — кімната: `id`, назви (uk/en, з відмінками/синонімами).
- **Device** — `id`, `type`, `room`, `names` (uk/en синоніми), `groups`
  (напр. `all_lights`), `protocol` (`native` за замовчуванням), `critical`
  (bool — для дверей/воріт, впливає на відповідь і, опційно, на права).
- **Capability** — атрибут стану з типом і межами (`on: bool`,
  `brightness: int 0..100`, `position: int 0..100`, `target_temp: float 16..30` …).
- **DeviceType** — набір capabilities + підтримувані дії + правило
  «чи досягнуто бажаного стану» (`converged(desired, reported)`), напр. для
  термостата перевіряється `target_temp`, а не поточна температура.

## 2. Каталог типів для симуляції (≈ 25–30 пристроїв, 8 кімнат)

| Тип | Capabilities | Дії голосом (приклади) | Фізика в симуляторі |
|---|---|---|---|
| `light` | on, brightness | увімкни/вимкни, яскравість 40 % | миттєво |
| `rgb_light` | on, brightness, color | зроби світло синім | миттєво |
| `curtains` | position, moving | відкрий/закрий/на половину | 25 %/с, проміжні state |
| `thermostat` | target_temp, current_temp, heating | встанови 22 градуси | current_temp повільно тягнеться до target |
| `air_conditioner` | on, mode, target_temp | увімкни кондиціонер на охолодження | — |
| `socket` | on, power_w | вимкни розетку | power_w ≈ 0 коли off |
| `kettle` | on, water_temp | закип'яти чайник | нагрів ~60 с, авто-off при 100° |
| `boiler` | on, water_temp | увімкни бойлер | повільний нагрів |
| `fan` | on, speed 1..3 | вентилятор на третю швидкість | — |
| `door_lock` | locked | зачини/відчини замок | 1 с, critical |
| `garage_gate` | position, moving | відкрий гараж | 8 с, critical |
| `robot_vacuum` | status(docked/cleaning/returning), battery | запусти пилосос / на базу | розряд батареї |
| `tv` | on, volume | гучніше / вимкни телевізор | — |
| `humidifier` | on, level | — | — |
| сенсори (лише читання) | temperature, humidity, motion, door_contact | яка температура у спальні? | шум, випадкові події |

Сенсори потрібні для запитів стану («чи зачинені двері?») — це частина
«контролю станів», хоча основний фокус — виконавчі пристрої.

## 3. `config/devices.yaml`

```yaml
language_default: uk
rooms:
  living_room: {uk: [вітальня, вітальні, вітальню, зал, залі], en: [living room, lounge]}
  bedroom:     {uk: [спальня, спальні, спальню],              en: [bedroom]}
  kitchen:     {uk: [кухня, кухні, кухню],                    en: [kitchen]}
  # bathroom, hallway, kids_room, garage, yard

devices:
  - id: curtains.living_room
    type: curtains
    room: living_room
    names: {uk: [штори, штору, фіранки, жалюзі], en: [curtains, blinds]}
  - id: light.kitchen_main
    type: light
    room: kitchen
    names: {uk: [світло, люстра, лампа], en: [light, lights, lamp]}
    groups: [all_lights]
  - id: lock.front_door
    type: door_lock
    room: hallway
    names: {uk: [замок, вхідні двері, двері], en: [lock, front door, door]}
    critical: true
```

Додаткові (необов'язкові) поля — уточнення етапу 1, потрібні для природних
відповідей і для NLU:

```yaml
rooms:
  kitchen:
    uk: [кухня, кухні, кухню]
    en: [kitchen]
    loc: {uk: на кухні, en: in the kitchen}   # місцевий відмінок для відповідей
groups:                                       # назви груп для фраз «все світло»
  all_lights: {uk: [все світло, усе світло, всі лампи], en: [all lights, all the lights]}
devices:
  - id: curtains.living_room
    ...
    title: {uk: Штори у вітальні, en: Living room curtains}  # за замовч.: перша назва + loc
    plural: true          # граматичне число для «штори не відповідають»
    heartbeat_s: 10       # період heartbeat (за замовч. з settings)
```

Вимога автора: одну дію можна викликати **кількома фразами**, напр.
«штори вітальня», «відкрий штори у вітальні» і «living room curtains» —
усе це той самий пристрій. Синоніми задаються в YAML, словник дій — у
`nlu/lexicon.py` (див. nlu.md). Уточнювальних діалогів немає: якщо пристрій
не визначено однозначно — асистент каже, що не зрозумів, і перелічує
варіанти («Знайшов штори у вітальні та спальні — назвіть кімнату»).

Валідація конфігу — pydantic: унікальні id, кімнати існують, синоніми не
конфліктують між пристроями однієї кімнати (інакше помилка при старті).
Також: групи пристроїв оголошені в `groups`, назви нормалізуються (нижній
регістр, апострофи ’ ʼ → '), тип пристрою існує в каталозі (перевіряє
`DeviceRegistry`, бо каталог типів живе в `devices/`).

## 4. Симулятор (`simulator/`)

Окремий процес: `python -m simulator --config config/devices.yaml`.

- На кожен пристрій — об'єкт `SimDevice` (asyncio task) з власним MQTT-
  з'єднанням **або** спільним з'єднанням + окремим LWT-імітатором. Рішення:
  окреме з'єднання на пристрій (≈30 з'єднань — нормально для Mosquitto),
  бо тоді LWT працює чесно, як у реального пристрою.
- Публікує `meta`, `availability=online` (LWT = offline), періодичний
  `state` (heartbeat), обробляє `cmd`, відповідає `ack`.
- Моделює затримку мережі/обробки (`latency_ms` з розподілом, напр. 20–80 мс)
  і фізику (див. таблицю).
- Ідемпотентність за `cmd_id`, відкидання команд з простроченим `ttl`.

### Ін'єкція збоїв (`simulator/faults.py`)

Керується з веб-панелі (топік `tkz/sim/faults`) або CLI. Потрібно для
демонстрації і для експериментів надійності (metrics.md).

| Збій | Поведінка | Що має виявити асистент |
|---|---|---|
| `offline` | розірвати з'єднання без disconnect → брокер шле LWT | DEVICE_OFFLINE одразу, відмова в команді |
| `freeze` | з'єднання є, але пристрій мовчить (ні ack, ні heartbeat) | TIMEOUT_ACK, потім stale-статус |
| `drop_rate: p` | губити частку команд | retry рятує; статистика |
| `stuck` | ack приходить, але стан не змінюється | TIMEOUT_STATE (не виконано) |
| `slow: ms` | велика затримка | спрацьовує/не спрацьовує таймаут залежно від налаштувань |
| `reject` | ack `rejected` | REJECTED, пояснення голосом |
| `drift` | стан змінюється сам (хтось вручну вимкнув світло) | DeviceStateChanged без команди → оновлення двійника, подія в журналі |

Сценарії збоїв можна задати файлом (`benchmarks/fault_scenarios.yaml`) для
відтворюваних експериментів.
