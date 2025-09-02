# TrackerBot - Currency Exchange Rates Tracker

Полнофункциональное веб-приложение для отслеживания курсов валют с React фронтендом и FastAPI бэкендом.

## 📱 Тестирование с мобильных устройств

Все ссылки доступны для внешних устройств (телефон, планшет, ТВ):

- **Главный фронтенд:** `http://app.grither.exchange/`
- **Клиент настроек:** `http://app.grither.exchange/settings-client`
- **ТВ режим:** `http://app.grither.exchange/?frontend=tv`

## 🚀 Быстрый старт

### Локальное развертывание

1. **Клонирование репозитория:**
   ```bash
   git clone https://github.com/Noozhdin/TrackerBot-Rates.git
   cd TrackerBot-Rates
   ```

2. **Запуск с Docker (рекомендуется):**
   ```bash
   docker-compose up --build -d
   ```

3. **Доступ к приложению:**
   - Веб-интерфейс: http://localhost:8000
   - Глобальный клиент настроек: http://localhost:8000/settings-client
   - API: http://localhost:8000/api/
   - Альтернативный порт: http://localhost:3000

## 🏗️ Архитектура проекта

```
📦 TrackerBot/
├── 🎨 frontend/                 # React приложение
│   ├── src/
│   │   ├── components/          # React компоненты
│   │   ├── hooks/              # Пользовательские хуки
│   │   ├── lib/                # Утилиты и API
│   │   └── utils/              # Вспомогательные функции
│   └── build/                  # Собранный фронтенд
├── 🔧 backend/                 # FastAPI сервер
│   ├── database.py             # Работа с базой данных
│   ├── fx_service.py           # Сервис курсов валют
│   ├── server_sqlite.py        # Основной сервер
│   └── static_server.py        # Сервер статики + API
├── 🐳 Docker конфигурация
│   ├── Dockerfile              # Многоступенчатая сборка
│   └── docker-compose.yml      # Локальная разработка
└── 🔧 Утилиты
    └── Различные скрипты развертывания
```

## 🛠️ Технологический стек

### Frontend
- **React 18** - Пользовательский интерфейс
- **Tailwind CSS** - Стилизация
- **Shadcn/ui** - Компоненты UI
- **React Router** - Маршрутизация
- **Lucide React** - Иконки

### Backend
- **FastAPI** - Веб-фреймворк
- **SQLite** - База данных
- **Pydantic** - Валидация данных
- **Uvicorn** - ASGI сервер
- **Python 3.11** - Язык программирования

### DevOps & Deploy
- **Docker** - Контейнеризация
- **Docker Compose** - Оркестрация
- **Nginx** - Reverse proxy
- **Multi-stage builds** - Оптимизация образов

## 📋 Возможности

### ✨ Основные функции
- 📊 Отображение актуальных курсов валют в виде таблиц и плиток
- 🔄 Автоматическое обновление данных в реальном времени
- 💱 Калькулятор валют с поддержкой всех валют
- 📈 История изменений курсов с отслеживанием процентов
- 🎛️ Глобальный клиент настроек с полным управлением
- 📱 Адаптивный дизайн для всех устройств
- 📺 ТВ режим для крупных экранов
- 🌙 Темная/светлая тема
- 🗂️ Управление отдельными фронтендами

## 🚀 Продакшен развертывание

### Быстрое развертывание

```bash
docker-compose up --build -d
```

### Управление на сервере

```bash
# Статус сервисов
docker-compose ps

# Логи приложения
docker-compose logs -f trackerbot

# Перезапуск приложения
docker-compose restart trackerbot
```

## 📊 Мониторинг

### Проверка состояния
```bash
# Статус контейнеров
docker-compose ps

# Логи приложения
docker-compose logs -f

# Health check
curl http://localhost:8000/api/
curl http://localhost:8000/health
```

## 📚 API Documentation

### Основные эндпоинты

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/api/` | Статус API |
| GET | `/api/rates` | Текущие курсы валют |
| GET | `/api/history` | История курсов |
| GET | `/api/global-settings` | Глобальные настройки |
| PUT | `/api/global-settings` | Обновить глобальные настройки |
| GET | `/api/settings` | Полные настройки системы |
| PUT | `/api/settings` | Обновить настройки |
| POST | `/api/sync-rates` | Синхронизировать курсы |
| GET | `/api/changes` | История изменений |
| GET | `/api/frontends` | Список фронтендов |
| GET | `/health` | Health check |

### Примеры запросов
```bash
# Получить статус
curl http://localhost:8000/api/

# Получить курсы валют
curl http://localhost:8000/api/rates

# Health check
curl http://localhost:8000/health
```

## 🤝 Участие в разработке

### Процесс разработки
1. Fork репозитория
2. Создайте feature branch
3. Внесите изменения
4. Добавьте тесты
5. Создайте Pull Request

### Стандарты кода
- **Python:** Black, isort, flake8, mypy
- **JavaScript:** ESLint, Prettier
- **Commit messages:** Conventional Commits

## 🐛 Устранение неполадок

### Частые проблемы

1. **Docker не запускается**
   - Убедитесь, что Docker Desktop запущен
   - Проверьте права доступа

2. **Порт занят**
   ```bash
   # Найти процесс, использующий порт
   netstat -tlnp | grep :8000
   # Остановить Docker Compose
   docker-compose down
   ```

3. **Проблемы с зависимостями Frontend**
   ```bash
   cd frontend
   rm -rf node_modules package-lock.json
   npm install --legacy-peer-deps
   ```

## 📄 Лицензия

Этот проект лицензирован под MIT License.

## 👨‍💻 Авторы

- **TrackerBot Team** - Начальная разработка

---

**Сделано с ❤️ для сообщества разработчиков**