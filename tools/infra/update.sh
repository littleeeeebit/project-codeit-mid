#!/bin/bash
# Moves the BidMate checkout to the latest GitHub main and restarts the service, rolling back when it fails
# (runbook 3.2). Run as root by bidmate-update.service when the app writes its request marker; never by the app.
#
# Installed as a root-owned copy (/usr/local/sbin/bidmate-update), not run from the checkout: the checkout belongs
# to the service user, and root must not execute what that user can edit. For the same reason it reads nothing
# from the marker but its existence, runs git, pip and npm as the service user, and installs a changed
# bidmate.service only when it still runs as that user.
#
# Every run writes $STATE/result.json (what the header shows) and $STATE/update.log (the previous run's log is
# kept as update.log.1). The paths below are the defaults on `codeit`; the variables exist for a dry run elsewhere.
set -uo pipefail

APP=${BIDMATE_APP:-/srv/bidmate/app}
VENV=${BIDMATE_VENV:-/srv/bidmate/venv}
APP_USER=${BIDMATE_USER:-bidmate}
APP_HOME=${BIDMATE_HOME:-/srv/bidmate}
ENV_FILE=${BIDMATE_ENV_FILE:-/etc/bidmate/server.env}
REQUEST=${BIDMATE_UPDATE_REQUEST:-/srv/bidmate/update-request/requested}
STATE=${BIDMATE_UPDATE_STATE_DIR:-/var/lib/bidmate-update}
UNIT=${BIDMATE_UNIT:-/etc/systemd/system/bidmate.service}
REMOTE=${BIDMATE_REMOTE:-https://github.com/littleeeeebit/project-codeit-mid.git}
HEALTH=${BIDMATE_HEALTH_URL:-http://127.0.0.1:8501/api/info}
HEALTH_SECONDS=${BIDMATE_HEALTH_SECONDS:-300}

mkdir -p "$STATE"
chmod 755 "$STATE"
[ -f "$STATE/update.log" ] && mv -f "$STATE/update.log" "$STATE/update.log.1"
exec >>"$STATE/update.log" 2>&1

now() { date -u +%Y-%m-%dT%H:%M:%S+00:00; }
log() { echo "[$(now)] $*"; }
as_app() { runuser -u "$APP_USER" -- env HOME="$APP_HOME" "$@"; }
git_app() { as_app git -C "$APP" "$@"; }

STARTED=$(now)
PREV=""
TARGET=""
result() {  # state, message: the only file the app reads from this run
    local tmp="$STATE/result.json.tmp"
    printf '{"state": "%s", "from_commit": "%s", "to_commit": "%s", "started_at": "%s", "finished_at": %s, "message": "%s"}\n' \
        "$1" "$PREV" "$TARGET" "$STARTED" "$([ "$1" = running ] && echo null || echo "\"$(now)\"")" "$2" >"$tmp"
    chmod 644 "$tmp"
    mv -f "$tmp" "$STATE/result.json"
    log "result: $1 - $2"
}

log "update requested; checkout $APP"
result running "업데이트를 진행하고 있습니다."
# rm never follows a symlink: whatever the service user put there, only the name goes.
rm -f "$REQUEST"

fail() {  # before anything changed
    result failed "$1"
    exit 1
}

PREV=$(git_app rev-parse HEAD) || fail "실행 중인 커밋을 읽지 못했습니다."
[ -z "$(git_app status --porcelain --untracked-files=no)" ] || fail "체크아웃에 커밋되지 않은 변경이 있어 업데이트하지 않았습니다."
# The URL is fixed here, not read from the checkout's config, so origin/main is always GitHub's main.
git_app fetch --quiet "$REMOTE" "+refs/heads/main:refs/remotes/origin/main" || fail "GitHub에서 main을 가져오지 못했습니다."
TARGET=$(git_app rev-parse refs/remotes/origin/main) || fail "origin/main을 읽지 못했습니다."
if [ "$PREV" = "$TARGET" ]; then
    result up_to_date "이미 최신 main을 실행하고 있습니다."
    exit 0
fi
git_app merge-base --is-ancestor "$PREV" "$TARGET" || fail "실행 중인 커밋에서 main으로 빨리 감기할 수 없어 업데이트하지 않았습니다."
CHANGED=$(git_app diff --name-only "$PREV" "$TARGET") || fail "바뀐 파일 목록을 읽지 못했습니다."
changed() { grep -qE "$1" <<<"$CHANGED"; }
log "updating $PREV -> $TARGET; changed files: $(wc -l <<<"$CHANGED")"

# What a rollback restores: the built screens and the installed unit.
rm -rf "$STATE/web-out.prev" "$STATE/bidmate.service.prev"
[ -d "$APP/web/out" ] && cp -a "$APP/web/out" "$STATE/web-out.prev"
cp -a "$UNIT" "$STATE/bidmate.service.prev"

install_deps() { as_app "$VENV/bin/pip" install --quiet -e "$APP"; }  # the VM venv follows pyproject.toml, no torch
build_web() { as_app sh -c "cd '$APP/web' && npm ci --no-audit --no-fund && npm run build"; }

# A unit from the checkout is installed as root's; it must still run as the service user, and nothing in it as root.
install_unit() {
    local unit="$APP/tools/infra/bidmate.service"
    if ! grep -qx "User=$APP_USER" "$unit" || grep -qE '^(User|Group)=(root|0)$|^Exec[A-Za-z]*=[^/]*[+!]' "$unit"; then
        log "refusing $unit: it must run as $APP_USER and nothing as root"
        return 1
    fi
    install -m 644 "$unit" "$UNIT" && systemctl daemon-reload
}

# Every absolute path server.env names must exist in the new checkout (runbook 3.6 step 3: a rename broke a start).
check_env_paths() {
    local key value path ok=0
    while IFS='=' read -r key value; do
        case "$key" in ''|\#*) continue ;; esac
        value=${value%$'\r'}; value=${value%\"}; value=${value#\"}; value=${value%\'}; value=${value#\'}
        path=${value##*=}  # RFP_PATH_MAP is <windows path>=<linux path>: the Linux side
        case "$path" in
            /*) [ -e "$path" ] || { log "server.env $key names a missing path: $path"; ok=1; } ;;
        esac
    done <"$ENV_FILE"
    return $ok
}

healthy() {  # the API answers, and still refuses a visitor who has not signed in
    local deadline=$((SECONDS + HEALTH_SECONDS)) code
    while [ $SECONDS -lt $deadline ]; do
        code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$HEALTH")
        [ "$code" = 401 ] && return 0
        sleep 3
    done
    log "no 401 from $HEALTH within ${HEALTH_SECONDS}s (last answer: ${code:-none})"
    return 1
}

DEPS=0
UNIT_CHANGED=0
rollback() {
    log "rolling back to $PREV: $1"
    local ok=0
    git_app reset --quiet --hard "$PREV" || ok=1
    if [ -d "$STATE/web-out.prev" ]; then
        rm -rf "$APP/web/out" && cp -a "$STATE/web-out.prev" "$APP/web/out" && chown -R "$APP_USER:" "$APP/web/out" || ok=1
    fi
    [ $DEPS = 1 ] && { install_deps || ok=1; }
    if [ $UNIT_CHANGED = 1 ]; then
        install -m 644 "$STATE/bidmate.service.prev" "$UNIT" && systemctl daemon-reload || ok=1
    fi
    systemctl restart bidmate || ok=1
    if [ $ok = 0 ] && healthy; then
        result rolled_back "$1 이전 커밋으로 되돌렸습니다."
    else
        result rollback_failed "$1 되돌리기도 실패했습니다. 서버에서 직접 확인해야 합니다 (journalctl -u bidmate, $STATE/update.log)."
    fi
    exit 1
}

git_app merge --quiet --ff-only "$TARGET" || fail "main으로 빨리 감기하지 못했습니다."
if changed '^(pyproject\.toml|requirements[^/]*\.txt)$'; then
    DEPS=1
    log "dependencies changed: pip install -e"
    install_deps || rollback "의존성을 설치하지 못했습니다."
fi
if changed '^web/'; then
    log "web/ changed: npm ci && npm run build"
    build_web || rollback "화면을 빌드하지 못했습니다."
fi
if changed '^tools/infra/bidmate\.service$'; then
    UNIT_CHANGED=1
    log "bidmate.service changed: installing it"
    install_unit || rollback "새 bidmate.service를 설치하지 못했습니다."
fi
check_env_paths || rollback "server.env가 가리키는 경로가 새 버전에 없습니다."
NOTE=""
if changed '^tools/infra/(update\.sh|bidmate-update\.(service|path))$'; then
    NOTE=" 업데이트 장치 자체가 바뀌었으니 런북 3.2대로 다시 설치하세요."
fi
log "restarting bidmate"
systemctl restart bidmate || rollback "bidmate를 다시 시작하지 못했습니다."
healthy || rollback "새 버전이 응답하지 않았습니다."
rm -rf "$STATE/web-out.prev"
result succeeded "main ${TARGET:0:7}(으)로 업데이트했습니다.$NOTE"
