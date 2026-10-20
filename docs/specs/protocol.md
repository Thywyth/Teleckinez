# Специфікація: протокол обміну з пристроями (MQTT)

## 1. Брокер

- Mosquitto 2.x у Docker (`docker-compose.yml`, порт 1883; 9001 — websockets,
  опційно). Конфіг — `config/mosquitto/mosquitto.conf`.
- Fallback без Docker: вбудований брокер `amqtt` (`python -m teleckinez.broker`,
  extra `broker`). Перевірено на Python 3.13 / amqtt 0.12.1: QoS 1, retained,
  Last Will працюють (ті самі тести `tests/test_broker.py`, що й для Mosquitto).
- Клієнти на Windows працюють у `SelectorEventLoop` (aiomqtt/paho потребують
  `add_reader`, якого немає в `ProactorEventLoop` за замовчуванням).
- Безпека (для записки, у демо — опційно): логін/пароль на клієнта, ACL
  (пристрій пише лише у свої топіки), TLS на 8883.

## 2. Простір топіків (адаптер `native`)

Префікс `tkz/` (налаштовується). `{id}` — ідентифікатор пристрою, наприклад
`light.kitchen_main`, `curtains.living_room`.

| Топік | Напрям | QoS | Retain | Вміст |
|---|---|---|---|---|
| `tkz/dev/{id}/meta` | пристрій → | 1 | так | опис можливостей (discovery) |
| `tkz/dev/{id}/availability` | пристрій → | 1 | так | `online` / `offline` (offline — через **LWT**) |
| `tkz/dev/{id}/state` | пристрій → | 1 | так | повний фактичний стан (reported) |
| `tkz/dev/{id}/cmd` | → пристрій | 1 | ні | команда |
| `tkz/dev/{id}/ack` | пристрій → | 1 | ні | підтвердження прийому/відмови |
| `tkz/sim/faults` | панель → симулятор | 1 | ні | керування ін'єкцією збоїв (лише симулятор) |
| `tkz/assistant/availability` | асистент → | 1 | так | LWT самого асистента |

## 3. Формати повідомлень (JSON, UTF-8)

### meta
```json
{"id": "curtains.living_room", "type": "curtains", "fw": "tkz-sim/1.0",
 "capabilities": {"position": {"type": "int", "min": 0, "max": 100, "unit": "%"},
                  "moving": {"type": "bool", "readonly": true}},
 "heartbeat_s": 10}
```

### state
```json
{"position": 40, "moving": true, "ts": 1761000000.123, "seq": 182}
```
- Завжди **повний** стан (не дельта) — простіше звіряти з desired.
- `seq` — монотонний лічильник, щоб відкидати застарілі повідомлення.
- Пристрій публікує state: після кожної зміни + періодично кожні `heartbeat_s`
  (це і є heartbeat).

### cmd
```json
{"cmd_id": "c-01J9Z…", "action": "set", "params": {"position": 100},
 "ts": 1761000000.050, "ttl_s": 5}
```
- `action`: `set` (встановити атрибути), `toggle`, `start`, `stop`, `step`
  (відносна зміна, напр. `{"brightness": +20}`).
- Асистент завжди надсилає `set` з **абсолютними** значеннями: бажаний стан
  обчислюється заздалегідь з дії і останнього reported (`toggle` → `{"on":
  false}`, «яскравіше» → `{"brightness": 60}`). Тоді повтор безпечний, а
  перевірка виконання — просте порівняння desired і reported. Решту дій
  (`toggle`, `step`, `start`, `stop`) симулятор підтримує для сумісності з
  протоколом і ручного тестування.
- `cmd_id` — ULID; пристрій **ідемпотентний** за `cmd_id` (повтор тієї ж
  команди не виконується вдруге, лише повторно ack-ується) → безпечні retry.
- `ttl_s` — пристрій ігнорує прострочені команди.

### ack
```json
{"cmd_id": "c-01J9Z…", "status": "accepted", "ts": 1761000000.081}
{"cmd_id": "c-01J9Z…", "status": "rejected", "error": "value_out_of_range"}
```
`status`: `accepted` | `rejected` | `done` (для миттєвих дій пристрій може
одразу відповісти `done`). Остаточне підтвердження успіху — це **state**,
що збігається з desired (див. dispatcher.md).

## 4. Адаптери під реальні прошивки (для розділу 3.9 і як P2-код)

`DeviceAdapter` перетворює внутрішню `DeviceCommand` на конкретні топіки/
payload і навпаки нормалізує стан:

| Адаптер | Команда | Стан | Доступність |
|---|---|---|---|
| `native` | `tkz/dev/{id}/cmd` | `tkz/dev/{id}/state` | `tkz/dev/{id}/availability` (LWT) |
| `tasmota` | `cmnd/{topic}/POWER` = `ON` | `stat/{topic}/RESULT`, `tele/{topic}/STATE` | `tele/{topic}/LWT` = `Online`/`Offline` |
| `zigbee2mqtt` | `zigbee2mqtt/{name}/set` = `{"state":"ON"}` | `zigbee2mqtt/{name}` | `zigbee2mqtt/{name}/availability` |
| `esphome` | через MQTT-компонент ESPHome: `{prefix}/{component}/{name}/command` | `{prefix}/{component}/{name}/state` | `{prefix}/status` |

У прототипі реалізується `native` (P0); `tasmota` і `zigbee2mqtt` — P2,
перевіряються тим, що симулятор вміє «прикидатися» таким пристроєм
(`protocol: tasmota` у devices.yaml). Точні формати звірити з офіційною
документацією Tasmota / Zigbee2MQTT / ESPHome перед реалізацією і послатися
на неї у записці.
