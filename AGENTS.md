# AGENTS.md — Lipetsk Gas Monitor (benz48)

Краткий контекст для AI-агентов. Полная внутренняя документация: `docs/internal/PROJECT.md` (локально, в gitignore).

## Проект

Мониторинг АЗС **Липецкой области**: сбор каждые 5 мин → SQLite → дашборд ECharts.

- **Prod:** https://gas.qantrix.ru
- **Repo:** https://github.com/positron48/benz48
- **Локально:** `make up` → http://localhost:18743

## Источник данных

Парсим HTML-таблицу с https://gas-monitoring.admlr.lipetsk.ru/reports/ (`SOURCE_URL`, вся область без фильтра по одному МО).

`app/parser.py`: brand, district, address, is_working, fuel_92/95/diesel, queue, reason, last_report_at.  
`station_id` = hash(brand|address|name). Регионы UI — 7 кластеров в `app/regions.py`.

## Стек

Python 3.12, FastAPI, BeautifulSoup, SQLite (WAL), Docker, ECharts.  
Два процесса: **collector** (пишет) + **web** (читает API + static). Web не читает JSON-снимки.

## Ключевые файлы

| Путь | Назначение |
|------|------------|
| `app/collector.py` | цикл сбора |
| `app/web.py` | API + static |
| `app/storage.py` | SQLite, dedupe |
| `app/bootstrap.py` | seed из `bootstrap/history.db.gz` если мало снимков |
| `app/static/index.html` | весь фронт |
| `docker-compose.yml` | local dev |
| `DEPLOY.md` | публичный деплой |

## CI / образ

Push в `main` → GitHub Actions (`.github/workflows/docker-image.yml`) → `ghcr.io/positron48/benz48:latest` (linux/amd64).

## Infra (k3s)

GitOps в **devops-time-host** → `apps/lipetsk-gas-monitor/`: web (1 replica, RollingUpdate) + collector (1), PVC, Ingress gas.qantrix.ru, Flux image automation.  
Prod: `SAVE_SNAPSHOT_FILES=false`. Перенос истории: `make export-data` + `scripts/import-k3s-data.sh`.

## Локальные правила для агентов

- **Не коммитить** `.env`, `data/`, `docs/internal/`
- После импорта `history.db` — **остановить Docker** (`import-local-data.sh`), иначе SQLite 500
- Bootstrap только на **web**, не на collector
- Минимальный diff; не трогать несвязанный код
- Тесты: `make test` (pytest, `BOOTSTRAP_ENABLED=false` в conftest)
- Коммиты — только по запросу пользователя

## Env (важное)

`SOURCE_URL`, `DATA_DIR`, `COLLECTION_ENABLED` (false — архив), `COLLECT_INTERVAL_SECONDS=300`, `SAVE_SNAPSHOT_FILES` (true dev / false prod), `BOOTSTRAP_ENABLED`, `ARCHIVE_FROM`/`ARCHIVE_TO`, `PORT=18743`.
