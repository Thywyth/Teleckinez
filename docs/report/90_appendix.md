# ДОДАТКИ

## Додаток А. Лістинги ключових модулів

Повний код наведено в репозиторії (додаток В). Нижче — фрагменти, що
реалізують контроль виконання команд і розбір фраз.

::: {custom-style="TableCaption"}
Лістинг А.1 – Таблиця переходів і перехід автомата станів команди (`src/teleckinez/dispatch/tracker.py`)
:::

```python
TRANSITIONS: dict[S, frozenset[S]] = {
    S.CREATED: frozenset({S.PUBLISHED, S.REJECTED_LOCAL, S.DEVICE_OFFLINE}),
    S.PUBLISHED: frozenset(
        {
            S.ACCEPTED,
            S.REJECTED,
            S.RETRYING,
            S.TIMEOUT_ACK,
            S.COMPLETED,  # state converged before the ack arrived
            S.DEVICE_OFFLINE,
            S.SUPERSEDED,
        }
    ),
    S.RETRYING: frozenset(
        {
            S.PUBLISHED,  # next attempt, same cmd_id
            S.ACCEPTED,  # late ack of the previous attempt
            S.REJECTED,
            S.COMPLETED,
            S.TIMEOUT_ACK,  # the last attempt could not even be published
            S.DEVICE_OFFLINE,
            S.SUPERSEDED,
        }
    ),
    S.ACCEPTED: frozenset({S.COMPLETED, S.TIMEOUT_STATE, S.DEVICE_OFFLINE, S.SUPERSEDED}),
}
TERMINAL = frozenset(s for s in S if s not in TRANSITIONS)


    def transition(self, record: CommandRecord, new: S, error: str | None = None) -> None:
        if not self.can(record, new):
            raise InvalidTransition(f"{record.cmd_id}: {record.status.value} -> {new.value}")
        now = self.clock()
        old = record.status
        record.status = new
        record.history.append((new, now))
        if error is not None:
            record.error = error
        if new is S.PUBLISHED:
            record.attempts += 1
            record.t_published = record.t_published or now
        elif new in (S.ACCEPTED, S.REJECTED) and record.t_ack is None:
            record.t_ack = now
        self.bus.publish(
            CommandStatusChanged(
                ts=now,
                session_id=record.session_id,
                cmd_id=record.cmd_id,
                device_id=record.device_id,
                old=old,
                new=new,
                attempts=record.attempts,
                error=record.error,
            )
        )
        if new in TERMINAL:
            record.t_finished = now
            active = self._active.get(record.device_id, [])
            if record in active:
                active.remove(record)
            self.bus.publish(
                CommandFinished(
                    ts=now,
                    session_id=record.session_id,
                    cmd_id=record.cmd_id,
                    device_id=record.device_id,
                    status=new,
                    error=record.error,
                    group_id=record.group_id,
                )
            )
```

::: {custom-style="TableCaption"}
Лістинг А.2 – Нагляд за командою і обробка подій пристрою в диспетчері (`src/teleckinez/dispatch/dispatcher.py`)
:::

```python
    async def _supervise(self, record: CommandRecord) -> None:
        cfg = self.settings
        if record.status is not S.CREATED:
            return
        for attempt in range(1, cfg.max_attempts + 1):
            if attempt > 1:
                self._move(record, S.RETRYING)
                await self._wait_while(record, (S.RETRYING,), cfg.backoff(attempt))
                if record.status is not S.RETRYING:
                    break  # a late ack/state arrived during the pause, or offline/superseded
            self._move(record, S.PUBLISHED)
            try:
                await self.sender.send(
                    record.device_id, record.cmd_id, record.desired, cfg.cmd_ttl_s
                )
            except MqttNotConnected as exc:
                log.warning("%s: publish failed: %s", record.cmd_id, exc)
                record.error = "broker_unavailable"
            else:
                record.error = None
                await self._wait_while(record, WAITING_FOR_ACK, cfg.ack_timeout_s)
            if record.status is not S.PUBLISHED:
                break
        else:
            if record.status is S.PUBLISHED:
                self._move(record, S.TIMEOUT_ACK, record.error or "no_ack")
                return

        if record.status is S.ACCEPTED:
            await self._wait_while(record, (S.ACCEPTED,), record.state_timeout_s or 3.0)
            if record.status is S.ACCEPTED:
                self._move(record, S.TIMEOUT_STATE, "state_not_reached")

    async def _wait_while(
        self, record: CommandRecord, statuses: tuple[S, ...], wait_s: float
    ) -> None:
        """Sleep up to wait_s, waking early when the record leaves `statuses`."""
        changed = self._changed.get(record.cmd_id)
        if changed is None:  # already terminal
            return
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait_s
        while record.status in statuses:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return
            changed.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(changed.wait(), remaining)

    # ----------------------------------------------------------- event inputs

    def _on_ack(self, ack: AckReport) -> None:
        record = self.tracker.get(ack.cmd_id)
        if record is None or record.is_terminal or record.device_id != ack.device_id:
            return  # unknown, late (after a final state) or forged ack
        if ack.status == "rejected":
            if record.status in (S.PUBLISHED, S.RETRYING):
                self._move(record, S.REJECTED, ack.error or "rejected")
            return
        if record.status in (S.PUBLISHED, S.RETRYING):
            self._move(record, S.ACCEPTED)
        self._check_converged(record)

    def _on_state(self, event: DeviceStateChanged) -> None:
        for record in self.tracker.active(event.device_id):
            self._check_converged(record)

    def _on_availability(self, event: DeviceAvailabilityChanged) -> None:
        if event.availability is not Availability.OFFLINE:
            return
        for record in self.tracker.active(event.device_id):
            if record.status in RUNNING:
                self._move(record, S.DEVICE_OFFLINE, "device_offline")

    def _check_converged(self, record: CommandRecord) -> None:
        if record.status not in RUNNING or not record.desired:
            return
        device = self.registry.get(record.device_id)
        if device.type.converged(record.desired, self.twins.get(record.device_id).reported):
            self._move(record, S.COMPLETED)
```

