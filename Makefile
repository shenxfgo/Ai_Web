# Windows 上没有 make 时改用：powershell -NoProfile -ExecutionPolicy Bypass -File scripts\dev.ps1 <目标>，目标名一致。
SHELL := bash
BACKEND := backend
FRONTEND := frontend

.PHONY: help bootstrap check-env migrate demo-db dev dev-backend dev-frontend lint fmt typecheck test check clean

help:
	@grep -E '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | sed 's/^\([a-z-]*\):.*## /\1\t/'

bootstrap: ## 装后端+前端依赖，并从 .env.example 生成带随机密钥的 backend/.env
	python scripts/bootstrap.py

check-env: ## 环境体检（配置/目录/元数据库/LLM 探活），有 fail 时退出码非 0
	cd $(BACKEND) && uv run python scripts/check_env.py

migrate: ## 元数据库升级到最新 schema
	cd $(BACKEND) && uv run alembic upgrade head

demo-db: ## 建本机演示库 ai_web_demo（只探测、只建不删；建库账号走 backend/.setup/my_login.cnf）
	powershell -NoProfile -ExecutionPolicy Bypass -File scripts/demo_db.ps1

dev-backend: ## 起 FastAPI（127.0.0.1:8000，热重载）
	cd $(BACKEND) && uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload

dev-frontend: ## 起 Vite（127.0.0.1:5173，/api 代理到 8000）
	cd $(FRONTEND) && npm run dev

dev: ## 并行起前后端（要分终端观察日志时改用上面两个目标）
	powershell -NoProfile -ExecutionPolicy Bypass -File scripts/dev.ps1 dev

lint: ## 后端 ruff 静态检查
	cd $(BACKEND) && uv run ruff check app scripts tests alembic

fmt: ## 后端 ruff 格式化
	cd $(BACKEND) && uv run ruff format app scripts tests alembic && uv run ruff check --fix app scripts tests alembic

typecheck: ## 前端 vue-tsc 类型检查
	cd $(FRONTEND) && npm run typecheck

test: ## 后端测试（未配 AIWEB_PG_TEST_DSN 时自动跳过 pg 用例）
	cd $(BACKEND) && uv run python -m pytest

check: lint typecheck test ## 提交前总闸

clean: ## 只清构建产物与缓存，绝不动数据库、结果集和 knowledge/
	rm -rf $(BACKEND)/.pytest_cache $(BACKEND)/.ruff_cache $(BACKEND)/.mypy_cache $(BACKEND)/.uvtmp
	rm -rf $(FRONTEND)/dist
