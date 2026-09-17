.PHONY: module setup lint clean

module.tar.gz: src requirements.txt run.sh setup.sh meta.json
	tar -czf module.tar.gz src requirements.txt run.sh setup.sh meta.json

module: module.tar.gz

setup:
	python3 -m venv venv
	./venv/bin/pip install --upgrade pip
	./venv/bin/pip install -e ".[dev]"

lint:
	./venv/bin/ruff check src

clean:
	rm -f module.tar.gz
	rm -rf venv build dist *.egg-info
	find . -type d -name __pycache__ -exec rm -rf {} +
