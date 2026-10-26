"""C5 -- several OS processes writing one stream file keep continuity exact (spec 8.4-8.6, INV-034/035).

SRT1 -- Seed Reflection Tool. Specification and authorship: Seed Class Intelligence.
"""
import multiprocessing as mp
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from srt1 import SRT1, SourceEvent, StateDelta  # noqa: E402

PROCS = int(os.environ.get("SRT1_C5_PROCS", 6))  # raise for a heavier run: SRT1_C5_PROCS=12
PER = int(os.environ.get("SRT1_C5_PER", 15))


def writer(path, sid, w, q):
    s = SRT1(path)
    out = []
    for i in range(PER):
        kind = ("DISCOVERY", "DECISION", "POSITION")[i % 3]
        out.append(s.observe(SourceEvent(content=f"{kind}: worker {w} item {i} unique{w}x{i}"), sid).status)
    q.put((w, out))


def dup_writer(path, sid, q):
    q.put(SRT1(path).observe(SourceEvent(content="DECISION: delivered twice", external_source_id="msg-42"),
                             sid).status)


def redeliver(path, sid, seid, q):
    """Each process turns the SAME stored source event into its own, differently-identified delta, bypassing
    observe()'s status check -- as a crash/redelivery race can. Only the source-event claim can stop a double commit."""
    try:
        d = StateDelta(stream_id=sid, expected_state_version=SRT1(path).get_state(sid)["state_version"],
                       source_event_ids=[seid], primary_classification="CONTINUATION",
                       create_items=[{"item_type": "DECISION", "authority_status": "CURRENT",
                                      "content": "redelivered after crash"}])
        q.put(SRT1(path).submit_delta(d).status)
    except Exception as e:  # a crash or constraint error is a failure of the property, not a pass
        q.put(f"ERROR {type(e).__name__}")


class TestMultiProcess(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "srt1.db")
        boot = SRT1(self.path)
        self.sid = boot.create_stream("c5")
        boot.observe("INTENT: concurrent writers", self.sid)
        boot.store.db.close()
        self.ctx = mp.get_context("spawn")

    def run_procs(self, target, args_list):
        q = self.ctx.Queue()
        ps = [self.ctx.Process(target=target, args=(*a, q)) for a in args_list]
        [p.start() for p in ps]
        res = [q.get(timeout=120) for _ in ps]
        [p.join(timeout=30) for p in ps]
        return res

    def test_concurrent_writers_lose_nothing_and_duplicate_nothing(self):
        res = self.run_procs(writer, [(self.path, self.sid, w) for w in range(PROCS)])
        statuses = [s for _, out in res for s in out]
        self.assertEqual(set(statuses), {"COMMITTED"}, statuses)
        s = SRT1(self.path)
        self.addCleanup(s.store.db.close)
        events = s.store.events(self.sid)
        self.assertEqual([e["resulting_state_version"] for e in events], list(range(1, PROCS * PER + 2)))
        self.assertEqual(s.get_state(self.sid)["state_version"], PROCS * PER + 1)
        self.assertTrue(s.verify(self.sid)["ok"])
        texts = [i["content"]["text"] for i in s.store.load_model(self.sid).items.values()]
        self.assertEqual(len(texts), len(set(texts)))

    def test_same_message_delivered_to_two_processes_commits_once(self):
        res = self.run_procs(dup_writer, [(self.path, self.sid)] * 3)
        self.assertEqual(sorted(res), ["COMMITTED", "DUPLICATE", "DUPLICATE"])
        s = SRT1(self.path)
        self.addCleanup(s.store.db.close)
        decs = [i for i in s.store.load_model(self.sid).items.values() if i["item_type"] == "DECISION"]
        self.assertEqual(len(decs), 1)

    def test_one_stored_source_event_processed_by_many_processes_commits_once(self):
        """The source-event claim (INV-034) is what stops this; delta ids differ, external ids are absent."""
        boot = SRT1(self.path)
        row, _ = boot.store.add_source_event(SourceEvent(content="DECISION: redelivered after crash",
                                                         stream_id=self.sid))
        boot.store.db.close()
        self.assertEqual(row["processing_status"], "RECEIVED")
        res = self.run_procs(redeliver, [(self.path, self.sid, row["source_event_id"])] * 4)
        self.assertEqual(sorted(res), ["COMMITTED", "DUPLICATE", "DUPLICATE", "DUPLICATE"])
        s = SRT1(self.path)
        self.addCleanup(s.store.db.close)
        decs = [i for i in s.store.load_model(self.sid).items.values() if i["item_type"] == "DECISION"]
        self.assertEqual(len(decs), 1)
        self.assertEqual(s.get_state(self.sid)["state_version"], 2)  # INTENT + exactly one decision
        self.assertTrue(s.verify(self.sid)["ok"])


if __name__ == "__main__":
    unittest.main()
