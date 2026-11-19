"""Import the real gateway only AFTER installing an isolated launch home.

The native model command pins gateway.run._hermes_home at module import; a
per-test env patch after importing it would read the user's real provider config.
Each case gets an actual process, gateway, SessionAuthority and loopback provider.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

CASES = [
    "test_native_authority_allows_idle_resume_and_reads_back_route",
    "test_native_model_pin_is_durable_and_next_real_turn_uses_it",
    "test_busy_native_turn_rejects_switch_without_interrupting",
    "test_stale_menu_does_not_change_fresh_route_or_pin",
    "test_resume_rejects_foreign_owner_through_native_store",
    "test_shared_runtime_admission_cannot_overtake_native_model_commit",
    "test_new_menu_uses_native_reset_without_nested_lock_deadlock",
    "test_native_confirmation_rechecks_original_menu_session",
    "test_cancelled_menu_holds_admission_fence_until_native_worker_finishes",
    "test_native_model_cost_confirmation_is_preserved_and_fenced",
    "test_native_model_cost_confirmation_refuses_a_stale_route",
    "test_resume_rejects_target_owned_by_a_concurrent_native_claim",
]


@pytest.mark.parametrize("case", CASES)
def test_native_gateway_admission(tmp_path, case):
    home = tmp_path / "launch"
    home.mkdir()
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("HERMES_", "OPENAI_", "OPENROUTER_", "ANTHROPIC_"))
    }
    env.update(
        HERMES_HOME=str(home),
        NATIVE_MENU_TEST_HOME=str(home),
        HERMES_DISABLE_LAZY_INSTALLS="1",
        HERMES_PROFILE="default",
        FEISHU_ALLOWED_USERS="tenant",
    )
    result = subprocess.run(
        [sys.executable, "-m", "pytest", f"tests/native_menu_gateway_cases.py::{case}", "-q", "-s"],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        timeout=100,
    )
    assert result.returncode == 0, result.stdout + result.stderr
