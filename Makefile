.PHONY: up down reset test seed-sql logs sync-github import apply-manifest pnl token e2e
comma := ,

# Every API call needs a session (Phase 4). FL_API_TOKEN from .env if set (service admin), otherwise a
# SANDBOX_MODE dev login as admin. Override: make pnl TOKEN=<token>
TOKEN ?= $(or $(shell grep -E '^FL_API_TOKEN=.+' .env 2>/dev/null | cut -d= -f2-),$(shell curl -s -X POST localhost:8000/api/v1/auth/dev-login -H 'Content-Type: application/json' -d '{"role":"admin"}' | python3 -c 'import json,sys; print(json.load(sys.stdin).get("token",""))' 2>/dev/null))
AUTH = -H "Authorization: Bearer $(TOKEN)"

up:            ## build and start everything (http://localhost:3000)
	docker compose up --build -d && docker compose ps

down:
	docker compose down

reset:         ## wipe the database volume and re-seed
	docker compose down -v && docker compose up --build -d

logs:
	docker compose logs -f api web

test:          ## backend unit + end-to-end engine tests (no database needed)
	cd backend && python -m pytest

seed-sql:      ## write the deterministic seed as plain SQL
	cd backend && python -m app.seed.generate --manifest ../features.yaml --out ../db/seed.sql

sync-github:   ## ingest a real repo: make sync-github REPO=owner/name [TEAM=team-key]
	curl -s -X POST localhost:8000/api/v1/integrations/github/sync $(AUTH) -H 'Content-Type: application/json' \
	  -d '{"repo": "$(REPO)"$(if $(TEAM),$(comma) "default_team_key": "$(TEAM)")}'

apply-manifest: ## re-read features.yaml into the Feature Graph
	curl -s -X POST localhost:8000/api/v1/manifest/apply $(AUTH); echo

import:        ## make import KIND=accounts FILE=templates/accounts.csv PERIOD=2026-10
	curl -s -X POST "localhost:8000/api/v1/import/$(KIND)?period=$(PERIOD)" $(AUTH) --data-binary @$(FILE); echo

pnl:           ## recompute the P&L for every month
	curl -s -X POST localhost:8000/api/v1/jobs/compute-pnl $(AUTH); echo

token:         ## print an admin token (for curl or the Authorize button at localhost:8000/docs)
	@echo $(TOKEN)

e2e:           ## Phase 4 demo walkthrough against the running stack (moves the seeded sunset plans)
	python3 scripts/e2e_phase4.py http://localhost:3000
