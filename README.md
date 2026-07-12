# Мониторинг топлива — Липецкая область

Сервис каждые 5 минут собирает данные со [всего портала мониторинга АЗС Липецкой области](https://gas-monitoring.admlr.lipetsk.ru/reports/?scope=recent_reports&district=), сохраняет JSON-снимки с меткой времени и строит интерактивные графики.

## Быстрый старт

```bash
cd ~/Projects/lipetsk-gas-monitor
make up
```

После запуска откройте: **http://localhost:18743**

Остановка:

```bash
make down
```

## Что внутри

- **collector** — парсит HTML-таблицу и сохраняет снимки в `data/snapshots/YYYY/MM/DD/`
- **web** — FastAPI + dashboard с графиками ECharts
- **SQLite** — `data/history.db` для быстрых временных выборок

## Настройки

Скопируйте `.env.example` в `.env` (это делает `make up` автоматически):

| Переменная | По умолчанию | Описание |
|---|---|---|
| `PORT` | `18743` | Локальный порт веб-интерфейса |
| `COLLECT_INTERVAL_SECONDS` | `300` | Интервал сбора (5 минут) |
| `SOURCE_URL` | URL Липецка | Страница для парсинга |
| `DATA_DIR` | `./data` | Каталог данных |
| `SAVE_SNAPSHOT_FILES` | `true` | JSON-снимки (в k3s: `false`) |

## Деплой в k3s

См. [DEPLOY.md](DEPLOY.md) и GitOps в `devops-time-host/apps/lipetsk-gas-monitor/`.

```bash
make export-data   # перед переездом — упаковать history.db
```

Production: **https://gas.qantrix.ru**

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env

# Один сбор
python -m app.collector --once

# Веб-сервер
python -m app.web

# Тесты
make test
```

## Резервное копирование

```bash
make export-data
# или полный каталог:
tar -czf lipetsk-gas-backup-$(date +%Y%m%d).tar.gz data/
```

## Mac локально

Работает через Docker Desktop, пока Mac включён и не уходит в сон. Закрытая крышка обычно переводит Mac в сон — оставьте крышку открытой или отключите автосон в настройках питания.

## Логи

```bash
make logs
```
