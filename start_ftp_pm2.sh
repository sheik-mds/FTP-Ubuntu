#!/bin/bash

APP_DIR="/Certa/Admin/FTP-site"
APP_NAME="FTP-site"
PYTHON="$APP_DIR/venv/bin/python"

cd "$APP_DIR" || exit 1

pm2 start ftp.py \
  --name "$APP_NAME" \
  --interpreter "$PYTHON"
