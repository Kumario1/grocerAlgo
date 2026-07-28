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
    public = HEBClient(database_path=tmp_path / "public.sqlite")
    onboarding = HEBClient(
        6,
        database_path=tmp_path / "onboarding.sqlite",
        allow_unsupported=True,
    )
    context = Context()
    onboarding._contexts[6] = context

    try:
        public._store(6)
    except HEBConnectionError:
        pass
    else:
        raise AssertionError("public client accepted an unsupported store")

    asyncio.run(onboarding.select_store(6))

    assert {cookie["name"]: cookie["value"] for cookie in context.cookies} == {
        "CURR_SESSION_STORE": "6",
        "SHOPPING_STORE_ID": "6",
        "USER_SELECT_STORE": "false",
    }


def test_pipeline_requires_live_verification_after_offline_pass(tmp_path):
    shutil.copy("pipeline.sh", tmp_path)
    fake = tmp_path / "python"
    fake.write_text("""#!/bin/sh
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
        env=os.environ | {"PIPE_PYTHON": str(fake)},
    )

    assert result.returncode == 1
    assert (tmp_path / "calls").read_text().splitlines() == [
        "calibrate.py 6",
        "calibrate.py 6 --verify",
    ]
