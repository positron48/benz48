# Деплой в k3s (GitOps)

Репозиторий: **https://github.com/positron48/benz48**

GitOps-манифесты: [`devops-time-host/apps/lipetsk-gas-monitor`](../../www/my/k3s/devops-time-host/apps/lipetsk-gas-monitor).

Публичный URL: **https://gas.qantrix.ru**

## Стратегия данных

| Слой | Локально (dev) | Production (k3s) |
|------|----------------|------------------|
| API | SQLite `history.db` | SQLite на PVC |
| JSON-снимки | пишутся в `data/snapshots/` | **отключены** (`SAVE_SNAPSHOT_FILES=false`) |
| Чтение файлов web'ом | нет | нет |

Web **никогда** не читает JSON — только SQLite. В prod collector пишет сразу в БД без лишнего I/O.

SQLite настроен с WAL + `busy_timeout` для быстрых чтений при фоновой записи collector'а.

## Перенос истории без потерь

```bash
# 1. Экспорт с Mac (только БД, ~2 MB)
make export-data

# 2. После первого pod на k3s — импорт
./scripts/import-k3s-data.sh lipetsk-gas-data-*.tar.gz lipetsk-gas-monitor
```

Подробный чеклист: `devops-time-host/apps/lipetsk-gas-monitor/RELEASE_K3S.md`.

## CI / образ

Push в `main` → GitHub Actions → `ghcr.io/positron48/benz48:latest`.

Flux ImagePolicy подхватывает digest автоматически.

## Архитектура pod

```
┌─────────────────────────────────────┐
│  lipetsk-gas-monitor (1 replica)  │
│  ┌─────────┐  ┌──────────────────┐  │
│  │   web   │  │    collector     │  │
│  │ :8000   │  │  loop 5 min      │  │
│  └────┬────┘  └────────┬─────────┘  │
│       └───────┬────────┘            │
│         PVC /app/data               │
│         └── history.db              │
└─────────────────────────────────────┘
          │
    Ingress gas.qantrix.ru
```

Один pod / два контейнера — единственный writer в SQLite, без гонок между pod'ами.
