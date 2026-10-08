"""tools/infra/update.sh against a local remote, with runuser, systemctl, curl, npm and pip stubbed: it fast-forwards
to main only, rebuilds what changed, checks server.env's paths, restarts, health-checks and rolls back on failure.
Runs wherever bash and git do (Git for Windows' bash on Windows)."""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "tools" / "infra" / "update.sh"
UNIT = (REPO / "tools" / "infra" / "bidmate.service").read_text(encoding="utf-8")


def bash() -> str | None:
    if os.name == "nt":  # System32's bash.exe is WSL's; use the one shipped with Git
        git = shutil.which("git")
        found = git and next((p / "bin" / "bash.exe" for p in Path(git).resolve().parents
                              if (p / "bin" / "bash.exe").is_file()), None)
        return str(found) if found else None
    return shutil.which("bash")


def unix(path: Path) -> str:
    """An absolute path as server.env on the VM holds it: starting with `/` (Git bash reads C: as /c)."""
    return f"/{path.drive[0].lower()}{path.as_posix()[2:]}" if path.drive else path.as_posix()


STUBS = {
    # runuser -u <user> -- cmd...: the test runs everything as the current user
    "runuser": 'shift 3; exec "$@"\n',
    "systemctl": 'echo "$*" >>"$STUB_LOG/systemctl"\n[ "$1 $2" = "restart bidmate" ] && [ -f "$STUB_LOG/restart-fails" ] && exit 1\nexit 0\n',
    "curl": 'cat "$STUB_LOG/health" 2>/dev/null || printf 000\n',
    # npm-kills: SIGKILL the updater (the parent of `sh -c "... npm ci ..."`), as its unit's TimeoutStartSec does
    "npm": 'echo "npm $*" >>"$STUB_LOG/npm"\n'
           '[ -f "$STUB_LOG/npm-kills" ] && { read -r _ _ _ up _ </proc/$PPID/stat; kill -9 "$up"; exit 1; }\n'
           '[ "$1" = run ] && { mkdir -p out; echo new >out/index.html; }\n[ -f "$STUB_LOG/npm-fails" ] && exit 1\nexit 0\n',
    "chown": "exit 0\n",
    # cp -a <app>/web/out <backup> fails, copying nothing, while $STUB_LOG/backup-fails exists (a full disk)
    "cp": 'case "$2" in */web/out) [ -f "$STUB_LOG/backup-fails" ] && exit 1 ;; esac\ncommand -p cp "$@"\n',
}


