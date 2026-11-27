#!/bin/bash
set -u
cd -- "$(dirname -- "${BASH_SOURCE[0]}")" || exit 1
if [[ ! -x .venv/bin/python ]]; then
  printf '먼저 setup_mac.command를 실행해 주세요.\n'
  read -r -p 'Enter를 누르면 닫습니다. ' sense_reply
  exit 1
fi
.venv/bin/python run.py --open-browser "$@"
sense_exit=$?
if [[ "$sense_exit" -ne 0 ]]; then
  printf '\n실행에 실패했습니다. 위 오류 내용과 README_KO.md를 확인해 주세요.\n'
fi
read -r -p 'Enter를 누르면 닫습니다. ' sense_reply
exit "$sense_exit"
