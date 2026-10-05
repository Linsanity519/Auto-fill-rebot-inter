from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.application.pricing import PricingService
from src.adapters.task_store import TaskStore
from src.adapters.archives import Archives, archive_chunks, collect_chunks, digest
from src.adapters.collection import Collection, read_all_pages
from src.domain.identity import identify_records
from src.runstate import RunState, StateError, StateUncertain


class Platform:
    def __init__(self):
        self.state = "off"
        self.writes = 0
        self.fail_after_write = False
        self.session = "test-session"

    def context(self):
        return {"environment": "test", "session_id": self.session, "identity_label": "fixture"}

    def discover(self, inputs):
        return [{"strategy_id": "123", "key": "456", "name": "示例人群", "link": "开启", "group": "A"}]

    def read(self, item):
        return self.state

    def set_state(self, item):
        self.writes += 1
        self.state = item.after
        if self.fail_after_write:
            raise TimeoutError("response lost")


class Submit:
    def decide(self, task_id, item):
        return "submit"


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = TaskStore(self.root / "client")
        self.platform = Platform()
        self.service = PricingService(self.store, self.platform, lambda: "2026-10-04T00:00:00+00:00")
        self.plan = self.service.prepare({"toggle_direction": "on"})

    def tearDown(self):
        self.tmp.cleanup()

    def task(self):
        return self.store.create_task(self.plan.plan_id, [self.plan.items[0].item_id], "auto", "test")

    def completed(self):
        task = self.task()
        self.service.execute(task, Submit())
        return task

    def test_prepare_is_readonly_and_bound_to_objects(self):
        self.assertEqual(self.platform.writes, 0)
        self.assertEqual(self.store.plan(self.plan.plan_id).items[0].object_id, "456")
        with self.assertRaises(ValueError):
            self.store.create_task(self.plan.plan_id, ["invented"], "auto", "test")

    def test_new_plan_cannot_bypass_unknown_submission(self):
        task = self.task()
        self.platform.fail_after_write = True
        self.service.execute(task, Submit())
        replacement = self.service.prepare({"toggle_direction": "on"})
        with self.assertRaises(ValueError):
            self.store.create_task(replacement.plan_id, [replacement.items[0].item_id], "auto", "test")

    def test_legacy_archive_values_keep_dates_and_exclude_credentials(self):
        from src.adapters.archives import scrub
        from datetime import date
        from decimal import Decimal
        value = scrub({"date": date(2026, 10, 4), "amount": Decimal("12.30"),
                       "cookie": "DO_NOT_KEEP", "url": "https://example.invalid/?key=DO_NOT_KEEP"})
        encoded = json.dumps(value)
        self.assertNotIn("DO_NOT_KEEP", encoded)
        self.assertEqual(value["date"], "2026-10-04")
        self.assertEqual(value["amount"], "12.30")

    def test_old_manifest_import_is_readonly_and_incomplete(self):
        path = self.root / "old.json"
        path.write_text(json.dumps({"run_id": "old-run", "items": [{"name": "fixture", "status": "ok"}]}), encoding="utf-8")
        archive = Archives(self.store)
        receipt = archive.import_legacy(path)
        self.assertEqual(receipt["status"], "received_complete")
        self.assertEqual(archive.imported()[0]["body"]["completeness"], "legacy_partial")
        self.assertEqual(self.store.tasks(), [])

    def test_legacy_crash_preserves_archive_and_live_run_not_marked_crashed(self):
        from src.adapters.legacy.archive_capture import LegacyCapture
        capture = LegacyCapture({"task_data_dir": str(self.store.root)}, {"name": "fixture"},
                                [{"header": {"名称": "fixture"}, "items": []}], "confirm", "test")
        self.store.recover_legacy()
        self.assertEqual(self.store.task(capture.task_id)["status"], "running")
        # Releasing the OS-owned lease models process death; no success result exists.
        capture.lease.release()
        self.store.recover_legacy()
        self.assertEqual(self.store.task(capture.task_id)["status"], "unknown")
        document, data = Archives(self.store).bundle(capture.task_id)
        self.assertEqual(document["body"]["plan"]["inputs"][0]["header"]["名称"], "fixture")

    def test_duplicate_client_submission_reuses_task(self):
        task = self.task()
        self.assertEqual(self.task(), task)
        self.service.execute(task, Submit())
        with self.assertRaises(ValueError):
            self.service.execute(task, Submit())
        self.assertEqual(self.platform.writes, 1)
        self.assertEqual(self.store.results(task)[0]["status"], "verified_success")

    def test_context_change_and_stale_state_never_write(self):
        task = self.task()
        self.platform.session = "another"
        with self.assertRaises(ValueError):
            self.service.execute(task, Submit())
        self.platform.session = "test-session"
        self.platform.state = "unknown"
        self.service.execute(task, Submit())
        self.assertEqual(self.platform.writes, 0)
        self.assertEqual(self.store.task(task)["status"], "blocked")

    def test_lost_response_is_unknown_and_reconciliation_only_reads(self):
        task = self.task()
        self.platform.fail_after_write = True
        self.service.execute(task, Submit())
        self.assertEqual(self.store.task(task)["status"], "unknown")
        self.service.reconcile(task)
        self.assertEqual(self.store.task(task)["status"], "reconciled")
        self.assertEqual(self.platform.writes, 1)

    def test_intent_persistence_failure_stops_platform_write(self):
        task = self.task()
        with patch.object(self.store, "record", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.service.execute(task, Submit())
        self.assertEqual(self.platform.writes, 0)

    def test_success_persistence_failure_preserves_submitted(self):
        task = self.task()
        original = self.store.record
        def fail_final(tid, iid, status, detail):
            if status == "verified_success":
                raise OSError("disk full after submit")
            return original(tid, iid, status, detail)
        with patch.object(self.store, "record", side_effect=fail_final):
            with self.assertRaises(OSError):
                self.service.execute(task, Submit())
        self.assertEqual(self.platform.writes, 1)
        self.assertEqual(self.store.results(task)[0]["status"], "submitted")

    def test_reverse_uses_exact_task_and_checks_current(self):
        task = self.completed()
        reverse = self.service.reverse(task)
        self.assertEqual(reverse.source_task, task)
        self.assertEqual(reverse.items[0].strategy_id, "123")
        self.assertEqual(reverse.items[0].after, "off")
        self.platform.state = "off"
        with self.assertRaises(ValueError):
            self.service.reverse(task)
        with self.assertRaises(ValueError):
            self.service.reverse("task_does_not_exist")

    def test_recover_host_does_not_replay_write(self):
        task = self.task()
        command = self.store.claim_command()
        self.store.finish(task, "running", {})
        self.store.record(task, self.plan.items[0].item_id, "submitted", {})
        self.store.recover_host()
        self.assertEqual(self.store.task(task)["status"], "interrupted")
        self.assertEqual(self.store.command_result(command["id"])["status"], "failed")
        self.assertIsNone(self.store.claim_command())

    def test_backup_consistent_and_future_schema_rejected(self):
        task = self.completed()
        destination = self.root / "backup.sqlite3"
        self.store.backup(destination)
        import sqlite3
        from contextlib import closing
        with closing(sqlite3.connect(destination)) as db:
            self.assertEqual(db.execute("SELECT status FROM tasks WHERE id=?", (task,)).fetchone()[0], "completed")
        with self.store.connect() as db:
            db.execute("PRAGMA user_version=999")
        with self.assertRaises(RuntimeError):
            TaskStore(self.store.root)

    def test_confirmation_is_bound_to_pending_item(self):
        task = self.task()
        with self.assertRaises(ValueError):
            self.store.answer(task, self.plan.items[0].item_id, "submit")
        self.store.finish(task, "awaiting_confirmation", {"item_id": self.plan.items[0].item_id})
        with self.assertRaises(ValueError):
            self.store.answer(task, "other-item", "submit")
        self.store.answer(task, self.plan.items[0].item_id, "skip")
        self.assertEqual(self.store.control(task)["decision"], "skip")

    def test_archive_two_clients_reassemble_dedupe_and_receipt(self):
        task = self.completed()
        source = Archives(self.store)
        attachment = self.root / "输入.csv"
        attachment.write_text("姓名,类型\n示例,A\n", encoding="utf-8")
        ref = source.attach(attachment)
        with self.store.connect() as db:
            body = json.loads(db.execute("SELECT body FROM tasks WHERE id=?", (task,)).fetchone()[0])
            body["attachments"] = [ref]
            db.execute("UPDATE tasks SET body=? WHERE id=?", (json.dumps(body), task))
        collection = Collection(self.store)
        count = collection.enqueue(task, 128)
        self.assertGreater(count, 1)
        self.assertEqual(collection.enqueue(task, 128), count)
        chunks = [r["body"] for r in collection.pending(task)]
        receiver = Collection(TaskStore(self.root / "maintainer"))
        receipt = receiver.receive(list(reversed(chunks)) + [chunks[0]])
        self.assertEqual(receipt["attachments"], 1)
        self.assertEqual(receiver.receive(chunks)["status"], "duplicate")
        self.assertEqual(collection.acknowledge(receipt), count)
        self.assertTrue(all(r["status"] == "verified_complete" for r in collection.pending(task)))
        with self.assertRaises(ValueError):
            receiver.receive(chunks[:-1])
        conflict = {**chunks[0], "content": "different"}
        with self.assertRaises(ValueError):
            receiver.receive(chunks + [conflict])

    def test_rejected_and_unknown_delivery_remain_pending(self):
        task = self.completed()
        collection = Collection(self.store)
        collection.enqueue(task)
        class Reject:
            def send(self, value): return False
        collection.push(Reject())
        self.assertTrue(all(r["status"] == "rejected" for r in collection.pending()))
        class Timeout:
            def send(self, value): raise TimeoutError()
        collection.push(Timeout())
        self.assertTrue(all(r["status"] == "retry" for r in collection.pending()))
        class Accept:
            def send(self, value): return True
        collection.push(Accept())
        self.assertTrue(all(r["status"] == "accepted" for r in collection.pending()))
        self.assertFalse(any(r["status"] == "verified_complete" for r in collection.pending()))

    def test_mismatched_chunk_identity_does_not_import_anything(self):
        task = self.completed()
        document, data = Archives(self.store).bundle(task)
        chunks = archive_chunks(document, data, 2000)
        for chunk in chunks:
            chunk["archive_id"] = "other-id"
        receiver = Collection(TaskStore(self.root / "receiver"))
        with self.assertRaises(ValueError):
            receiver.receive(chunks)
        self.assertEqual(receiver.archives.imported(), [])

    def test_archive_conflict_and_corruption_are_visible(self):
        import io, zipfile
        task = self.completed()
        source = Archives(self.store)
        document, data = source.bundle(task)
        receiver = Archives(TaskStore(self.root / "receiver"))
        receiver.import_bytes(data)
        document["body"]["task"]["status"] = "tampered-but-new-checksum"
        from src.adapters.task_store import encode
        document["sha256"] = digest(encode(document["body"]).encode())
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("manifest.json", encode(document))
        self.assertEqual(receiver.import_bytes(stream.getvalue())["status"], "conflict")
        self.assertEqual(receiver.imported()[0]["body"]["task"]["status"], "completed")
        with zipfile.ZipFile(stream := io.BytesIO(), "w") as archive:
            archive.writestr("../outside", "danger")
        with self.assertRaises(ValueError):
            receiver.import_bytes(stream.getvalue())

    def test_collect_pagination_never_truncates_silently(self):
        self.assertEqual(read_all_pages(lambda cursor, size: ([cursor], "next") if cursor is None else ([cursor], None)),
                         [None, "next"])
        with self.assertRaises(ValueError):
            read_all_pages(lambda cursor, size: ([1], "same"))
        with self.assertRaises(ValueError):
            read_all_pages(lambda cursor, size: ([1], str(cursor) + "x"), max_pages=2)

    def test_stable_ids_survive_filter_and_duplicate_content(self):
        rows = identify_records([{"name": "bad"}, {"name": "a"}, {"name": "a"}], "same")
        filtered = identify_records(rows[1:], "same")
        self.assertEqual(filtered[0]["_execution_id"], rows[1]["_execution_id"])
        self.assertEqual(filtered[0]["_execution_row"], 2)
        self.assertNotEqual(filtered[0]["_execution_id"], filtered[1]["_execution_id"])

    def test_state_durability_failure_and_unknown_block_replay(self):
        path = self.root / "state.json"
        state = RunState(path, "test")
        with patch("src.runstate.atomic_json", side_effect=OSError("disk full")):
            with self.assertRaises(StateError):
                state.mark_done("id")
        self.assertEqual(state.done, [])
        with self.assertRaises(StateError):
            state.is_done("another")
        state = RunState(path, "test")
        state.begin("id")
        with self.assertRaises(StateUncertain):
            RunState(path, "test", resume=False).is_done("id")
        with self.assertRaises(StateUncertain):
            state.clear()
