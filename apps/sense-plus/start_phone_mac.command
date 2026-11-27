#!/usr/bin/env bash
cd "$(dirname "$0")" || exit 1
if [[ ! -x .venv/bin/python ]]; then echo "먼저 setup_mac.command를 실행하세요."; exit 1; fi
.venv/bin/python phone.py
