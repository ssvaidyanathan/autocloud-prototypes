"""F10: S4 keeps pg_terminate_backend as a labelled, mutating operator step after the read-only review."""

from agent.app.specialists.synthesizer import db_caller_actions, db_pool_actions

DB = "ata-inventory-db"


def test_s4_order_review_then_mutating_terminate_then_describe():
    acts = db_pool_actions(DB, "p", db_caller_actions(DB, "p"))
    assert len(acts) == 3
    assert acts[0].title.startswith("Inspect connection pool release logic and pool size in ata-inventory-api")
    assert "pg_terminate_backend" in acts[1].command
    assert acts[1].title.startswith("Mutating, operator judgement required:")
    assert "only after the review above" in acts[1].title
    assert "gcloud sql instances describe ata-inventory-db" in acts[2].command
    assert "80% of max_connections" in acts[1].verification


def test_without_a_caller_the_read_only_describe_leads():
    acts = db_pool_actions(DB, "p", [])
    assert "gcloud sql instances describe" in acts[0].command
    assert "pg_terminate_backend" in acts[1].command


def test_no_mutating_command_other_than_the_labelled_one():
    acts = db_pool_actions(DB, "p", db_caller_actions(DB, "p"))
    for a in acts:
        if "pg_terminate_backend" not in (a.command or ""):
            assert "patch" not in (a.command or "") and "DELETE" not in (a.command or "").upper()
        else:
            assert "Mutating" in a.title
