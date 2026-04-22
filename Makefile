.DEFAULT_GOAL := help
.PHONY: help logs test docker-test stop build up up-view install setup setup-account browse-account run admin view view-1 view-2 view-3 view-4

# Positional argument for `make setup-account <username>`.
# Only active when `setup-account` is the first goal, so it doesn't shadow
# real targets like `make test`.
ifeq (setup-account,$(firstword $(MAKECMDGOALS)))
SETUP_ACCOUNT_ARG := $(wordlist 2,2,$(MAKECMDGOALS))
$(eval $(SETUP_ACCOUNT_ARG):;@:)
endif

# Positional argument for `make browse-account <username>`.
ifeq (browse-account,$(firstword $(MAKECMDGOALS)))
BROWSE_ACCOUNT_ARG := $(wordlist 2,2,$(MAKECMDGOALS))
$(eval $(BROWSE_ACCOUNT_ARG):;@:)
endif

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

setup-account: ## interactive VNC login for a LinkedIn account (usage: make setup-account <username>). Uses the account's stored proxy_url.
	@test -n "$(SETUP_ACCOUNT_ARG)" || { echo "usage: make setup-account <username>"; exit 1; }
	docker compose -f local.yml up -d postgres
	@echo ""
	@echo ">>> Starting setup for account '$(SETUP_ACCOUNT_ARG)'"
	@echo ">>> Connect via VNC:   vnc://localhost:5910"
	@echo ">>> Or noVNC in browser: http://localhost:6090/vnc.html"
	@echo ""
	docker compose -f local.yml run --rm \
		-e RUN_MODE=setup \
		-e LINKEDIN_PROFILE=$(SETUP_ACCOUNT_ARG) \
		-p 5910:5900 \
		-p 6090:6080 \
		worker-pool

browse-account: ## open a browser with saved cookies + the account's proxy_url, no automation (usage: make browse-account <username>).
	@test -n "$(BROWSE_ACCOUNT_ARG)" || { echo "usage: make browse-account <username>"; exit 1; }
	docker compose -f local.yml up -d postgres
	@echo ""
	@echo ">>> Browse mode for account '$(BROWSE_ACCOUNT_ARG)' (no daemon, no automation)"
	@echo ">>> Connect via VNC:   vnc://localhost:5911"
	@echo ">>> Or noVNC in browser: http://localhost:6091/vnc.html"
	@echo ">>> Ctrl+C (or docker stop) to exit."
	@echo ""
	docker compose -f local.yml run --rm \
		-e RUN_MODE=browse \
		-e LINKEDIN_PROFILE=$(BROWSE_ACCOUNT_ARG) \
		-p 5911:5900 \
		-p 6091:6080 \
		worker-pool

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
