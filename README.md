# Telegram Summary Bot

Бот для суммаризации чатов в Telegram.

## Функции
- Сохраняет все сообщения (текст, голос, видео)
- Распознает голосовые и видеосообщения через Whisper
- Делает суммаризацию чата через YandexGPT

## Команды
- `/summary` - сделать суммаризацию чата
- `/help` - помощь

## Переменные окружения
- `BOT_TOKEN` - токен Telegram бота
- `FOLDER_ID` - ID папки в Yandex Cloud
- `API_KEY` - API ключ Yandex Cloud