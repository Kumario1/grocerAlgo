import asyncio
import os
import shutil
import subprocess

from router.heb import HEBClient, HEBConnectionError


class Context:
    def __init__(self):
        self.cookies = None

    async def add_cookies(self, cookies):
        self.cookies = cookies


def test_onboarding_selects_an_unsupported_store_without_publicly_enabling_it(
        tmp_path):
    store = 1234
    public = HEBClient(database_path=tmp_path / "public.sqlite")
    onboarding = HEBClient(
        store,
        database_path=tmp_path / "onboarding.sqlite",
        allow_unsupported=True,
    )
    context = Context()
    onboarding._contexts[store] = context

    try:
        public._store(store)
    except HEBConnectionError:
        pass
    else:
        raise AssertionError("public client accepted an unsupported store")

    asyncio.run(onboarding.select_store(store))

    assert {cookie["name"]: cookie["value"] for cookie in context.cookies} == {
        "CURR_SESSION_STORE": str(store),
        "SHOPPING_STORE_ID": str(store),
        "USER_SELECT_STORE": "false",
    }


def test_pipeline_requires_live_verification_after_offline_pass(tmp_path):
    shutil.copy("pipeline.sh", tmp_path)
    fake = tmp_path / "python"
    fake.write_text("""#!/bin/sh
echo "${GROCER_ADMIN_TOKEN-unset}|${GROCER_PROD_URL-unset}" >> environment
case $1 in
  capture_atlas.py) exit 0 ;;
  calibrate.py)
    echo "$*" >> calls
    [ "$3" = --verify ] && exit 1
    exit 0 ;;
esac
""")
    fake.chmod(0o755)

    result = subprocess.run(
        ["./pipeline.sh", "6", "--from", "6"],
        cwd=tmp_path,
        env=os.environ | {
            "PIPE_PYTHON": str(fake),
            "GROCER_ADMIN_TOKEN": "must-not-leak",
            "GROCER_PROD_URL": "https://prod.example",
        },
    )

    assert result.returncode == 1
    assert (tmp_path / "environment").read_text().splitlines() == [
        "unset|unset",
        "unset|unset",
        "unset|unset",
    ]
    assert (tmp_path / "calls").read_text().splitlines() == [
        "calibrate.py 6",
        "calibrate.py 6 --verify",
    ]


def test_codex_agent_resumes_the_same_session_after_capacity(tmp_path):
    fake = tmp_path / "codex"
    fake.write_text("""#!/bin/sh
echo "$*" >> "$FAKE_LOG"
case " $* " in
  *" exec resume "*)
    echo "session id: 00000000-0000-0000-0000-000000000231"
    echo "AUDIT CLEAN"
    exit 0
    ;;
esac
cat > "$FAKE_PROMPT"
echo "session id: 00000000-0000-0000-0000-000000000231"
echo "ERROR: Selected model is at capacity. Please try a different model." >&2
exit 1
""")
    fake.chmod(0o755)
    log = tmp_path / "calls"
    prompt = tmp_path / "prompt"

    result = subprocess.run(
        ["./scripts/codex_agent.sh"],
        input="runbook\n",
        text=True,
        capture_output=True,
        env=os.environ | {
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "FAKE_LOG": str(log),
            "FAKE_PROMPT": str(prompt),
            "PIPE_CODEX_CAPACITY_WAIT": "0",
        },
    )

    assert result.returncode == 0
    assert prompt.read_text() == "runbook\n"
    assert len(log.read_text().splitlines()) == 2
    assert "exec resume 00000000-0000-0000-0000-000000000231" in log.read_text()
    assert "-c sandbox_mode=workspace-write" in log.read_text()
    assert "AUDIT CLEAN" in result.stdout
