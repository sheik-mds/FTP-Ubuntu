# FTP Site - Setup Guide

## Overview

FTP Site is a Flask-based web application for:

- Secure login
- Server management
- Host file browsing
- Remote file transfer over SSH/SFTP
- Folder compression before transfer
- Dashboard stats for transfer size and server counts
- Site shortcut management

### Default Details

- **App file:** `ftp.py`
- **Default port:** `5009`
- **Database path:** `/home/sheik/FTP-Site/FTP-Site/app.db`
- **Host browse path:** `/home/sheik/FTP-Site/FTP-Site`
- **Remote destination path:** `/tmp`
- **Default username:** `superadmin01`
- **Default password:** `Admin@123`

---

## Project Structure

Example structure:

```bash
/home/sheik/FTP-site
├── ftp.py
├── start_ftp_pm2.sh
├── venv/
├── app.db
└── ...

Prerequisites

Make sure the server has:

Ubuntu/Linux
Python 3
pip
python3-venv
Node.js
npm
PM2
SSH access to target servers
Valid PEM key files on the host machine
Step 1: Create Application Folder

Create the application folder and move into it:

sudo mkdir -p /Certa/Admin/FTP-site
sudo chown -R $USER:$USER /Certa/Admin/FTP-site
cd /Certa/Admin/FTP-site

Copy these files into this folder:

ftp.py
start_ftp_pm2.sh
Step 2: Install System Packages

Install required system packages:

sudo apt update
sudo apt install -y python3 python3-pip python3-venv nodejs npm

Install PM2 globally:

sudo npm install -g pm2

Check installed versions:

python3 --version
pip3 --version
node --version
npm --version
pm2 -v
Step 3: Create Python Virtual Environment

Go to the project folder:

cd /Certa/Admin/FTP-site

Create virtual environment:

python3 -m venv venv

Activate virtual environment:

source venv/bin/activate

Upgrade pip:

pip install --upgrade pip

Install Python dependencies:

pip install flask flask-login paramiko werkzeug
Step 4: Verify Application Configuration

Open ftp.py and verify these values:

APP_PORT = 5009
DB_PATH = "/home/sheik/FTP-Site/FTP-Site/app.db"
HOST_UPLOADS = "/home/sheik/FTP-Site/FTP-Site"
REMOTE_SEND_DEST = "/tmp"
MAX_LIST_ITEMS = 5000
Secret Key Configuration

This app uses:

app.secret_key = os.environ.get("APP_SECRET", "change-me-please-very-long-secret")
Recommended

Before production use, set a strong secret key:

export APP_SECRET="your-strong-random-secret-key"
Step 5: Create Required Host Directories

The app expects these paths to exist:

/home/sheik/FTP-Site/FTP-Site
database parent path for app.db

Create them if needed:

sudo mkdir -p /home/sheik/FTP-Site/FTP-Site
sudo chown -R $USER:$USER /home/sheik/FTP-Site
Step 6: First Manual Run Test

Before using PM2, test the app manually.

Activate virtual environment:

cd /Certa/Admin/FTP-site
source venv/bin/activate

Run the application:

python ftp.py

Expected console output will show:

Starting GTS XFTP on port 5009
Host uploads directory: /home/sheik/FTP-Site/FTP-Site
Database path: /home/sheik/FTP-Site/FTP-Site/app.db
Remote destination: /tmp

Open in browser:

http://SERVER-IP:5009

Stop manual run using:

CTRL + C
Step 7: Default Login

On first run, the app creates a default user if the database is empty.

Use:

Username: superadmin01
Password: Admin@123
Step 8: Database Initialization

The application automatically creates these tables on first run:

users
servers
site_shortcuts
transfers

It also performs safe database migration for older versions.

No manual database setup is required.