::: {custom-style="TableCaption"}
Лістинг А.3 – Пошук фраз словника з урахуванням відмінків і нечіткого збігу (`src/teleckinez/nlu/rule_parser.py`)
:::

```python
class PhraseIndex:
    def __init__(self) -> None:
        self.exact: dict[tuple[str, ...], list[Entry]] = defaultdict(list)
        self.by_len: dict[int, list[str]] = defaultdict(list)  # fuzzy candidates
        self._fuzzy_ok: set[tuple[str, ...]] = set()

    def add(self, phrase: str, entry: Entry, fuzzy: bool = True) -> None:
        key = tuple(tokenize(phrase))
        if not key:
            return
        if entry not in self.exact[key]:
            self.exact[key].append(entry)
        if fuzzy and key not in self._fuzzy_ok and len(" ".join(key)) >= FUZZY_MIN_LEN:
            self._fuzzy_ok.add(key)
            self.by_len[len(key)].append(" ".join(key))

    def lookup(self, tokens: list[str], i: int, threshold: float) -> Match | None:
        # 1. exact, longest first
        for n in range(min(MAX_PHRASE, len(tokens) - i), 0, -1):
            key = tuple(tokens[i : i + n])
            if key in self.exact:
                return Match(i, i + n, " ".join(key), self.exact[key], 1.0)
        # 2. stem (single words, Ukrainian case endings) and 3. fuzzy
        best: Match | None = None
        for n in range(min(MAX_PHRASE, len(tokens) - i), 0, -1):
            text = " ".join(tokens[i : i + n])
            if len(text) < FUZZY_MIN_LEN or any(
                t.isdigit() or t in "%°" for t in tokens[i : i + n]
            ):
                continue
            if n == 1:
                stem = self._stem_match(text)
                if stem and (best is None or best.score < STEM_SCORE):
                    best = Match(i, i + 1, stem, self.exact[(stem,)], STEM_SCORE)
            found = process.extractOne(
                text, self.by_len.get(n, []), scorer=fuzz.ratio, score_cutoff=threshold
            )
            if found and (best is None or found[1] / 100 > best.score):
                key = tuple(found[0].split())
                best = Match(i, i + n, found[0], self.exact[key], found[1] / 100)
        return best

    def _stem_match(self, word: str) -> str | None:
        """'кухні' ~ 'кухня', 'спальню' ~ 'спальня': long common prefix, similar length."""
        best, best_prefix = None, 0
        for cand in self.by_len.get(1, []):
            if abs(len(cand) - len(word)) > 3:
                continue
            prefix = 0
            for a, b in zip(cand, word, strict=False):
                if a != b:
                    break
                prefix += 1
            needed = max(4, len(cand) - 2, len(word) - 3)
            if prefix >= needed and prefix > best_prefix:
                best, best_prefix = cand, prefix
        return best
```

## Додаток Б. Фрагмент конфігурації пристроїв

::: {custom-style="TableCaption"}
Лістинг Б.1 – Фрагмент файлу `config/devices.yaml`
:::

```yaml
rooms:
  living_room:
    uk: [вітальня, вітальні, вітальню, вітальнею, зал, залі, залу]
    en: [living room, lounge]
    loc: {uk: у вітальні, en: in the living room}
  bedroom:
    uk: [спальня, спальні, спальню, спальнею]
    en: [bedroom]
    loc: {uk: у спальні, en: in the bedroom}
  kitchen:
    uk: [кухня, кухні, кухню, кухнею]
    en: [kitchen]
    loc: {uk: на кухні, en: in the kitchen}
  # ...

groups:
  all_lights:
    uk: [все світло, усе світло, всі світильники, усі світильники, всі лампи, усі лампи]
    en: [all lights, all the lights, every light]
  # ...

devices:
  - id: curtains.living_room
    type: curtains
    room: living_room
    names: {uk: [штори, штору, фіранки, жалюзі], en: [curtains, blinds]}
    title: {uk: Штори у вітальні, en: Living room curtains}
    plural: true
    groups: [all_curtains]
    sim: {speed: 25}
  - id: light.kitchen_main
    type: light
    room: kitchen
    names: {uk: [світло, люстра, люстру, лампа, лампу], en: [light, lights, lamp]}
    title: {uk: Світло на кухні, en: Kitchen light}
    groups: [all_lights]
  - id: fan.kitchen_hood
    type: fan
    room: kitchen
    names: {uk: [витяжка, витяжку, вентилятор], en: [hood, extractor, fan]}
    title: {uk: Витяжка на кухні, en: Kitchen hood}
  - id: lock.front_door
    type: door_lock
    room: hallway
    names: {uk: [замок, вхідні двері, двері, вхідний замок], en: [lock, front door, door]}
    title: {uk: Замок вхідних дверей, en: Front door lock}
    critical: true
  # ... (усього 31 пристрій у 8 кімнатах)
```

## Додаток В. Репозиторій проєкту

Вихідний код прототипу, специфікації, вихідні файли діаграм, набори даних
для експериментів, скрипти вимірювань та їхні результати розміщено в
репозиторії GitHub:

https://github.com/Thywyth/Teleckinez

Інструкцію зі встановлення і запуску наведено у файлі `README.md`
репозиторію.
