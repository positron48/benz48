PORT ?= 18743

.PHONY: up down logs test collect awake up-awake down-awake status export-data

up:
	@test -f .env || cp .env.example .env
	docker compose up -d --build
	@echo ""
	@echo "Lipetsk Gas Monitor запущен"
	@echo "Открыть: http://localhost:$(PORT)"
	@echo "Остановить: make down"

up-awake: up awake
	@$(MAKE) --no-print-directory status

down:
	docker compose down
	@echo ""
	@echo "Lipetsk Gas Monitor остановлен"

down-awake: down
	@if [ -f data/caffeinate.pid ]; then \
		kill $$(cat data/caffeinate.pid) 2>/dev/null && echo "caffeinate остановлен" || true; \
		rm -f data/caffeinate.pid; \
	fi

awake:
	@mkdir -p data
	@if [ -f data/caffeinate.pid ] && kill -0 $$(cat data/caffeinate.pid) 2>/dev/null; then \
		echo "caffeinate уже запущен (pid $$(cat data/caffeinate.pid))"; \
	else \
		nohup caffeinate -dims tail -f /dev/null >> data/caffeinate.log 2>&1 & \
		echo $$! > data/caffeinate.pid; \
		sleep 0.5; \
		if kill -0 $$(cat data/caffeinate.pid) 2>/dev/null; then \
			echo "caffeinate запущен (pid $$(cat data/caffeinate.pid)) — Mac не уйдёт в сон"; \
		else \
			echo "Ошибка: caffeinate не запустился, см. data/caffeinate.log"; \
			rm -f data/caffeinate.pid; \
			exit 1; \
		fi; \
	fi

status:
	@echo "=== Docker ==="
	@docker compose ps
	@echo ""
	@echo "=== API ==="
	@curl -sf http://localhost:$(PORT)/api/meta | python3 -c "import sys,json;d=json.load(sys.stdin);print(f'снимков: {d[\"snapshot_count\"]}, АЗС: {len(d[\"stations\"])}, последний: {d[\"to\"]}')" 2>/dev/null || echo "API недоступен"
	@echo ""
	@if [ -f data/caffeinate.pid ] && kill -0 $$(cat data/caffeinate.pid) 2>/dev/null; then \
		echo "caffeinate: активен (pid $$(cat data/caffeinate.pid))"; \
	else \
		echo "caffeinate: не запущен"; \
	fi

logs:
	docker compose logs -f

test:
	python -m pytest -q

collect:
	python -m app.collector --once

export-data:
	./scripts/export-k3s-data.sh