@unittest.skipUnless(bash() and shutil.which("git"), "needs bash and git")
class UpdateScriptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.root = root
        self.remote, self.app, self.work = root / "remote.git", root / "app", root / "work"
        self.state, self.stub_log = root / "state", root / "log"
        self.stub_log.mkdir()
        stubs = root / "stubs"
        stubs.mkdir()
        for name, body in STUBS.items():
            (stubs / name).write_text("#!/bin/sh\n" + body, encoding="utf-8", newline="\n")
        venv = root / "venv" / "bin"
        venv.mkdir(parents=True)
        (venv / "pip").write_text('#!/bin/sh\necho "pip $*" >>"$STUB_LOG/pip"\nexit 0\n', encoding="utf-8", newline="\n")
        self.request = root / "request" / "requested"
        self.request.parent.mkdir()
        self.config = root / "config.json"
        self.config.write_text("{}", encoding="utf-8")
        self.env_file = root / "server.env"
        self.env_file.write_text(f"# comment\nRFP_CONFIG_FILE={unix(self.config)}\n"
                                 f"RFP_PATH_MAP=C:/Users/owner/project={unix(root)}\n"
                                 f"BIDMATE_UPDATE_REQUEST={unix(self.request)}\n"  # consumed before the check
                                 "BIDMATE_HUB_URL=http://35.255.64.243:8000\n", encoding="utf-8", newline="\n")
        self.git("init", "--quiet", "--bare", "--initial-branch=main", str(self.remote), cwd=root)
        self.git("clone", "--quiet", str(self.remote), str(self.work), cwd=root)
        self.commit({"README.md": "one\n", "tools/infra/bidmate.service": UNIT}, "first")
        self.git("clone", "--quiet", str(self.remote), str(self.app), cwd=root)
        (self.app / "web" / "out").mkdir(parents=True)
        (self.app / "web" / "out" / "index.html").write_text("old\n", encoding="utf-8")
        self.prev = self.head(self.app)
        self.env = {**os.environ, "BIDMATE_APP": self.app.as_posix(), "BIDMATE_VENV": (root / "venv").as_posix(),
                    "BIDMATE_USER": "bidmate", "BIDMATE_HOME": root.as_posix(),
                    "BIDMATE_ENV_FILE": self.env_file.as_posix(), "BIDMATE_UPDATE_REQUEST": self.request.as_posix(),
                    "BIDMATE_UPDATE_STATE_DIR": self.state.as_posix(),
                    "BIDMATE_REMOTE": self.remote.as_posix(), "BIDMATE_HEALTH_SECONDS": "1",
                    "STUB_LOG": self.stub_log.as_posix(), "STUB_DIR": stubs.as_posix()}

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args, cwd=None) -> str:
        return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd or self.work,
                              check=True, capture_output=True, text=True).stdout.strip()

    def head(self, where: Path) -> str:
        return self.git("rev-parse", "HEAD", cwd=where)

    def commit(self, files: dict[str, str], message: str) -> str:
        for name, text in files.items():
            path = self.work / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="\n")
        self.git("add", "-A")
        self.git("commit", "--quiet", "-m", message)
        self.git("push", "--quiet", "origin", "HEAD:main")
        return self.head(self.work)

    def run_update(self, health: str = "401", finished: bool = True) -> dict | None:
        (self.stub_log / "health").write_text(health, encoding="utf-8")
        self.request.write_text("{}", encoding="utf-8")
        # Git for Windows' bash puts its own bin directories first, so the stubs go in front from inside bash.
        subprocess.run([bash(), "-c", 'd=$STUB_DIR; command -v cygpath >/dev/null && d=$(cygpath -u "$d")\n'
                                      'PATH="$d:$PATH" exec "$BASH" "$0"', SCRIPT.as_posix()],
                       env=self.env, capture_output=True, timeout=120)
        self.assertFalse(self.request.exists(), "the marker is consumed")
        self.log = (self.state / "update.log").read_text(encoding="utf-8", errors="replace")
        if not finished:  # killed: its result still says running
            return None
        return json.loads((self.state / "result.json").read_text(encoding="utf-8"))

    def calls(self, name: str) -> str:
        path = self.stub_log / name
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def test_a_newer_main_is_fast_forwarded_built_and_restarted(self):
        target = self.commit({"web/src/page.tsx": "x\n", "pyproject.toml": "[project]\n"}, "screens and deps")
        result = self.run_update()
        self.assertEqual(result["state"], "succeeded", self.log)
        self.assertEqual((result["from_commit"], result["to_commit"]), (self.prev, target))
        self.assertEqual(self.head(self.app), target)
        self.assertIn("npm ci", self.calls("npm"))
        self.assertIn("npm run build", self.calls("npm"))
        self.assertIn("install --quiet -e", self.calls("pip"))
        self.assertIn("restart bidmate", self.calls("systemctl"))
        self.assertNotIn("daemon-reload", self.calls("systemctl"))
        self.assertFalse((self.state / "deploying").exists())

    def test_a_failed_health_check_restores_the_previous_commit_and_build(self):
        target = self.commit({"web/src/page.tsx": "x\n"}, "breaks startup")
        result = self.run_update(health="502")
        self.assertEqual(result["state"], "rollback_failed", self.log)  # the old version is just as unhealthy here
        self.assertEqual(self.head(self.app), self.prev)
        self.assertEqual((self.app / "web" / "out" / "index.html").read_text(encoding="utf-8"), "old\n")
        self.assertEqual(self.calls("systemctl").count("restart bidmate"), 2)
        result = self.run_update()  # the next run starts again from the same commit and saved screens
        self.assertEqual((result["state"], result["from_commit"], result["to_commit"]),
                         ("succeeded", self.prev, target), self.log)

    def test_a_rollback_that_comes_back_healthy_says_rolled_back(self):
        self.commit({"web/src/page.tsx": "x\n"}, "screens")
        (self.stub_log / "npm-fails").write_text("", encoding="utf-8")
        result = self.run_update()
        self.assertEqual(result["state"], "rolled_back", self.log)
        self.assertIn("화면을 빌드하지 못했습니다", result["message"])
        self.assertEqual(self.head(self.app), self.prev)
        self.assertEqual((self.app / "web" / "out" / "index.html").read_text(encoding="utf-8"), "old\n")

    def test_a_changed_service_unit_is_never_installed_and_nothing_is_deployed(self):
        # systemd takes the last User= and ignores spaces around `=`: this unit runs as root
        unit = UNIT.replace("User=bidmate\n", "User=bidmate\nUser = root\n")
        self.commit({"tools/infra/bidmate.service": unit, "web/src/page.tsx": "x\n"}, "root unit")
        result = self.run_update()
        self.assertEqual(result["state"], "failed", self.log)
        self.assertIn("bidmate.service", result["message"])
        self.assertEqual(self.head(self.app), self.prev)
        self.assertEqual((self.calls("npm"), self.calls("systemctl")), ("", ""))

    def test_a_failed_backup_changes_nothing(self):
        self.commit({"web/src/page.tsx": "x\n"}, "screens")
        (self.stub_log / "backup-fails").write_text("", encoding="utf-8")
        (self.stub_log / "npm-fails").write_text("", encoding="utf-8")
        result = self.run_update()
        self.assertEqual(result["state"], "failed", self.log)
        self.assertEqual(self.head(self.app), self.prev)
        self.assertEqual((self.app / "web" / "out" / "index.html").read_text(encoding="utf-8"), "old\n")
        self.assertEqual((self.calls("npm"), self.calls("systemctl")), ("", ""))

    def test_a_run_cut_off_after_the_fast_forward_is_finished_by_the_next_one(self):
        target = self.commit({"web/src/page.tsx": "x\n"}, "screens")
        (self.stub_log / "npm-kills").write_text("", encoding="utf-8")
        self.run_update(finished=False)
        self.assertEqual(self.head(self.app), target)  # fast-forwarded, never built or restarted
        self.assertEqual(self.calls("systemctl"), "")
        (self.stub_log / "npm-kills").unlink()
        result = self.run_update()
        self.assertEqual(result["state"], "succeeded", self.log)
        self.assertEqual((result["from_commit"], result["to_commit"]), (self.prev, target))
        self.assertIn("npm run build", self.calls("npm"))
        self.assertIn("restart bidmate", self.calls("systemctl"))
        self.assertEqual(self.run_update()["state"], "up_to_date", self.log)  # nothing left to finish

    def test_a_cut_off_run_that_fails_again_rolls_back_to_the_commit_before_it(self):
        self.commit({"web/src/page.tsx": "x\n"}, "screens")
        (self.stub_log / "npm-kills").write_text("", encoding="utf-8")
        self.run_update(finished=False)
        (self.stub_log / "npm-kills").unlink()
        remote, self.env["BIDMATE_REMOTE"] = self.env["BIDMATE_REMOTE"], (self.root / "gone.git").as_posix()
        result = self.run_update()  # GitHub unreachable: still unfinished, said so, and kept for the next run
        self.assertEqual(result["state"], "interrupted", self.log)
        self.assertEqual(result["from_commit"], self.prev)
        self.env["BIDMATE_REMOTE"] = remote
        (self.stub_log / "npm-fails").write_text("", encoding="utf-8")
        result = self.run_update()
        self.assertEqual(result["state"], "rolled_back", self.log)
        self.assertEqual(self.head(self.app), self.prev)
        self.assertEqual((self.app / "web" / "out" / "index.html").read_text(encoding="utf-8"), "old\n")

    def test_a_missing_server_env_path_rolls_back(self):
        self.commit({"README.md": "two\n"}, "docs")
        self.config.unlink()
        result = self.run_update()
        self.assertEqual(result["state"], "rolled_back", self.log)
        self.assertIn("RFP_CONFIG_FILE", self.log)
        self.assertEqual(self.head(self.app), self.prev)

    def test_no_fast_forward_and_nothing_new_change_nothing(self):
        result = self.run_update()
        self.assertEqual(result["state"], "up_to_date", self.log)
        (self.app / "local.txt").write_text("local\n", encoding="utf-8")
        self.git("add", "local.txt", cwd=self.app)
        self.git("commit", "--quiet", "-m", "local only", cwd=self.app)
        local = self.head(self.app)
        self.commit({"README.md": "two\n"}, "main moved on")
        result = self.run_update()
        self.assertEqual(result["state"], "failed", self.log)
        self.assertIn("빨리 감기할 수 없어", result["message"])
        self.assertEqual(self.head(self.app), local)
        self.assertEqual(self.calls("systemctl"), "")


if __name__ == "__main__":
    unittest.main()
