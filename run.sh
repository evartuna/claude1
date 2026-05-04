#!/bin/bash
# .env 파일이 있으면 환경변수 로드
if [ -f .env ]; then
  export $(grep -v '^#' .env | grep -v '^$' | xargs)
fi

# gunicorn이 있으면 운영 모드, 없으면 개발 모드
if command -v gunicorn &>/dev/null; then
  echo "[운영 모드] gunicorn 4 workers, port 5000"
  gunicorn -w 4 -b 0.0.0.0:5000 app:app
else
  echo "[개발 모드] Flask dev server, port 5000"
  python app.py
fi
