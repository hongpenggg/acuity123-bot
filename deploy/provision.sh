#!/usr/bin/env bash
#
# Provision a fresh Ubuntu droplet/VPS to run the LKC OphSoc Tele Bot.
#
# Run as root on the server:
#     bash deploy/provision.sh
#
# Idempotent: every step checks before it acts, so re-running is safe and is the
# correct response to a failure part-way through.
#
# It deliberately does NOT start the bot. The service is installed and enabled,
# but you must write a real .env first — otherwise it crash-loops on a missing
# token. Sequence: run this, write .env, then `systemctl start studybot`.
#
# What it does, in order:
#   1. swap (essential on a 512 MB droplet — without it the OOM killer takes
#      Postgres or the bot)
#   2. OS updates and packages
#   3. timezone
#   4. a `deploy` user with your root key copied across
#   5. SSH hardening, validated with `sshd -t` before it dares restart the daemon
#   6. ufw and Fail2ban
#   7. PostgreSQL, tuned for the RAM it finds, plus role and database
#   8. the repo cloned to /opt/studybot in a virtualenv
#   9. the systemd unit, enabled but not started
#
set -euo pipefail

APP_DIR=/opt/studybot
APP_USER=deploy
REPO_URL="${REPO_URL:-https://github.com/hongpenggg/acuity123-bot.git}"
BRANCH="${BRANCH:-main}"
DB_NAME="${DB_NAME:-studybot}"
DB_USER="${DB_USER:-studybot}"
SWAP_MB="${SWAP_MB:-1024}"

