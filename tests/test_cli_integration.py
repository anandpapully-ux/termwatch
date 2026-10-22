"""End-to-end tests: real `termwatch run` subprocesses against a real uvicorn server."""

from __future__ import annotations

import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Any

import pytest

from .conftest import CLI, LiveServer, cli_env, free_port, make_settings, start_live_server, wait_for

PY = sys.executable


def run_cli(home: Path, server: LiveServer | None, *args: str, timeout: float = 60) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [*CLI, "run", *args],
        env=cli_env(home, server.url if server else None),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=timeout,
    )


def start_cli(home: Path, url: str, *args: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [*CLI, "run", *args],
        env=cli_env(home, url),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def sessions_named(server: LiveServer, name: str) -> list[dict[str, Any]]:
    with server.viewer_client() as client:
        return [s for s in client.get("/api/sessions").json()["sessions"] if s["name"] == name]


def session_named(server: LiveServer, name: str) -> dict[str, Any]:
    matches = sessions_named(server, name)
    assert len(matches) == 1, matches
    return matches[0]


def output_of(server: LiveServer, session_id: str) -> str:
    with server.viewer_client() as client:
        page = client.get(f"/api/sessions/{session_id}/logs", params={"limit": 2000}).json()
    return "".join(chunk["data"] for chunk in page["chunks"]).replace("\r\n", "\n")


def script(code: str) -> list[str]:
    return [PY, "-c", textwrap.dedent(code)]


def test_success_output_local_and_remote(home: Path, live_server: LiveServer) -> None:
    result = run_cli(
        home,
        live_server,
        "--name",
        "PDF Parser",
        "--",
        *script(
            """
            import sys
            for page in range(1, 4):
                print(f"Processed page {page}")
            print("warning on stderr", file=sys.stderr)
            """
        ),
    )
    assert result.returncode == 0, result.stderr.decode()
    local = result.stdout.decode().replace("\r\n", "\n")
    assert "Processed page 3\n" in local
    assert "warning on stderr" in local  # combined stream, echoed locally
    session = session_named(live_server, "PDF Parser")
    assert session["status"] == "succeeded"
    assert session["exit_code"] == 0
    assert session["ended_at"] >= session["started_at"]
    assert session["last_line"] == "warning on stderr"
    remote = output_of(live_server, session["id"])
    assert remote == "Processed page 1\nProcessed page 2\nProcessed page 3\nwarning on stderr\n"


@pytest.mark.parametrize("code", [1, 3, 42])
def test_failure_exit_code_propagates(home: Path, live_server: LiveServer, code: int) -> None:
    result = run_cli(home, live_server, "--name", f"fail {code}", "--", *script(f"import sys; sys.exit({code})"))
    assert result.returncode == code
    session = session_named(live_server, f"fail {code}")
    assert session["status"] == "failed"
    assert session["exit_code"] == code


def test_arguments_are_not_interpreted_by_a_shell(home: Path, live_server: LiveServer) -> None:
    result = run_cli(
        home, live_server, "--name", "argv", "--", PY, "-c", "import sys; print(sys.argv[1:])", "a b", "$HOME", "*", ";rm"
    )
    assert result.returncode == 0
    assert "['a b', '$HOME', '*', ';rm']" in result.stdout.decode()
    assert session_named(live_server, "argv")["command"][-4:] == ["a b", "$HOME", "*", ";rm"]


def test_child_sees_a_tty_and_session_id_but_env_is_not_uploaded(home: Path, live_server: LiveServer) -> None:
    result = run_cli(
        home,
        live_server,
        "--name",
        "tty",
        "--",
        *script(
            """
            import os, sys
            print("isatty", sys.stdout.isatty())
            print("session", os.environ["TERMWATCH_SESSION_ID"])
            """
        ),
    )
    assert result.returncode == 0
    session = session_named(live_server, "tty")
    out = output_of(live_server, session["id"])
    assert "isatty True" in out
    assert f"session {session['id']}" in out
    assert "env" not in session and "environment" not in session
    assert home.name not in str(session.get("command"))  # TERMWATCH_HOME was not leaked


def test_command_not_found(home: Path, live_server: LiveServer) -> None:
    result = run_cli(home, live_server, "--name", "missing", "--", "definitely-not-a-command-xyz")
    assert result.returncode == 127
    assert b"command not found" in result.stderr
    session = session_named(live_server, "missing")
    assert session["status"] == "failed"
    assert session["exit_code"] == 127
    assert "command not found" in session["error"]


def test_permission_denied(home: Path, live_server: LiveServer, tmp_path: Path) -> None:
    target = tmp_path / "not-executable.sh"
    target.write_text("#!/bin/sh\necho hi\n")
    target.chmod(0o644)
    result = run_cli(home, live_server, "--name", "noexec", "--", str(target))
    assert result.returncode == 126
    assert session_named(live_server, "noexec")["status"] == "failed"


def test_output_streams_while_running(home: Path, live_server: LiveServer) -> None:
    proc = start_cli(
        home,
        live_server.url,
        "--name",
        "streaming",
        "--",
        *script(
            """
            import time
            print("first line")
            time.sleep(4)
            print("second line")
            """
        ),
    )
    try:
        session = wait_for(lambda: (sessions_named(live_server, "streaming") or [None])[0], timeout=5)
        wait_for(lambda: "first line" in output_of(live_server, session["id"]), timeout=3)
        assert proc.poll() is None  # still running: output arrived before exit
        assert session_named(live_server, "streaming")["status"] == "running"
    finally:
        proc.wait(timeout=30)
    assert proc.returncode == 0
    assert "second line" in output_of(live_server, session["id"])


def test_sigint_marks_interrupted_and_returns_130(home: Path, live_server: LiveServer) -> None:
    proc = start_cli(
        home,
        live_server.url,
        "--name",
        "interrupt me",
        "--",
        *script(
            """
            import time
            for i in range(600):
                print("tick", i, flush=True)
                time.sleep(0.1)
            """
        ),
    )
    session = wait_for(lambda: (sessions_named(live_server, "interrupt me") or [None])[0], timeout=10)
    wait_for(lambda: "tick 2" in output_of(live_server, session["id"]), timeout=10)
    proc.send_signal(signal.SIGINT)
    _, stderr = proc.communicate(timeout=30)
    assert proc.returncode == 130, stderr.decode()
    assert b"Interrupt received" in stderr
    session = session_named(live_server, "interrupt me")
    assert session["status"] == "interrupted"
    assert session["exit_code"] == 130
    assert session["signal"] == "SIGINT"


def test_sigterm_marks_stopped(home: Path, live_server: LiveServer) -> None:
    proc = start_cli(home, live_server.url, "--name", "stop me", "--", PY, "-c", "import time; print('up', flush=True); time.sleep(60)")
    session = wait_for(lambda: (sessions_named(live_server, "stop me") or [None])[0], timeout=10)
    wait_for(lambda: "up" in output_of(live_server, session["id"]), timeout=10)
    proc.send_signal(signal.SIGTERM)
    proc.communicate(timeout=30)
    assert proc.returncode == 128 + signal.SIGTERM
    session = session_named(live_server, "stop me")
    assert session["status"] == "stopped"
    assert session["signal"] == "SIGTERM"


def test_child_killed_by_signal_is_a_failure(home: Path, live_server: LiveServer) -> None:
    result = run_cli(home, live_server, "--name", "oom", "--", *script("import os, signal; os.kill(os.getpid(), signal.SIGKILL)"))
    assert result.returncode == 128 + signal.SIGKILL
    session = session_named(live_server, "oom")
    assert session["status"] == "failed"
    assert session["signal"] == "SIGKILL"
    assert session["exit_code"] == 137


def test_concurrent_sessions(home: Path, live_server: LiveServer) -> None:
    jobs = {
        "PDF Parser": ("parser", 0),
        "Model Training": ("train", 5),
        "PDF Parser ": ("dup", 0),  # names are stripped -> duplicate name, distinct session
    }
    procs = []
    for name, (word, code) in jobs.items():
        procs.append(
            start_cli(
                home,
                live_server.url,
                "--name",
                name,
                "--",
                *script(
                    f"""
                    import sys, time
                    for i in range(15):
                        print("{word}", i, flush=True)
                        time.sleep(0.05)
                    sys.exit({code})
                    """
                ),
            )
        )
    codes = [proc.wait(timeout=60) for proc in procs]
    assert codes == [0, 5, 0]
    parsers = sessions_named(live_server, "PDF Parser")
    assert len(parsers) == 2 and parsers[0]["id"] != parsers[1]["id"]
    training = session_named(live_server, "Model Training")
    assert training["status"] == "failed" and training["exit_code"] == 5
    train_out = output_of(live_server, training["id"])
    assert train_out == "".join(f"train {i}\n" for i in range(15))
    for parser in parsers:
        assert parser["status"] == "succeeded"
        out = output_of(live_server, parser["id"])
        word = "parser" if "parser 0" in out else "dup"
        assert out == "".join(f"{word} {i}\n" for i in range(15))


def test_server_unreachable_then_available(home: Path) -> None:
    settings = make_settings(home, port=free_port())
    url = f"http://127.0.0.1:{settings.port}"
    proc = start_cli(
        home,
        url,
        "--name",
        "late server",
        "--",
        *script(
            """
            import time
            for i in range(8):
                print("step", i, flush=True)
                time.sleep(0.4)
            """
        ),
    )
    time.sleep(1.0)
    server = start_live_server(settings)
    try:
        _, stderr = proc.communicate(timeout=60)
        assert proc.returncode == 0
        assert b"Cannot reach the TermWatch server" in stderr
        assert b"Reconnected" in stderr
        session = session_named(server, "late server")
        assert session["status"] == "succeeded"
        assert output_of(server, session["id"]) == "".join(f"step {i}\n" for i in range(8))
    finally:
        server.stop()


def test_server_never_available_preserves_exit_code(home: Path) -> None:
    url = f"http://127.0.0.1:{free_port()}"
    proc = subprocess.run(
        [*CLI, "run", "--flush-timeout", "1", "--name", "offline", "--", PY, "-c", "print('hi'); raise SystemExit(4)"],
        env=cli_env(home, url),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=60,
    )
    assert proc.returncode == 4
    assert b"hi" in proc.stdout
    assert b"Could not deliver the final status" in proc.stderr


def test_server_restart_mid_run(home: Path) -> None:
    settings = make_settings(home, port=free_port())
    server = start_live_server(settings)
    proc = start_cli(
        home,
        server.url,
        "--name",
        "survives restart",
        "--",
        *script(
            """
            import time
            for i in range(12):
                print("beat", i, flush=True)
                time.sleep(0.4)
            """
        ),
    )
    try:
        session = wait_for(lambda: (sessions_named(server, "survives restart") or [None])[0], timeout=10)
        server.stop()
        time.sleep(1.0)
        server = start_live_server(settings)
        assert session_named(server, "survives restart")["status"] in ("disconnected", "running")
        proc.wait(timeout=60)
        assert proc.returncode == 0
        final = session_named(server, "survives restart")
        assert final["status"] == "succeeded"
        assert output_of(server, session["id"]) == "".join(f"beat {i}\n" for i in range(12))
    finally:
        server.stop()


def test_cli_list_and_token_commands(home: Path, live_server: LiveServer) -> None:
    run_cli(home, live_server, "--name", "listed", "--", PY, "-c", "pass")
    env = cli_env(home, live_server.url)
    listed = subprocess.run([*CLI, "list"], env=env, capture_output=True, timeout=30)
    assert listed.returncode == 0
    assert b"listed" in listed.stdout and b"succeeded" in listed.stdout
    shown = subprocess.run([*CLI, "token", "show", "--url", "https://box.example.ts.net"], env=env, capture_output=True, timeout=30)
    token = live_server.credentials.access_token
    assert f"https://box.example.ts.net/#token={token}".encode() in shown.stdout
    assert live_server.credentials.agent_token.encode() not in shown.stdout
    rotated = subprocess.run([*CLI, "token", "rotate"], env=env, capture_output=True, timeout=30)
    assert rotated.returncode == 0
    assert live_server.credentials.access_token != token


def test_usage_errors(home: Path) -> None:
    result = subprocess.run([*CLI, "run", "--name", "x"], env=cli_env(home), capture_output=True, timeout=30)
    assert result.returncode == 2
    help_text = subprocess.run([*CLI, "--help"], env=cli_env(home), capture_output=True, timeout=30)
    assert help_text.returncode == 0 and b"run" in help_text.stdout


def test_ctrl_c_typed_in_a_real_terminal(home: Path, live_server: LiveServer) -> None:
    """Ctrl+C from a TTY reaches the whole foreground group; TermWatch must not double-signal."""
    import fcntl
    import os
    import termios

    master, slave = os.openpty()

    def become_terminal_owner() -> None:
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)

    code = script(
        """
        import signal, sys, time
        hits = []
        def on_int(signum, frame):
            hits.append(signum)
            print(f"child got SIGINT x{len(hits)}", flush=True)
            time.sleep(0.5)
            sys.exit(7 if len(hits) == 1 else 99)
        signal.signal(signal.SIGINT, on_int)
        print("ready", flush=True)
        time.sleep(60)
        """
    )
    proc = subprocess.Popen(
        [*CLI, "run", "--name", "tty ctrl-c", "--", *code],
        env=cli_env(home, live_server.url),
        stdin=slave,
        stdout=slave,
        stderr=slave,
        start_new_session=True,
        preexec_fn=become_terminal_owner,
    )
    os.close(slave)
    captured = b""
    try:
        deadline = time.monotonic() + 20
        while b"ready" not in captured and time.monotonic() < deadline:
            captured += os.read(master, 4096)
        os.write(master, b"\x03")  # what the terminal sends for Ctrl+C
        proc.wait(timeout=30)
        try:
            while True:
                chunk = os.read(master, 4096)
                if not chunk:
                    break
                captured += chunk
        except OSError:
            pass
    finally:
        os.close(master)
    assert proc.returncode == 7, captured.decode(errors="replace")
    text = captured.decode(errors="replace")
    assert "child got SIGINT x1" in text
    assert "x2" not in text  # exactly one SIGINT reached the child
    session = session_named(live_server, "tty ctrl-c")
    assert session["status"] == "interrupted"
    assert session["exit_code"] == 7
