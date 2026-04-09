.DEFAULT_GOAL := help
.PHONY: help logs test docker-test stop build up up-view install setup run admin view view-1 view-2 view-3 view-4

help:
	@perl -nle'print $& if m{^[a-zA-Z_-]+:.*?## .*$$}' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-25s\033[0m %s\n", $$1, $$2}'

install: ## install all Python dependencies (local dev)
	pip install uv 2>/dev/null || true
	uv pip install -r requirements/local.txt

setup: install ## install deps + Playwright browsers + migrate + bootstrap CRM
	playwright install --with-deps chromium
	python manage.py migrate --no-input
	python manage.py setup_crm

run: ## run the daemon
	python manage.py rundaemon

test: ## run the test suite
	.venv/bin/pytest

admin: ## start the Django Admin web server (with postgres)
	docker compose -f local.yml up -d postgres
	docker compose -f local.yml up admin

# Docker targets
logs: ## follow the logs of the service
	docker compose -f local.yml logs -f

docker-test: ## run tests in Docker
	docker compose -f local.yml run --remove-orphans -e DB_ENGINE=django.db.backends.sqlite3 admin py.test -vv -p no:cacheprovider

stop: ## stop all services defined in Docker Compose
	docker compose -f local.yml stop

build: ## build all services defined in Docker Compose
	docker compose -f local.yml build

up: ## run the defined service in Docker Compose
	docker compose -f local.yml up --build -d
	docker compose -f local.yml logs -f

up-view: ## run the defined service in Docker Compose and open vinagre
	docker compose -f local.yml up --build -d
	sleep 3
	$(MAKE) view-1
	docker compose -f local.yml logs -f

view: view-1 ## open vinagre for worker-1 (alias)

view-1: ## open vinagre for worker-1
	@sh -c 'vinagre vnc://127.0.0.1:5901 > /dev/null 2>&1 &'

view-2: ## open vinagre for worker-2
	@sh -c 'vinagre vnc://127.0.0.1:5902 > /dev/null 2>&1 &'

view-3: ## open vinagre for worker-3
	@sh -c 'vinagre vnc://127.0.0.1:5903 > /dev/null 2>&1 &'

view-4: ## open vinagre for worker-4
	@sh -c 'vinagre vnc://127.0.0.1:5904 > /dev/null 2>&1 &'
