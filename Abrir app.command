#!/bin/bash
# Dois cliques para abrir o app. Na primeira vez instala as dependências.
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo "Primeira vez: instalando dependências..."
  python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt || exit 1
fi
exec .venv/bin/python app.py
