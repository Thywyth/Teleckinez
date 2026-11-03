# АНОТАЦІЯ

Ціздин Р. Р. Розробка архітектури голосового асистента для
диспетчеризації та контролю станів виконавчих пристроїв IoT. Курсова
робота. Львівський національний університет імені Івана Франка, 2026.

У роботі запропоновано архітектуру локального голосового асистента
розумного будинку, який не лише надсилає команду пристрою, а й
перевіряє її виконання: команда вважається виконаною, коли фактичний стан
пристрою збігся з бажаним. Асистент побудовано як подієво-орієнтований
модульний моноліт на Python з обміном з пристроями через MQTT. Кожна
команда проходить скінченний автомат із сімома кінцевими станами, що
розрізняє успіх, відмову, відсутність відповіді, невиконання,
відключення пристрою і заміну новішою командою. Реалізовано прототип із
розпізнаванням мовлення Vosk і faster-whisper, розбором команд правилами
та локальною мовною моделлю, синтезом Piper, веб-панеллю і симулятором
31 пристрою з ін'єкцією збоїв. Експерименти показали точність розбору
команд 90,6 %, затримку від кінця фрази до відповіді 0,18 с (без
очікування паузи), правильну реакцію на 198 із 200 змодельованих збоїв і
відсутність деградації при 100 пристроях.

**Ключові слова:** голосовий асистент, інтернет речей, розумний будинок,
MQTT, контроль виконання команд, цифровий двійник, розпізнавання
мовлення, скінченний автомат.

# ABSTRACT

Tsizdyn R. R. Architecture of a voice assistant for dispatching and
monitoring the states of IoT actuators. Course work. Ivan Franko National
University of Lviv, 2026.

The work proposes an architecture of an offline smart-home voice
assistant that does not stop at sending a command but verifies its
execution: a command is considered done only when the reported state of
the device matches the desired one. The assistant is an event-driven
modular monolith in Python that talks to devices over MQTT. Every command
goes through a finite-state machine with seven terminal states that
distinguish success, rejection, no response, accepted but not executed,
device offline and superseded by a newer command. The prototype includes
speech recognition with Vosk and faster-whisper, rule-based and local LLM
command parsing, Piper speech synthesis, a web panel and a simulator of 31
devices with fault injection. Experiments showed 90.6 % command parsing
accuracy, 0.18 s from the end of speech to the start of the answer
(excluding the end-of-speech pause), a correct reaction to 198 of 200
simulated faults and no degradation with 100 devices.

**Keywords:** voice assistant, Internet of Things, smart home, MQTT,
command execution control, digital twin, speech recognition, finite-state
machine.
