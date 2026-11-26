.PHONY: install lint test unit smoke regression negative all-tests probe order order-dry flow clean

install:
	pip install -e ".[dev]"

lint:
	ruff check src tests

# Оффлайн-проверки каркаса: работают везде, доступ к стенду не нужен
unit:
	pytest -m unit -q

# Основной флоу, создаёт заказы на стенде
smoke:
	pytest -m smoke -v

regression:
	pytest -m "regression and not negative" -v

negative:
	pytest -m negative -v

# Всё, кроме создания заказов — безопасно гонять часто
test:
	pytest -m "not creates_order" -v

all-tests:
	pytest -v

# Параллельный прогон: N не больше размера пула аккаунтов в LO_ACCOUNTS
par:
	pytest -m "not creates_order" -n 2 -v

# Оформление заказа — основной инструмент
order:
	python -m lo_api.cli.order

# Собрать корзину и остановиться перед созданием заказа
order-dry:
	python -m lo_api.cli.order --dry-run

# Диагностика путей авторизации (код входа не запрашивается)
probe:
	python -m lo_api.cli.probe_auth

# Интерактивный ручной прогон
flow:
	python -m lo_api.cli.run_flow

clean:
	rm -rf .pytest_cache allure-results **/__pycache__
