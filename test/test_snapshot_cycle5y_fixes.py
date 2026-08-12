"""The redaction opt-out is a ceiling, so it does not live where the agent can write."""

from __future__ import annotations

import inspect
import json
import os
from pathlib import Path

import pytest

from kiro_crew import security
from kiro_crew import snapshot as snap
from kiro_crew import snapshot_redact as redact
from kiro_crew.config.loader import KiroCrewConfig


@pytest.fixture()
def home(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    return tmp_path


class TestTheSwitchIsBeyondTheAgentsReach:
    def test_it_lives_inside_the_fenced_backup_directory(self, home: Path) -> None:
        switch = redact.redaction_switch_path()
        assert switch.parent.name == "backup", switch
        # The fence classifies the DIRECTORY, so the leaf inherits it by living there.
        assert switch.parent.parent == home
        assert switch.name == "redaction.json"

    def test_the_file_gate_refuses_it(self, home: Path) -> None:
        """The layer that holds on every platform: containment, not pattern matching."""
        assert security.is_sensitive_path(str(redact.redaction_switch_path()))

    @pytest.mark.skipif(
        os.name != "posix",
        reason=(
            "The shell-form scan does not recognise a drive-letter data home, so it fires "
            "for no fenced file at all on Windows -- not this one, and not the deny list "
            "or the computer-use enable either. That is a limit of the shared scanner, "
            "proven by the companion test below, and asserting it here would claim "
            "protection the platform does not give."
        ),
    )
    @pytest.mark.parametrize("verb", ["cat {p}", "echo x > {p}", "tee {p}", "rm {p}"])
    def test_every_shell_form_refuses_it(self, home: Path, verb: str) -> None:
        """A read-only fence is not enough: flipping it is the attack, reading it is recon."""
        cmd = verb.format(p=redact.redaction_switch_path())
        assert security.is_sensitive_bash_command(cmd), cmd

    def test_the_shell_scan_treats_this_file_exactly_like_the_other_ceilings(
        self, home: Path
    ) -> None:
        """Pins the SHAPE of the coverage rather than one platform's answer.

        Whatever the shell scan does with the data home it is given, it must do the same
        for this switch as for the deny list and the computer-use enable. That keeps the
        skip above honest -- it records a shared-scanner limit rather than a hole specific
        to this file -- and it fails if this switch ever becomes the odd one out.
        """
        cfg = redact.redaction_switch_path().parent.parent
        peers = [cfg / "denied_commands.json", cfg / "computer_use.json"]
        mine = redact.redaction_switch_path()
        verdicts = {
            str(p): bool(security.is_sensitive_bash_command(f"cat {p}"))
            for p in [*peers, mine]
        }
        assert len(set(verdicts.values())) == 1, verdicts

    def test_it_is_not_a_config_field(self) -> None:
        """Two places to turn it off means the agent-writable one decides.

        The repo already keeps its other ceilings (the deny list, the computer-use enable)
        out of `config.json` for exactly this reason, and states that the config section
        must carry no enable field so there is only one place the thing can be switched.
        """
        assert not hasattr(KiroCrewConfig, "redact_backup_uploads")
        assert "redact_backup_uploads" not in inspect.getsource(snap)


class TestTheProtectiveAnswerNeedsNoFile:
    def _write(self, home: Path, payload: str) -> None:
        switch = redact.redaction_switch_path()
        switch.parent.mkdir(parents=True, exist_ok=True)
        switch.write_text(payload, encoding="utf-8")

    def test_absent_means_redacted(self, home: Path) -> None:
        assert redact.outbound_redaction_enabled() is True

    def test_an_explicit_opt_out_is_honoured(self, home: Path) -> None:
        self._write(home, json.dumps({"redact_uploads": False}))
        assert redact.outbound_redaction_enabled() is False

    @pytest.mark.parametrize(
        "payload",
        [
            "not json at all",
            "",
            "[]",
            json.dumps({}),
            json.dumps({"redact_uploads": "false"}),
            json.dumps({"redact_uploads": 0}),
            json.dumps({"other": True}),
        ],
    )
    def test_anything_but_a_clear_opt_out_redacts(self, home: Path, payload: str) -> None:
        """Only the exact boolean turns it off, so no near-miss can downgrade the default."""
        self._write(home, payload)
        assert redact.outbound_redaction_enabled() is True

    def test_an_unreadable_file_redacts(self, home: Path, monkeypatch) -> None:
        self._write(home, json.dumps({"redact_uploads": False}))

        def deny(*a, **k):
            raise OSError(13, "Permission denied")

        monkeypatch.setattr(Path, "read_text", deny)
        assert redact.outbound_redaction_enabled() is True

    def test_the_upload_asks_the_switch_not_the_config(self) -> None:
        src = inspect.getsource(snap._redacted_upload_copy)
        assert "outbound_redaction_enabled()" in src
        assert "KiroCrewConfig" not in src

    def test_a_failure_to_read_it_still_redacts_at_the_upload(self, home: Path, monkeypatch) -> None:
        """The upload's own fallback, independent of the reader's."""
        monkeypatch.setattr(
            redact, "outbound_redaction_enabled", lambda: (_ for _ in ()).throw(RuntimeError("x"))
        )
        src = inspect.getsource(snap._redacted_upload_copy)
        assert "redact = True" in src, "the upload must fail closed on an unreadable switch"
