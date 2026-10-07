"""One source-bound generation loop: feedback, billing, invalid drafts and independent approval."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rfp_assistant.service import auth, drafting
from rfp_assistant.gateway import budget, generation
from rfp_assistant.evaluation import evaluation, gold
from rfp_assistant.storage import store
from tests import fixtures, release_fixtures as fx


def amount_slot(env) -> dict:
    """A drafting slot for the fixture's unreviewed amount question, bound to its evidence."""
    row = fx.amount_row(env, reviewed=False)
    return {**{k: row[k] for k in ('question_id', 'revision', 'question_type', 'scope', 'as_of_date')},
            'intent': 'Budget with tax', 'sources': [
                {'doc_id': g['doc_id'], 'element_id': g['alternatives'][0]['element_id']}
                for g in row['evidence_groups']]}


class DraftingTest(unittest.TestCase):
    def test_gateway_ownership_and_setup_failure_cleanup(self):
        with tempfile.TemporaryDirectory() as folder:
            env = fx.make_env(Path(folder))
            slot = amount_slot(env)
            transport = generation.FakeTransport()
            with fixtures.foreign_gateway(env.settings):  # another process owns the paid gateway
                with self.assertRaisesRegex(gold.GoldError, 'already owns'):
                    drafting.generate(env.settings, {'slots': [slot]}, Path(folder)/'locked', 100000, transport)
                self.assertEqual(transport.calls, [])
                self.assertFalse((Path(folder)/'locked').exists())
            parent = Path(folder)/'file'
            parent.write_text('not a directory', encoding='utf-8')
            for out, schema_failure in [(parent/'output', False), (Path(folder)/'setup-failed', True)]:
                owned = generation.FakeTransport()
                with patch.object(drafting, 'read_api_key', return_value='offline'), \
                     patch.object(generation, 'OpenAITransport', return_value=owned), \
                     patch.object(drafting, 'response_format', side_effect=RuntimeError('schema failure') if schema_failure else None):
                    # Windows reports a file in the parent path as FileExistsError, POSIX as NotADirectoryError
                    with self.assertRaises((OSError, RuntimeError)):
                        drafting.generate(env.settings.with_(provider='openai'), {'slots': [slot]}, out, 100000)
                self.assertTrue(owned.closed)
                self.assertEqual(owned.calls, [])
                with fixtures.foreign_gateway(env.settings):  # the failed setup released the gateway
                    pass
            with store.open_db(env.settings.db_path) as conn:
                self.assertEqual(conn.execute("SELECT status FROM requests WHERE idempotency_key LIKE 'gold-draft:%'").fetchone()[0], 'failed')

    def test_deadline_cutoff_is_preserved_and_required_when_stated(self):
        with tempfile.TemporaryDirectory() as folder:
            env = fx.make_env(Path(folder))
            original = fx.deadline_row(env, reviewed=False)
            slot = {**{k: original[k] for k in ('question_id', 'revision', 'question_type', 'scope', 'as_of_date')},
                    'intent': 'Submission cutoff', 'sources': [{'doc_id': original['scope'][0]['doc_id'],
                        'element_id': original['evidence_groups'][0]['alternatives'][0]['element_id']}]}
            resolved = drafting.source_slots(env.settings, {'slots': [slot]})[0]
            draft = {'question': 'When must we submit?', 'answer': '2024-06-11 17:00', 'difficulty': 'Cutoff time',
                     'pins': [{'source': 0, 'quote': fx.DEADLINE}], 'claims': [{'kind': 'date', 'value': '2024-06-11',
                     'unit': None, 'time': '17:00', 'support': [0], 'qualifiers': [], 'critical_kind': 'deadline'}]}
            claim = drafting.materialize(resolved, draft, {})['required_claims'][0]
            self.assertEqual(evaluation.typed_verdict(claim, '2024-06-11 17:00'), 'correct')
            self.assertEqual(evaluation.typed_verdict(claim, '2024-06-11 18:00'), 'wrong_value')
            self.assertEqual(evaluation.typed_verdict(claim, '2024-06-11'), 'incomplete_qualifier')
            draft['claims'][0].pop('time')
            with self.assertRaisesRegex(gold.GoldError, 'typed HH:MM'):
                drafting.materialize(resolved, draft, {})

    def test_sealed_generation_requires_owner_and_private_storage(self):
        with tempfile.TemporaryDirectory() as folder:
            env = fx.make_env(Path(folder))
            fx.set_splits(env, {'기관A': 'test', '기관F': 'dev', '기관D': 'test', '기관E': 'test'})
            ref = fx.scope(env, '기관A')
            slot = {'question_id': 'sealed-warranty', 'revision': 1, 'question_type': 'direct_fact',
                    'intent': 'Warranty period', 'scope': [ref], 'as_of_date': '2024-06-01',
                    'sources': [{'doc_id': ref['doc_id'], 'element_id': fx.element(env, '기관A', '%하자보수%')}]}
            private = env.settings.data_dir/'sealed'/'drafts'
            transport = generation.FakeTransport()
            with self.assertRaises(auth.AuthError):
                drafting.generate(env.settings, {'slots': [slot]}, private, 100000, transport, split='test')
            with self.assertRaisesRegex(gold.GoldError, 'private sealed'):
                drafting.generate(env.settings, {'slots': [slot]}, Path(folder)/'public', 100000, transport,
                                  split='test', principal=auth.OWNER_CLI)
            with self.assertRaises(gold.GoldError):
                drafting.source_slots(env.settings, {'slots': [slot]})
            draft = {'question_id': slot['question_id'], 'question': 'How long after acceptance?',
                     'answer': '12 months', 'difficulty': 'Reference event',
                     'pins': [{'source': 0, 'quote': fx.WARRANTY}], 'claims': [{'kind': 'number', 'value': 12,
                         'unit': '개월', 'support': [0], 'qualifiers': [['검수 완료일로부터']], 'critical_kind': None}],
                     'skip_reason': None}
            transport.responder = lambda _: generation.ProviderResponse(json.dumps({'drafts': [draft]}), None,
                'stop', {'prompt_tokens': 10, 'completion_tokens': 10}, 'sealed-draft-test')
            result = drafting.generate(env.settings, {'slots': [slot]}, private, 100000, transport,
                                       split='test', principal=auth.OWNER_CLI)
            self.assertEqual((result['rows'], result['approvals']), (1, 0))
            self.assertEqual(store.read_jsonl(private/'candidates.jsonl')[0]['split'], 'test')

    def test_uncertain_usage_blocks_the_next_generation_without_replaying(self):
        with tempfile.TemporaryDirectory() as folder:
            env = fx.make_env(Path(folder))
            slot = amount_slot(env)
            transport = generation.FakeTransport(lambda _: generation.ProviderResponse('{}', None, 'stop', None, 'unknown'))
            with self.assertRaises(gold.GoldError):
                drafting.generate(env.settings, {'slots': [slot]}, Path(folder)/'blocked', 1, transport)
            self.assertEqual(len(transport.calls), 0)
            with self.assertRaisesRegex(gold.GoldError, 'usage unknown'):
                drafting.generate(env.settings, {'slots': [slot]}, Path(folder)/'unknown', 100000, transport)
            self.assertGreater(budget.snapshot(env.settings.db_path).unknown_micro_usd, 0)
            with self.assertRaisesRegex(gold.GoldError, 'outstanding drafting'):
                drafting.generate(env.settings, {'slots': [slot]}, Path(folder)/'repeat', 100000, transport)
            self.assertEqual(len(transport.calls), 1)

    def test_quote_anchoring_preserves_characters_and_rejects_changed_or_repeated_facts(self):
        source = 'preface\n12%  above\nexception applies'
        a, b = drafting.anchor_quote(source, '12% above exception applies')
        self.assertEqual(source[a:b], '12%  above\nexception applies')
        for quote in ('12% below exception applies', '13% above exception applies', ''):
            with self.assertRaises(gold.GoldError):
                drafting.anchor_quote(source, quote)
        with self.assertRaises(gold.GoldError):
            drafting.anchor_quote('12% above; 12% above', '12% above')
        spans = drafting.source_spans('Header\n○ Before entry submit\n - Officer: declaration\n○ Train quarterly')
        self.assertEqual(''.join(s['text'] for s in spans), 'Header\n○ Before entry submit\n - Officer: declaration\n○ Train quarterly')
        native = 'Header\n Record grades\n - Map current to target\n User groups\n - Map current to target'
        spans = drafting.source_spans(native)
        self.assertEqual([s['text'].strip() for s in spans],
                         ['Header', ' Record grades', '- Map current to target', ' User groups', '- Map current to target'])
        self.assertEqual(''.join(s['text'] for s in spans), native)
        slot = {'question_id': 'span-check', 'revision': 1, 'split': 'dev', 'mode': 'single', 'scope': [],
                'as_of_date': '2026-10-02', 'family_ids': [], 'question_type': 'direct_fact', 'sources': [{
                    'doc_id': 'doc', 'source_hash': 'hash', 'extraction_id': 'ex', 'element_id': 'el',
                    'text': 'Repeated clause; repeated clause',
                    'spans': [{'offsets': [0, 15], 'text': 'Repeated clause'}]}]}
        draft = {'question': 'q', 'answer': 'a', 'difficulty': 'd', 'pins': [{'source': 0, 'span': 0, 'quote': None}],
                 'claims': []}
        row = drafting.materialize(slot, draft, {})
        self.assertEqual(row['evidence_groups'][0]['alternatives'][0]['quote'], 'Repeated clause')
        draft['pins'][0]['span'] = 99
        with self.assertRaises(gold.GoldError):
            drafting.materialize(slot, draft, {})

    def _rejected_lookup_and_sealed_row(self, env, folder):
        """A drafter's own introductory lookup, refused approval, withdrawn with a lesson; and a sealed row a
        reviewer rejected with a secret note. Returns both rows."""
        s = env.settings
        old = fx.amount_row(env, reviewed=False)
        batch = Path(folder) / "old.jsonl"
        store.write_jsonl_atomic(batch, [old])
        gold.submit(s, batch, "old", "dev", "agent-a")
        c = gold.candidate(s, "dev-amount-r1")
        with self.assertRaises(gold.GoldError):
            gold.decide(s, c["candidate_id"], "approve", "agent-a", c["row_sha256"], original_inspected=True)
        gold.decide(s, c["candidate_id"], "reject", "agent-a", c["row_sha256"], ["too_easy"],
                    "Withdraw my introductory lookup before API regeneration")
        with self.assertRaises(gold.GoldError):
            drafting.lessons(s)
        gold.infer(s, c["candidate_id"], "agent-a", {"cause": "Introductory lookup", "lesson": "Need a real task",
                    "drafting_rule": "Retain the amount and its separately stated tax condition"})
        sealed = fx.row(env, "sealed-hidden", "Hidden question", "기관D", split="test", reviewed=False,
                        groups=[], claims=[], answerability="unanswerable", expected_status="insufficient_evidence",
                        negative_validation={"scope_searched": [env.refs['기관D'].doc_id], "methods": ['read'],
                                             "locations": ['all'], "original_complete": True, "rationale": 'none'})
        sealed_batch = Path(folder) / "sealed.jsonl"
        store.write_jsonl_atomic(sealed_batch, [sealed])
        gold.submit(s, sealed_batch, "sealed", "test", "agent-a")
        c = gold.candidate(s, "sealed-hidden-r1", include_sealed=True)
        gold.decide(s, c["candidate_id"], "reject", "person-b", c["row_sha256"], ["other"],
                    "SEALED SECRET", include_sealed=True)
        return old, sealed

    def test_generation_preserves_roles_and_learns_without_sealed_leakage(self):
        with tempfile.TemporaryDirectory() as folder:
            env = fx.make_env(Path(folder))
            s = env.settings
            old, sealed = self._rejected_lookup_and_sealed_row(env, folder)
            slots = [{"question_id": "api-amount", "revision": 1, "question_type": "table_numeric",
                      "intent": "Budget and tax condition", "scope": old["scope"], "as_of_date": old['as_of_date'],
                      "sources": [{"doc_id": g["doc_id"], "element_id": g['alternatives'][0]['element_id']}
                                  for g in old['evidence_groups']]}]
            valid = {"question_id": "api-amount", "question": "How much with which tax condition?",
                     "answer": "130,000,000 KRW including VAT", "difficulty": "Tax in a separate passage",
                     "pins": [{"source": 0, "quote": fx.AMOUNT}, {"source": 1, "quote": fx.VAT}],
                     "claims": [{"kind": "number", "value": 130000000, "unit": "KRW", "support": [0, 1],
                                 "qualifiers": [["부가가치세를 포함"]], "critical_kind": "amount"},
                                {"kind": "text", "value": "부가가치세를 포함", "unit": None, "support": [1],
                                 "qualifiers": [], "critical_kind": "mandatory_condition"}], "skip_reason": None}

            def reply(messages):
                self.assertNotIn('SEALED SECRET', json.dumps(messages))
                self.assertIn('Retain the amount', json.dumps(messages))
                self.assertIn('synthetic_examples', messages[-1]['content'])
                return generation.ProviderResponse(json.dumps({"drafts": [valid]}), None, "stop",
                    {"prompt_tokens": 100, "completion_tokens": 100, "cached_tokens": 0}, "test-drafting-1")

            transport = generation.FakeTransport(reply)
            result = drafting.generate(s, {"slots": slots}, Path(folder) / 'output', 100000, transport)
            self.assertEqual((result['rows'], result['invalid'], result['approvals']), (1, 0, 0))
            rows = store.read_jsonl(Path(folder) / 'output/candidates.jsonl')
            self.assertEqual(rows[0]['review']['drafted_by'], drafting.DRAFTER)
            self.assertEqual(rows[0]['generation_provenance']['model'], drafting.MODEL)
            self.assertEqual(rows[0]['evidence_groups'][1]['alternatives'][0]['offsets'][0], 0)
            self.assertEqual(budget.snapshot(s.db_path).pending_micro_usd, 0)
            self.assertEqual(len(transport.calls), 1)
            packed = json.loads(transport.calls[0]['messages'][-1]['content'])
            self.assertEqual(packed['slots'][0]['documents'][0]['doc_id'], old['scope'][0]['doc_id'])
            self.assertTrue(packed['slots'][0]['documents'][0]['institution'])
            valid['pins'][0]['quote'] = 'not in the original'
            result = drafting.generate(s, {"slots": slots}, Path(folder) / 'invalid', 100000,
                generation.FakeTransport(lambda _: generation.ProviderResponse(json.dumps({'drafts': [valid]}),
                    None, 'stop', {'prompt_tokens': 10, 'completion_tokens': 10}, 'test-drafting-2')))
            self.assertEqual((result['rows'], result['invalid']), (0, 1))
            self.assertGreater(result['settled_micro_usd'], 0)
            sealed_slot = {**slots[0], 'scope': sealed['scope']}
            with self.assertRaises(gold.GoldError):
                drafting.source_slots(s, {'slots': [sealed_slot]})
            # The answer model never moves drafting: gpt-5-mini answers, gpt-6-luna still drafts and is billed.
            mini = s.with_(generation_model='gpt-5-mini')
            luna = generation.FakeTransport(lambda _: generation.ProviderResponse(json.dumps({'drafts': [valid]}),
                None, 'stop', {'prompt_tokens': 10, 'completion_tokens': 10}, 'test-drafting-3'))
            drafting.generate(mini, {'slots': slots}, Path(folder) / 'mini', 100000, luna)
            self.assertEqual(luna.calls[-1]['model'], 'gpt-6-luna')
            with store.open_db(s.db_path) as conn:
                billed = {r[0] for r in conn.execute("SELECT model FROM attempts WHERE stage = 'gold_drafting'")}
            self.assertEqual(billed, {'gpt-6-luna'})
            with self.assertRaises(gold.GoldError):
                drafting.generate(mini, {'slots': slots}, Path(folder) / 'blocked', 0, transport)
            self.assertEqual(gold.check(s), [])


if __name__ == '__main__':
    unittest.main()
