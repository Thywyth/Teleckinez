# Специфікація: голосовий конвеєр

Повністю локально. Ввід — мікрофон ноутбука, вивід — динамік ноутбука.

## 1. Потік

```
AudioInput (16 kHz, mono, int16, блоки 30 мс)
   → Trigger (WakeWord | PushToTalk)
   → UtteranceRecorder (+ VAD: старт/кінець фрази, max 8 с)
   → STT → TranscriptReady
                                   ResponseComposer → TTS → AudioOutput
```

- `AudioInput` — `sounddevice.InputStream`, callback у потоці → `asyncio.Queue`.
- Поки асистент говорить (TTS), вхід не обробляється (half-duplex), щоб не
  розпізнавати самого себе. Короткий звуковий сигнал «слухаю» після тригера.
- Стани конвеєра: `IDLE → LISTENING → RECOGNIZING → SPEAKING → IDLE` —
  передаються у веб-панель (індикатор).

## 2. Тригери (вибір користувача в settings/веб-панелі)

| Режим | Реалізація | Примітки |
|---|---|---|
| `wake_word: vosk_kws` (за замовч.) | Vosk з обмеженою граматикою `["телекінез", "[unk]"]` | власне українське слово без тренування; поріг за confidence |
| `wake_word: openwakeword` | openWakeWord (ONNX), модель `hey_jarvis` | англ. слово; на Windows лише `inference_framework="onnx"` |
| `push_to_talk` | гаряча клавіша (бібліотека `keyboard` або `pynput`) або кнопка у веб-панелі | найнадійніше для захисту в шумній аудиторії |

Обидва варіанти wake word порівнюються в бенчмарку (false accept / false reject).

## 3. VAD

`webrtcvad-wheels` (агресивність 2): початок фрази — 3 мовні кадри поспіль,
кінець — 700 мс тиші (налаштовується), жорсткий ліміт 8 с. Альтернатива —
Silero VAD (вже є у faster-whisper) — розглянути, якщо webrtcvad погано
працює з шумом.

## 4. STT (інтерфейс `SpeechRecognizer.transcribe(audio, lang) -> Transcript`)

| Рушій | Моделі | Режими | Очікування |
|---|---|---|---|
| **Vosk** | `vosk-model-small-uk-v3-small` / `vosk-model-uk-v3`; `vosk-model-small-en-us-0.15` | вільний словник **або граматика** (фрази зібрані з devices.yaml + lexicon) | дуже швидкий, CPU; з граматикою — висока точність на командах |
| **faster-whisper** | `small` / `medium` (CTranslate2, CUDA float16; CPU int8 як fallback) | `language="uk"`/`"en"`/авто, `initial_prompt` зі словами з devices.yaml | точніший на вільній мові, повільніший |

- Мова: `uk`, `en` або `auto`. Для Vosk `auto` = два розпізнавачі паралельно
  і вибір за confidence; для Whisper — вбудоване визначення мови.
- Текстовий режим (`--text`, поле в веб-панелі) повністю оминає STT — для
  тестів і як запасний варіант на захисті.
- Точні назви/розміри моделей звірити на alphacephei.com/vosk/models та
  HuggingFace перед завантаженням (`scripts/download_models.py`).

## 5. TTS (інтерфейс `SpeechSynthesizer.speak(text, lang)`)

| Рушій | Голоси | Примітки |
|---|---|---|
| **Piper** (за замовч.) | `uk_UA-ukrainian_tts-medium`, `en_US-lessac-medium` | локальна нейромережа (ONNX), швидкий на CPU |
| **SAPI** (`pyttsx3`) | системні голоси Windows | fallback; українського голосу може не бути → тоді лише en |
| **text-only** | — | відповідь лише текстом у веб-панелі/консолі |

Користувач вибирає режим: голос + текст / лише текст. Текст відповіді
завжди йде у веб-панель.

## 6. Розпізнавання користувача (P3, опційно)

Ідея з анкети: верифікація мовця (SpeechBrain ECAPA або Resemblyzer),
профілі `admin`/`guest`; `guest` не може керувати пристроями з
`critical: true`. Реалізується лише якщо залишиться час; в іншому випадку —
описати як перспективу розвитку у висновках.
