#!/usr/bin/env bash

set -euo pipefail

if [ "${1:-}" = "" ]; then
  echo 'Usage: ./git-commit.sh "message"'
  exit 1
fi

git add .
git commit -m "$1"
./git-push-current.sh
