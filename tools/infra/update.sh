#!/bin/bash
# Moves the BidMate checkout to the latest GitHub main and restarts the service, rolling back when it fails
# (runbook 3.2). Run as root by bidmate-update.service when the app writes its request marker; never by the app.
#
# Installed as a root-owned copy (/usr/local/sbin/bidmate-update), not run from the checkout: the checkout belongs
# to the service user, and root must not execute what that user can edit. For the same reason it reads nothing
# from the marker but its existence, runs git, pip and npm as the service user, and never installs a unit from the
# checkout: a changed bidmate.service is refused, for root to review and install by hand.
#
# A run that fast-forwarded records the commit it came from in $STATE/deploying until it succeeds or rolls back.
# A run that finds that file was cut off (the unit's TimeoutStartSec, a reboot): it deploys from the recorded
# commit again, rebuilding and restarting, and rolls back to it on failure, instead of taking an equal commit as
# done.
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
REMOTE=${BIDMATE_REMOTE:-https://github.com/littleeeeebit/project-codeit-mid.git}
HEALTH=${BIDMATE_HEALTH_URL:-http://127.0.0.1:8501/api/info}
HEALTH_SECONDS=${BIDMATE_HEALTH_SECONDS:-300}
PENDING=$STATE/deploying
BACKUP=$STATE/web-out.prev

mkdir -p "$STATE" && chmod 755 "$STATE" || exit 1
[ -f "$STATE/update.log" ] && mv -f "$STATE/update.log" "$STATE/update.log.1"
exec >>"$STATE/update.log" 2>&1

now() { date -u +%Y-%m-%dT%H:%M:%S+00:00; }
log() { echo "[$(now)] $*"; }
as_app() { runuser -u "$APP_USER" -- env HOME="$APP_HOME" "$@"; }
git_app() { as_app git -C "$APP" "$@"; }

STARTED=$(now)
PREV=""
TARGET=""
RECOVERING=0
result() {  # state, message: the only file the app reads from this run
    local tmp="$STATE/result.json.tmp"
    printf '{"state": "%s", "from_commit": "%s", "to_commit": "%s", "started_at": "%s", "finished_at": %s, "message": "%s"}\n' \
        "$1" "$PREV" "$TARGET" "$STARTED" "$([ "$1" = running ] && echo null || echo "\"$(now)\"")" "$2" >"$tmp" \
        && chmod 644 "$tmp" && mv -f "$tmp" "$STATE/result.json" || { log "could not write result.json ($1)"; return 1; }
    log "result: $1 - $2"
}

fail() {  # before this run changed anything
    if [ $RECOVERING = 1 ]; then  # the cut-off run's changes are still there; the next run takes them up again
        result interrupted "$1 중단된 지난 업데이트(${PREV:0:7}에서 시작)를 아직 마무리하지 못했습니다. 다시 업데이트하면 이어서 진행합니다."
    else
        result failed "$1"
    fi
    exit 1
}

install_deps() { as_app "$VENV/bin/pip" install --quiet -e "$APP"; }  # the VM venv follows pyproject.toml, no torch
build_web() { as_app sh -c "cd '$APP/web' && npm ci --no-audit --no-fund && npm run build"; }

# Every absolute path server.env names must exist in the new checkout (runbook 3.6 step 3: a rename broke a start).
check_env_paths() {
    local key value path ok=0
    while IFS='=' read -r key value; do
        case "$key" in ''|\#*) continue ;; esac
        value=${value%$'\r'}; value=${value%\"}; value=${value#\"}; value=${value%\'}; value=${value#\'}
        path=${value##*=}  # RFP_PATH_MAP is <windows path>=<linux path>: the Linux side
        [ "$key" = BIDMATE_UPDATE_REQUEST ] && path=$(dirname "$path")  # the marker itself is gone by now
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
rollback() {
    log "rolling back to $PREV: $1"
    local ok=0
    git_app reset --quiet --hard "$PREV" || ok=1
    if [ -d "$BACKUP" ]; then
        rm -rf "$APP/web/out" && cp -a "$BACKUP" "$APP/web/out" && chown -R "$APP_USER:" "$APP/web/out" || ok=1
    else
        log "no saved screens at $BACKUP"
        ok=1
    fi
    [ $DEPS = 1 ] && { install_deps || ok=1; }
    systemctl restart bidmate || ok=1
    if [ $ok = 0 ] && healthy; then
        rm -f "$PENDING"
        result rolled_back "$1 이전 커밋으로 되돌렸습니다."
    else  # $PENDING stays: the next run starts again from the same commit and the same saved screens
        result rollback_failed "$1 되돌리기도 실패했습니다. 서버에서 직접 확인해야 합니다 (journalctl -u bidmate, $STATE/update.log)."
    fi
    exit 1
}

log "update requested; checkout $APP"
# `running` is on disk before the marker goes, and the app reads the marker before the result
# (UpdateWatch.in_progress), so its paid-work fence never finds neither. Without the result the marker stays, and
# with it the fence, for someone to look. rm never follows a symlink: whatever the service user put there, only
# the name goes.
result running "업데이트를 진행하고 있습니다." || exit 1
rm -f "$REQUEST"

if [ -f "$PENDING" ]; then
    PREV=$(<"$PENDING")
    [[ $PREV =~ ^[0-9a-f]{40}$ ]] && [ -d "$BACKUP" ] \
        || { PREV=""; fail "$PENDING 또는 $BACKUP이 온전하지 않습니다. 서버에서 직접 확인해야 합니다."; }
    RECOVERING=1
    DEPS=1  # the cut-off run may have installed its dependencies: any rollback from here reinstalls $PREV's
    log "a run that fast-forwarded from $PREV never finished: deploying from there again"
else
    PREV=$(git_app rev-parse HEAD) || fail "실행 중인 커밋을 읽지 못했습니다."
fi
[ -z "$(git_app status --porcelain --untracked-files=no)" ] || fail "체크아웃에 커밋되지 않은 변경이 있어 업데이트하지 않았습니다."
# The URL is fixed here, not read from the checkout's config, so origin/main is always GitHub's main.
git_app fetch --quiet "$REMOTE" "+refs/heads/main:refs/remotes/origin/main" || fail "GitHub에서 main을 가져오지 못했습니다."
TARGET=$(git_app rev-parse refs/remotes/origin/main) || fail "origin/main을 읽지 못했습니다."
if [ "$PREV" = "$TARGET" ] && [ $RECOVERING = 0 ]; then
    result up_to_date "이미 최신 main을 실행하고 있습니다."
    exit 0
fi
git_app merge-base --is-ancestor "$PREV" "$TARGET" || fail "실행 중인 커밋에서 main으로 빨리 감기할 수 없어 업데이트하지 않았습니다."
CHANGED=$(git_app diff --name-only "$PREV" "$TARGET") || fail "바뀐 파일 목록을 읽지 못했습니다."
changed() { grep -qE "$1" <<<"$CHANGED"; }
log "updating $PREV -> $TARGET; changed files: $(wc -l <<<"$CHANGED")"
# Root would run what the service user's checkout says (User=, ExecStartPre=): only a person installs a unit.
if changed '^tools/infra/bidmate\.service$'; then
    fail "bidmate.service가 바뀌어 자동으로 업데이트하지 않았습니다. 런북 3.2의 수동 업데이트로 root가 검토해 설치해야 합니다."
fi

# What a rollback restores. Nothing changes until the screens are saved whole and the start commit is on disk; a
# cut-off run saved them already, and they are still the screens of $PREV.
if [ $RECOVERING = 0 ]; then
    [ -d "$APP/web/out" ] || fail "되돌릴 화면 빌드($APP/web/out)가 없어 업데이트하지 않았습니다."
    rm -rf "$BACKUP" && cp -a "$APP/web/out" "$BACKUP" || fail "되돌릴 화면 빌드를 저장하지 못해 업데이트하지 않았습니다."
    printf '%s\n' "$PREV" >"$PENDING.tmp" && mv -f "$PENDING.tmp" "$PENDING" || fail "진행 기록을 남기지 못해 업데이트하지 않았습니다."
fi

git_app merge --quiet --ff-only "$TARGET" || rollback "main으로 빨리 감기하지 못했습니다."
# $PREV...$TARGET does not say what a cut-off run already installed or built (main may have reverted it since):
# after one, both are redone (DEPS is already 1 then).
if [ $RECOVERING = 1 ] || changed '^(pyproject\.toml|requirements[^/]*\.txt)$'; then
    DEPS=1
    log "dependencies changed or a cut-off run may have changed them: pip install -e"
    install_deps || rollback "의존성을 설치하지 못했습니다."
fi
if [ $RECOVERING = 1 ] || changed '^web/'; then
    log "web/ changed or a cut-off run may have built it: npm ci && npm run build"
    build_web || rollback "화면을 빌드하지 못했습니다."
fi
check_env_paths || rollback "server.env가 가리키는 경로가 새 버전에 없습니다."
NOTE=""
if changed '^tools/infra/(update\.sh|bidmate-update\.(service|path))$'; then
    NOTE=" 업데이트 장치 자체가 바뀌었으니 런북 3.2대로 다시 설치하세요."
fi
log "restarting bidmate"
systemctl restart bidmate || rollback "bidmate를 다시 시작하지 못했습니다."
healthy || rollback "새 버전이 응답하지 않았습니다."
rm -f "$PENDING"
rm -rf "$BACKUP"
result succeeded "main ${TARGET:0:7}(으)로 업데이트했습니다.$NOTE"
