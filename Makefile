.PHONY: up down reset test seed-sql logs sync-github

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

sync-github:   ## ingest a real repo: make sync-github REPO=owner/name
	curl -s -X POST localhost:8000/api/v1/integrations/github/sync -H 'Content-Type: application/json' \
	  -d '{"repo": "$(REPO)"}'