log()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[1;32mok\033[0m  %s\n' "$*"; }
warn() { printf '    \033[1;33mwarn\033[0m %s\n' "$*"; }

if [ "$(id -u)" -ne 0 ]; then
    echo "run this as root" >&2
    exit 1
fi

if ! grep -qi ubuntu /etc/os-release; then
    warn "this was written for Ubuntu; $(. /etc/os-release && echo "$PRETTY_NAME") may need adjustments"
fi

TOTAL_MB=$(awk '/MemTotal/ {printf "%d", $2/1024}' /proc/meminfo)
log "Server has ${TOTAL_MB} MB RAM"

# --------------------------------------------------------------------- 1. swap
if swapon --show | grep -q .; then
    ok "swap already active"
else
    log "Creating ${SWAP_MB} MB swap"
    fallocate -l "${SWAP_MB}M" /swapfile 2>/dev/null || dd if=/dev/zero of=/swapfile bs=1M count="$SWAP_MB" status=none
    chmod 600 /swapfile
    mkswap /swapfile >/dev/null
    swapon /swapfile
    grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
    # Prefer RAM: only swap under real pressure, so the bot is not pushed out to disk.
    sysctl -q -w vm.swappiness=10
    grep -q '^vm.swappiness' /etc/sysctl.conf || echo 'vm.swappiness=10' >> /etc/sysctl.conf
    ok "swap on (swappiness=10)"
fi

# --------------------------------------------------------------- 2. packages
log "Updating the OS and installing packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get upgrade -y -qq
apt-get install -y -qq git python3-venv python3-pip fail2ban ufw \
    unattended-upgrades curl ca-certificates >/dev/null
ok "packages installed"

# --------------------------------------------------------------- 3. timezone
log "Setting the timezone to Asia/Singapore"
timedatectl set-timezone Asia/Singapore
ok "$(date '+%Y-%m-%d %H:%M %Z')"

# ------------------------------------------------------------ 4. deploy user
log "Ensuring the ${APP_USER} user exists"
if id "$APP_USER" >/dev/null 2>&1; then
    ok "user ${APP_USER} already exists"
else
    adduser --disabled-password --gecos "" "$APP_USER"
    usermod -aG sudo "$APP_USER"
    ok "created ${APP_USER}"
fi

# Give the deploy user the same key root already has, so root login can be
# restricted without losing access.
install -d -m 700 -o "$APP_USER" -g "$APP_USER" "/home/${APP_USER}/.ssh"
if [ -s /root/.ssh/authorized_keys ]; then
    install -m 600 -o "$APP_USER" -g "$APP_USER" \
        /root/.ssh/authorized_keys "/home/${APP_USER}/.ssh/authorized_keys"
    ok "copied authorized_keys to ${APP_USER} ($(wc -l < "/home/${APP_USER}/.ssh/authorized_keys") key(s))"
else
    warn "no /root/.ssh/authorized_keys found — set up a key before disabling password auth"
fi

# ---------------------------------------------------------- 5. SSH hardening
log "Hardening SSH"
SSHD_CONF=/etc/ssh/sshd_config.d/99-ophsoc-hardening.conf
cat > "$SSHD_CONF" <<'CONF'
# Managed by deploy/provision.sh
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
CONF
ok "wrote $SSHD_CONF"

# Cloud images often override sshd_config in a drop-in; make the daemon prove
# what it will actually use, and refuse to restart if the config is invalid —
# that is how people lock themselves out.
if sshd -t 2>/dev/null; then
    # `|| true`: with `set -e`, a grep that matches nothing would kill the script.
    EFFECTIVE=$(sshd -T | grep -E '^(passwordauthentication|permitrootlogin)' | tr '\n' ' ' || true)
    ok "sshd config valid: ${EFFECTIVE}"
    systemctl reload ssh 2>/dev/null || systemctl restart ssh
    ok "ssh reloaded"
else
    warn "sshd -t FAILED — not restarting. Fix $SSHD_CONF before logging out."
    sshd -t || true
fi

# ---------------------------------------------------------------- 6. firewall
log "Configuring ufw"
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw allow OpenSSH >/dev/null
ufw --force enable >/dev/null
ok "$(ufw status | head -1 || true)"

log "Enabling Fail2ban"
systemctl enable --now fail2ban >/dev/null 2>&1 || true
ok "fail2ban $(systemctl is-active fail2ban || true)"

log "Enabling unattended security upgrades"
echo 'unattended-upgrades unattended-upgrades/enable_auto_updates boolean true' | debconf-set-selections
dpkg-reconfigure -plow unattended-upgrades >/dev/null 2>&1 || true
ok "unattended-upgrades configured"

# --------------------------------------------------------------- 7. postgres
log "Installing PostgreSQL"
apt-get install -y -qq postgresql postgresql-contrib >/dev/null
systemctl enable --now postgresql >/dev/null 2>&1 || true
ok "postgresql $(systemctl is-active postgresql || true)"

# Tune for a small box: on 512 MB the stock shared_buffers (128 MB) plus a
# Python process is enough to invite the OOM killer.
BUFFERS=64; CACHE=256; WORK_MEM=4; MAINT_MEM=32
if [ "$TOTAL_MB" -ge 1800 ]; then BUFFERS=256; CACHE=1024; WORK_MEM=8; MAINT_MEM=64; fi
if [ "$TOTAL_MB" -ge 3800 ]; then BUFFERS=512; CACHE=2048; WORK_MEM=8; MAINT_MEM=128; fi
PG_VERSION_DIR=$(find /etc/postgresql -maxdepth 1 -mindepth 1 -type d | sort | tail -1)
PG_CONF="${PG_VERSION_DIR}/main/conf.d/ophsoc.conf"
mkdir -p "$(dirname "$PG_CONF")"
cat > "$PG_CONF" <<CONF
# Managed by deploy/provision.sh — sized for ${TOTAL_MB} MB of RAM
shared_buffers = ${BUFFERS}MB
effective_cache_size = ${CACHE}MB
work_mem = ${WORK_MEM}MB
maintenance_work_mem = ${MAINT_MEM}MB
max_connections = 20
CONF
systemctl restart postgresql
ok "postgres tuned for ${TOTAL_MB} MB RAM (shared_buffers=${BUFFERS}MB)"

if [ -n "${DB_PASSWORD:-}" ]; then
    log "Ensuring role and database exist"
    sudo -u postgres psql -tAc "select 1 from pg_roles where rolname='${DB_USER}'" \
        | grep -q 1 || sudo -u postgres psql -qc \
        "create role ${DB_USER} login password '${DB_PASSWORD}';"
    sudo -u postgres psql -tAc "select 1 from pg_database where datname='${DB_NAME}'" \
        | grep -q 1 || sudo -u postgres createdb -O "${DB_USER}" "${DB_NAME}"
    ok "role ${DB_USER} owns database ${DB_NAME} (owner bypasses RLS, as intended)"
else
    warn "DB_PASSWORD not set — skipping role/database creation"
fi

# ------------------------------------------------------------------- 8. code
log "Cloning the bot to ${APP_DIR}"
if [ -d "$APP_DIR/.git" ]; then
    git -C "$APP_DIR" fetch --quiet origin
    ok "repo already present (not overwriting local changes)"
else
    install -d -o "$APP_USER" -g "$APP_USER" "$APP_DIR"
    sudo -u "$APP_USER" git clone --quiet --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
    ok "cloned branch ${BRANCH}"
fi

log "Creating the virtualenv"
if [ -x "$APP_DIR/.venv/bin/python" ]; then
    ok "venv already exists"
else
    sudo -u "$APP_USER" python3 -m venv "$APP_DIR/.venv"
fi
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install -q --upgrade pip
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
ok "$("$APP_DIR/.venv/bin/python" -c 'import aiogram, asyncpg, apscheduler; print("aiogram", aiogram.__version__)')"

# ------------------------------------------------------- 8b. schema and banks
# Idempotent: only applies what is missing, so a re-run never trips the seeds'
# own double-load guard (which would abort the script under `set -e`).
if [ -n "${DB_PASSWORD:-}" ]; then
    export DATABASE_URL="postgresql://${DB_USER}:${DB_PASSWORD}@127.0.0.1:5432/${DB_NAME}"

    log "Applying the schema"
    if [ "$(psql "$DATABASE_URL" -tAc "select to_regclass('public.questions') is not null" 2>/dev/null || echo f)" = "t" ]; then
        ok "schema already applied"
    else
        psql "$DATABASE_URL" -q -f "$APP_DIR/schema.sql"
        ok "schema applied"
    fi

    log "Loading the question banks"
    LOADED=$(psql "$DATABASE_URL" -tAc "select count(*) from questions" 2>/dev/null || echo 0)
    if [ "${LOADED:-0}" -eq 0 ]; then
        for seed in "$APP_DIR"/seeds/*.sql; do
            psql "$DATABASE_URL" -q -f "$seed" >/dev/null
            ok "loaded $(basename "$seed")"
        done
    else
        ok "already loaded (${LOADED} questions)"
    fi

    log "Content in the database"
    psql "$DATABASE_URL" -c "select level, count(*) as questions from questions group by level order by level"
else
    warn "DB_PASSWORD not set — skipping schema and question banks"
fi

# ----------------------------------------------------------------- 9. systemd
log "Installing the systemd unit"
install -m 644 "$APP_DIR/deploy/studybot.service" /etc/systemd/system/studybot.service
systemctl daemon-reload
systemctl enable studybot >/dev/null 2>&1
ok "enabled"

# ------------------------------------------------------------------ 10. start
if [ -f "$APP_DIR/.env" ] && grep -qE '^BOT_TOKEN=.+' "$APP_DIR/.env"; then
    log "Starting the bot"
    systemctl restart studybot
    sleep 4
    ok "studybot is $(systemctl is-active studybot || true)"
    journalctl -u studybot -n 15 --no-pager || true
else
    warn "no usable ${APP_DIR}/.env yet — installed but not started"
fi

log "Done"
cat <<NEXT
    Next steps:
      1. write ${APP_DIR}/.env  (chmod 600, owner ${APP_USER})
      2. load the database:
           psql "\$DATABASE_URL" -f ${APP_DIR}/schema.sql
           for f in ${APP_DIR}/seeds/*.sql; do psql "\$DATABASE_URL" -f "\$f"; done
      3. systemctl start studybot && journalctl -u studybot -f

    Also worth doing:
      - DigitalOcean: add a cloud firewall allowing only 22/tcp in
      - a nightly pg_dump (see docs/SETUP.md phase 2.4)
NEXT
