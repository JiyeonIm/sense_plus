#!/bin/bash
set -u
cd -- "$(dirname -- "${BASH_SOURCE[0]}")" || exit 1

sense_fail() {
  printf '\n설치가 완료되지 않았습니다. 위 오류와 README_KO.md를 확인하세요.\n'
  read -r -p 'Enter를 누르면 닫습니다. ' sense_reply
  exit 1
}

printf 'SENSE+ 최초 설치 - Python 3.11 또는 3.12가 필요합니다.\n'
printf '기존 프로젝트와 전역 Python 설정은 바꾸지 않습니다.\n\n'

if [[ ! -x .venv/bin/python ]]; then
  sense_python=''
  for sense_candidate in python3.12 python3.11 python3; do
    if command -v "$sense_candidate" >/dev/null 2>&1 && "$sense_candidate" -c 'import sys; assert sys.version_info[:2] in ((3, 11), (3, 12))' >/dev/null 2>&1; then
      sense_python="$sense_candidate"
      break
    fi
  done
  if [[ -z "$sense_python" ]]; then
    printf 'Python 3.11 또는 3.12를 찾지 못했습니다. 설치 후 다시 실행하세요.\nPython은 자동 설치하지 않습니다.\n'
    sense_fail
  fi
  "$sense_python" -m venv .venv || sense_fail
fi

.venv/bin/python -c 'import sys; assert sys.version_info[:2] in ((3, 11), (3, 12)), "Python 3.11/3.12 required"' || sense_fail
.venv/bin/python -m pip install -r requirements.txt || sense_fail
printf '\n설치 완료. start_mac.command를 실행하면 됩니다.\n유료 AI API는 호출하지 않았습니다.\n'
read -r -p 'Enter를 누르면 닫습니다. ' sense_reply
