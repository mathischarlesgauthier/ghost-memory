"""Tests des détecteurs sur bases synthétiques minimales."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from ghost.db import connect
from ghost.detect import (
    bash_token,
    commit_timestamps,
    detect_failure_loops,
    detect_human_overrides,
    detect_repeated_sequences,
    normalize_error,
)
from ghost.scan import run_scan


@pytest.fixture
def db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    _SEQ.clear()
    conn = connect(tmp_path / "test.db")
    yield conn
    conn.close()


_SEQ: dict[tuple[str, str | None], int] = {}


def ev(
    conn: sqlite3.Connection,
    session: str,
    *,
    role: str = "assistant",
    block: str = "text",
    tool: str | None = None,
    tuid: str | None = None,
    err: int = 0,
    human: int = 0,
    text: str | None = None,
    payload: dict[str, object] | None = None,
    ts: str = "2026-01-05T10:00:00.000Z",
    agent: str | None = None,
) -> int:
    conn.execute(
        "INSERT OR IGNORE INTO sessions (id, project) VALUES (?, ?)",
        (session, f"proj-{session[0]}"),
    )
    key = (session, agent)
    _SEQ[key] = _SEQ.get(key, 0) + 1
    cur = conn.execute(
        "INSERT INTO events (session_id, agent_id, seq, ts, role, block_type, tool_name,"
        " tool_use_id, is_error, is_human, text, payload_json, src_file, src_line)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            session,
            agent,
            _SEQ[key],
            ts,
            role,
            block,
            tool,
            tuid,
            err,
            human,
            text,
            json.dumps(payload) if payload else None,
            f"src/{session}/{agent or 'main'}.jsonl",
            _SEQ[key],
        ),
    )
    return int(cur.lastrowid or 0)


def tool_call(
    conn: sqlite3.Connection,
    session: str,
    tool: str,
    tuid: str,
    *,
    err: int = 0,
    result: str = "ok",
    payload: dict[str, object] | None = None,
    ts: str = "2026-01-05T10:00:00.000Z",
    agent: str | None = None,
) -> None:
    ev(conn, session, block="tool_use", tool=tool, tuid=tuid, payload=payload, ts=ts, agent=agent)
    ev(conn, session, role="user", block="tool_result", tuid=tuid, err=err, text=result,
       ts=ts, agent=agent)


def test_normalize_error_stable_across_paths_and_numbers() -> None:
    a = normalize_error("Exit code 1\nModuleNotFoundError: No module named 'gw_kernel' "
                        "in /Users/x/proj/svc/main.py line 42")
    b = normalize_error("Exit code 2\nModuleNotFoundError: No module named 'gw_kernel' "
                        "in /Users/y/other/app.py line 7")
    assert a == b
    assert "<path>" in a and "<n>" in a


def test_bash_token_enrichment() -> None:
    assert bash_token("git commit -m 'x'") == "Bash:git-commit"
    assert bash_token("cd /tmp && FOO=1 uv run pytest -q") == "Bash:uv-run-pytest"
    assert bash_token("git -C /repo status") == "Bash:git-status"
    assert bash_token("ls -la") == "Bash:ls"


def test_failure_loop_detected_at_two_errors(db: sqlite3.Connection) -> None:
    s = "s1"
    tool_call(db, s, "Bash", "t1", err=1, result="Exit code 1\nModuleNotFoundError: x")
    tool_call(db, s, "Read", "t2")  # interleavé, ne casse pas le run
    tool_call(db, s, "Bash", "t3", err=1, result="Exit code 1\nModuleNotFoundError: x")
    tool_call(db, s, "Bash", "t4", result="ok enfin")
    occs = detect_failure_loops(db)
    assert len(occs) == 1
    assert occs[0].meta["converged"] is True
    assert occs[0].cost == 2.0
    assert occs[0].signature.startswith("Bash|")


def test_failure_loop_single_error_excluded(db: sqlite3.Connection) -> None:
    tool_call(db, "s1", "Bash", "t1", err=1, result="Exit code 1\nboom")
    tool_call(db, "s1", "Bash", "t2", result="ok")
    assert detect_failure_loops(db) == []


def test_failure_loop_harness_noise_is_neutral(db: sqlite3.Connection) -> None:
    s = "s1"
    tool_call(db, s, "Edit", "t1", err=1,
              result="<tool_use_error>File has not been read yet.</tool_use_error>")
    tool_call(db, s, "Bash", "t2", err=1,
              result="Permission for this action was denied by the Claude Code "
                     "auto mode classifier")
    tool_call(db, s, "Bash", "t3", err=1, result="Exit code 1\nreal error")
    tool_call(db, s, "Bash", "t4", result="ok")
    # 1 seule vraie erreur Bash -> pas de run >= 2, et le bruit ne compte pas.
    assert detect_failure_loops(db) == []


def test_human_override_keywords_and_turn(db: sqlite3.Connection) -> None:
    s = "s1"
    ev(db, s, role="user", block="text", human=1, text="ajoute la feature")
    tool_call(db, s, "Edit", "t1", payload={"input": {"file_path": "/p/app/main.py"}})
    ev(db, s, role="user", block="text", human=1,
       text="non c'est pas ça, refais le menu")
    occs = detect_human_overrides(db)
    assert len(occs) == 1
    keywords = occs[0].meta["keywords"]
    assert isinstance(keywords, list) and "refais" in keywords
    assert occs[0].meta["file"] == "app/main.py"


def test_human_override_needs_keywords_and_tools(db: sqlite3.Connection) -> None:
    s = "s1"
    ev(db, s, role="user", block="text", human=1, text="vas-y")
    tool_call(db, s, "Edit", "t1", payload={"input": {"file_path": "/p/a.py"}})
    ev(db, s, role="user", block="text", human=1, text="merci continue comme ça")
    assert detect_human_overrides(db) == []
    # Mots-clés mais tour sans Edit/Write/Bash -> rien non plus.
    ev(db, s, role="user", block="text", human=1, text="non pas comme ça")
    assert detect_human_overrides(db) == []


def test_repeated_sequence_needs_three_sessions(db: sqlite3.Connection) -> None:
    for i, s in enumerate(["s1", "s2", "s3"]):
        tool_call(db, s, "Read", f"r{i}")
        tool_call(db, s, "Edit", f"e{i}")
        tool_call(db, s, "Bash", f"b{i}", payload={"input": {"command": "uv run pytest -q"}})
    occs = detect_repeated_sequences(db)
    sigs = {o.signature for o in occs}
    assert "Read→Edit→Bash:uv-run-pytest" in sigs
    assert len([o for o in occs if o.signature == "Read→Edit→Bash:uv-run-pytest"]) == 3


def test_repeated_sequence_pure_exploration_excluded(db: sqlite3.Connection) -> None:
    for i, s in enumerate(["s1", "s2", "s3"]):
        tool_call(db, s, "Read", f"r{i}")
        tool_call(db, s, "Bash", f"g{i}", payload={"input": {"command": "grep -r foo ."}})
        tool_call(db, s, "Read", f"r2{i}")
    assert detect_repeated_sequences(db) == []


def test_repeated_sequence_two_sessions_excluded(db: sqlite3.Connection) -> None:
    for i, s in enumerate(["s1", "s2"]):
        tool_call(db, s, "Read", f"r{i}")
        tool_call(db, s, "Edit", f"e{i}")
        tool_call(db, s, "Bash", f"b{i}", payload={"input": {"command": "uv run pytest"}})
    assert detect_repeated_sequences(db) == []


def test_failure_loop_replayed_session_counted_once(db: sqlite3.Connection) -> None:
    # Une session reprise rejoue l'historique dans un nouveau fichier avec
    # les MÊMES tool_use_id : le run ne doit compter qu'une fois.
    for s in ("s1", "s2"):
        tool_call(db, s, "Bash", "tA", err=1, result="Exit code 1\nerr X")
        tool_call(db, s, "Bash", "tB", err=1, result="Exit code 1\nerr X")
        tool_call(db, s, "Bash", "tC", result="ok")
    occs = detect_failure_loops(db)
    assert len(occs) == 1
    assert occs[0].cost == 2.0  # pas gonflé par le fan-out de la jointure


def test_override_no_keyword_false_positives(db: sqlite3.Connection) -> None:
    s = "s1"
    ev(db, s, role="user", block="text", human=1, text="vas-y")
    tool_call(db, s, "Edit", "t1", payload={"input": {"file_path": "/p/a.py"}})
    ev(db, s, role="user", block="text", human=1,
       text="ok lance le debugger, le serveur est stoppé proprement, sinon on merge")
    assert detect_human_overrides(db) == []


def test_override_bash_only_turn_gets_command_signature(db: sqlite3.Connection) -> None:
    s = "s1"
    ev(db, s, role="user", block="text", human=1, text="vas-y")
    tool_call(db, s, "Bash", "t1", payload={"input": {"command": "git rebase main"}})
    ev(db, s, role="user", block="text", human=1, text="non annule ce rebase")
    occs = detect_human_overrides(db)
    assert len(occs) == 1
    assert occs[0].signature.endswith("|Bash:git-rebase")


def test_contains_subsequence_token_boundaries() -> None:
    from ghost.detect import _contains_subsequence

    assert _contains_subsequence(("Edit", "Bash:uv-run-pytest", "Read"), ("Edit",))
    assert _contains_subsequence(("a", "b", "c"), ("b", "c"))
    # 'Bash:uv-run' n'est PAS contenu dans 'Bash:uv-run-pytest' (piège du
    # substring sur la chaîne jointe).
    assert not _contains_subsequence(
        ("Edit", "Bash:uv-run-pytest", "Read"), ("Edit", "Bash:uv-run")
    )


def test_commit_classifier_reads_command_not_description(db: sqlite3.Connection) -> None:
    s = "s1"
    tool_call(db, s, "Bash", "t1", result="[main abc] done",
              payload={"input": {"command": "git -C /repo commit -m 'x'"}},
              ts="2026-01-05T10:00:00.000Z")
    tool_call(db, s, "Bash", "t2", result="hi",
              payload={"input": {"command": "echo hi",
                                 "description": "Explique git commit au lecteur"}},
              ts="2026-01-05T10:01:00.000Z")
    commits = commit_timestamps(db)
    assert commits == {s: ["2026-01-05T10:00:00.000Z"]}


def test_ground_truth_commit_after(db: sqlite3.Connection) -> None:
    s = "s1"
    tool_call(db, s, "Bash", "t1", err=1, result="Exit code 1\nerr X",
              ts="2026-01-05T10:00:00.000Z")
    tool_call(db, s, "Bash", "t2", err=1, result="Exit code 1\nerr X",
              ts="2026-01-05T10:01:00.000Z")
    tool_call(db, s, "Bash", "t3", result="ok", ts="2026-01-05T10:02:00.000Z")
    tool_call(db, s, "Bash", "t4", result="[main abc] done",
              payload={"input": {"command": "git commit -m x"}},
              ts="2026-01-05T10:03:00.000Z")
    commits = commit_timestamps(db)
    assert s in commits
    merged = run_scan(db)
    floop = next(c for c in merged if c.kind == "FAILURE_LOOP")
    assert floop.evidence[0]["ground_truth"] is True


def test_scan_upsert_preserves_status(db: sqlite3.Connection) -> None:
    s = "s1"
    tool_call(db, s, "Bash", "t1", err=1, result="Exit code 1\nerr X")
    tool_call(db, s, "Bash", "t2", err=1, result="Exit code 1\nerr X")
    tool_call(db, s, "Bash", "t3", result="ok")
    run_scan(db)
    db.execute("UPDATE candidates SET status = 'rejected'")
    db.commit()
    run_scan(db)
    rows = db.execute("SELECT status FROM candidates").fetchall()
    assert rows and all(r[0] == "rejected" for r in rows)
