"""Synthetic regression checks for operator supplied research bundles."""

import copy
import unittest
from unittest.mock import MagicMock, patch

from cloud.library import Repository
from cloud.neon_http import MigrationConnection
from cloud.research import ResearchStore, validate_bundle


def bundle():
    return {
        "schema_version": 1,
        "run": {
            "period": "2026-09", "category": "arbitration", "status": "partial",
            "started_at": "2026-10-20T10:00:00+05:00",
            "finished_at": "2026-10-20T10:05:00+05:00",
            "coverage": [{"source": "Картотека", "status": "blocked", "detail": "Защитная страница"}],
            "limitations": ["Другие реестры не проверены"],
        },
        "items": [{
            "category": "arbitration", "competitor_code": "example", "status": "verified",
            "title": "Заседание по делу", "summary": "Опубликовано определение",
            "original_text": "Определение суда: заседание назначено на 15 сентября.",
            "source_name": "Картотека", "source_url": "https://court.example/case/123",
            "case_number": "А00-123/2026", "case_url": "https://court.example/case/123",
            "event_key": "doc-123",
            "published_at": "2026-09-15", "published_precision": "day", "period": "2026-09",
            "date_basis": "Дата определения в карточке дела",
            "discovered_at": "2026-10-20T10:04:00+05:00",
            "evidence": {"url": "https://court.example/case/123", "quote": "Заседание назначено",
                         "captured_at": "2026-10-20T10:04:00+05:00",
                         "snapshot_html": "<html><body><p data-document-id='doc-123'>Заседание назначено на 15 сентября.</p></body></html>"},
        }],
    }


class ResearchTests(unittest.TestCase):
    def test_active_library_exposes_research_without_promoting_to_news(self):
        data = validate_bundle(bundle(), {"example": {}})
        with patch("cloud.library.NeonHTTP") as client:
            client.return_value.query.return_value = [
                {"id": "a" * 64, "manifest": {}, "kind": record["kind"],
                 "key": record["key"], "payload": record["payload"]}
                for record in data
            ]
            library = Repository({"cloud": {"database_url":
                "postgresql://test:TEST@ep-test.neon.tech/db"}}).load()
        self.assertEqual(len(library["research_item"]), 1)
        self.assertEqual(len(library["research_run"]), 1)
        self.assertEqual(library["event"], {})

    def test_known_day_and_unknown_date_stay_distinct(self):
        data = bundle()
        records = validate_bundle(data, {"example": {}})
        self.assertEqual([r["kind"] for r in records],
                         ["research_run", "research_item_version", "research_item"])
        item = records[-1]["payload"]
        self.assertEqual(item["period"], "2026-09")
        self.assertEqual(item["published_at"], "2026-09-15")
        self.assertEqual(item["run_id"], records[0]["key"])
        unknown = copy.deepcopy(data)
        unknown["items"][0].update(published_at=None, published_precision="unknown", period=None)
        self.assertIsNone(validate_bundle(unknown, {"example": {}})[-1]["payload"]["period"])
        unknown["items"][0]["period"] = "2026-09"
        with self.assertRaisesRegex(ValueError, "Неизвестную дату"):
            validate_bundle(unknown, {"example": {}})

    def test_same_article_or_case_has_stable_id_and_new_immutable_version(self):
        first = bundle()
        later = copy.deepcopy(first)
        later["items"][0]["title"] = "Уточнённый заголовок заседания"
        later["items"][0]["summary"] = "Уточнена информация о заседании"
        later["items"][0]["discovered_at"] = "2026-10-20T10:04:30+05:00"
        later["items"][0]["evidence"]["captured_at"] = "2026-10-20T10:04:30+05:00"
        old = validate_bundle(first, {"example": {}})[-1]["payload"]
        new = validate_bundle(later, {"example": {}})[-1]["payload"]
        self.assertEqual(old["id"], new["id"])
        self.assertNotEqual(old["version"], new["version"])
        self.assertEqual(validate_bundle(first, {"example": {}}), validate_bundle(first, {"example": {}}))

    def test_same_case_events_in_two_months_keep_both_periods(self):
        september = bundle()
        october = copy.deepcopy(september)
        october["run"]["period"] = "2026-10"
        october["items"][0].update(published_at="2026-10-15", period="2026-10",
                                   event_key="doc-124",
                                   title="Следующее заседание по делу",
                                   date_basis="Дата нового определения в карточке")
        october["items"][0]["evidence"]["snapshot_html"] = (
            "<p data-document-id='doc-124'>Заседание назначено на 15 сентября.</p>")
        first = validate_bundle(september, {"example": {}})[-1]["payload"]
        second = validate_bundle(october, {"example": {}})[-1]["payload"]
        read_model = {item["id"]: item for item in (first, second)}
        self.assertEqual(len(read_model), 2)
        self.assertEqual({item["period"] for item in read_model.values()}, {"2026-09", "2026-10"})

    def test_two_court_events_on_same_day_and_one_revision_remain_distinct(self):
        data = bundle()
        second = copy.deepcopy(data["items"][0])
        second.update(event_key="doc-124", title="Второе определение")
        second["evidence"]["snapshot_html"] = (
            "<p data-document-id='doc-124'>Заседание назначено на 15 сентября.</p>")
        data["items"].append(second)
        records = validate_bundle(data, {"example": {}})
        items = [record["payload"] for record in records if record["kind"] == "research_item"]
        self.assertEqual(len({item["id"] for item in items}), 2)
        correction = copy.deepcopy(data)
        correction["items"] = [correction["items"][0]]
        correction["items"][0]["summary"] = "Уточнённый предмет первого определения"
        revised = validate_bundle(correction, {"example": {}})[-1]["payload"]
        self.assertEqual(revised["id"], items[0]["id"])
        self.assertNotEqual(revised["version"], items[0]["version"])

    def test_unkeyed_court_observations_are_separate_candidates(self):
        data = bundle()
        data["items"][0].pop("event_key")
        second = copy.deepcopy(data["items"][0])
        second["original_text"] = "Другое определение по тому же делу и в тот же день"
        data["items"].append(second)
        records = validate_bundle(data, {"example": {}})
        items = [record["payload"] for record in records if record["kind"] == "research_item"]
        self.assertEqual(len({item["id"] for item in items}), 2)
        self.assertEqual([item["status"] for item in items], ["candidate", "candidate"])

    def test_candidate_promotion_links_exact_match_without_losing_history(self):
        first = bundle()
        first["items"][0].pop("event_key")
        candidate = validate_bundle(first, {"example": {}})[-1]["payload"]
        promoted_bundle = bundle()
        promoted = validate_bundle(promoted_bundle, {"example": {}},
                                   {candidate["id"]: candidate})[-1]["payload"]
        self.assertEqual(promoted["supersedes_candidate_id"], candidate["id"])
        self.assertNotEqual(promoted["id"], candidate["id"])
        self.assertEqual(candidate["status"], "candidate")
        linked = {**candidate, "superseded_by": promoted["id"]}
        again = validate_bundle(promoted_bundle, {"example": {}},
                                {candidate["id"]: linked})[-1]["payload"]
        self.assertEqual(again["version"], promoted["version"])
        revised_bundle = copy.deepcopy(promoted_bundle)
        revised_bundle["items"][0]["summary"] = "Позднее уточнение того же определения"
        revised = validate_bundle(revised_bundle, {"example": {}},
                                  {candidate["id"]: linked})[-1]["payload"]
        self.assertEqual(revised["id"], promoted["id"])
        self.assertEqual(revised["supersedes_candidate_id"], candidate["id"])

    def test_changed_content_requires_explicit_candidate_link_and_same_scope(self):
        first = bundle()
        first["items"][0].pop("event_key")
        candidate = validate_bundle(first, {"example": {}})[-1]["payload"]
        verified = bundle()
        verified["items"][0]["title"] = "Исправленное название определения"
        with self.assertRaisesRegex(ValueError, "supersedes_candidate_id"):
            validate_bundle(verified, {"example": {}}, {candidate["id"]: candidate})
        verified["items"][0]["supersedes_candidate_id"] = candidate["id"]
        promoted = validate_bundle(verified, {"example": {}},
                                   {candidate["id"]: candidate})[-1]["payload"]
        self.assertEqual(promoted["supersedes_candidate_id"], candidate["id"])
        unrelated = {**candidate, "published_at": "2026-09-16"}
        with self.assertRaisesRegex(ValueError, "другому событию"):
            validate_bundle(verified, {"example": {}}, {candidate["id"]: unrelated})

    def test_rejects_unproven_date_unquoted_verification_and_unknown_competitor(self):
        data = bundle()
        data["items"][0]["published_at"] = "2026-09-31"
        with self.assertRaisesRegex(ValueError, "День"):
            validate_bundle(data, {"example": {}})
        data = bundle()
        data["items"][0]["evidence"]["quote"] = ""
        with self.assertRaisesRegex(ValueError, "цитату"):
            validate_bundle(data, {"example": {}})
        with self.assertRaisesRegex(ValueError, "реестре"):
            validate_bundle(bundle(), {})

    def test_success_cannot_hide_incomplete_coverage(self):
        data = bundle()
        data["run"]["status"] = "success"
        with self.assertRaisesRegex(ValueError, "Успешный обход"):
            validate_bundle(data, {"example": {}})

    def test_verified_requires_same_source_saved_html_and_visible_quote(self):
        data = bundle()
        data["items"][0]["evidence"]["url"] = "https://court.example/case/123?other=1"
        with self.assertRaisesRegex(ValueError, "не совпадает"):
            validate_bundle(data, {"example": {}})
        data = bundle()
        data["items"][0]["evidence"].pop("snapshot_html")
        with self.assertRaisesRegex(ValueError, "HTML-снимок"):
            validate_bundle(data, {"example": {}})
        data = bundle()
        data["items"][0]["evidence"]["snapshot_html"] = "<script>Заседание назначено</script><p>Другая новость</p>"
        with self.assertRaisesRegex(ValueError, "Цитата отсутствует"):
            validate_bundle(data, {"example": {}})
        data["items"][0]["status"] = "candidate"
        validate_bundle(data, {"example": {}})
        data = bundle()
        data["items"][0]["event_key"] = "doc-not-in-source"
        with self.assertRaisesRegex(ValueError, "Ключ судебного события"):
            validate_bundle(data, {"example": {}})
        data = bundle()
        data["items"][0]["event_key"] = data["items"][0]["case_url"]
        with self.assertRaisesRegex(ValueError, "а не всё дело"):
            validate_bundle(data, {"example": {}})

    def test_hidden_html_does_not_prove_quote(self):
        for concealed in ("hidden", "aria-hidden='true'", "style='display:none'",
                          "style='visibility:hidden'"):
            data = bundle()
            data["items"][0]["evidence"]["snapshot_html"] = (
                f"<p data-document-id='doc-123' {concealed}>Заседание назначено</p>"
                "<p>Иная информация</p>")
            with self.subTest(concealed=concealed), self.assertRaisesRegex(ValueError, "Цитата отсутствует"):
                validate_bundle(data, {"example": {}})

    def test_timestamps_cannot_be_future_or_exceed_run(self):
        data = bundle()
        data["items"][0]["discovered_at"] = "2099-01-01T00:00:00+00:00"
        with self.assertRaisesRegex(ValueError, "за пределы"):
            validate_bundle(data, {"example": {}})
        data = bundle()
        data["items"][0]["evidence"]["captured_at"] = "2099-01-01T00:00:00+00:00"
        with self.assertRaisesRegex(ValueError, "за пределы"):
            validate_bundle(data, {"example": {}})
        data = bundle()
        data["run"]["finished_at"] = "2099-01-01T00:00:00+00:00"
        with self.assertRaisesRegex(ValueError, "будущем"):
            validate_bundle(data, {"example": {}})

    def test_zero_year_is_not_a_calendar_month(self):
        data = bundle()
        data["run"]["period"] = "0000-09"
        with self.assertRaisesRegex(ValueError, "период"):
            validate_bundle(data, {"example": {}})
        data = bundle()
        data["items"][0].update(published_precision="month", published_at="0000-09", period="0000-09")
        with self.assertRaisesRegex(ValueError, "Месяц"):
            validate_bundle(data, {"example": {}})

    def test_import_fences_active_library_and_uses_versioned_atomic_write(self):
        client = MagicMock()
        connection = MigrationConnection(client)
        repository = MagicMock()
        repository.load.return_value = {"id": "a" * 64, "competitor": {"example": {}}}
        repository.write_connect.return_value = connection
        repository.params = {}
        outcome = ResearchStore({}, repository=repository).import_bundle(bundle())
        self.assertEqual(outcome["items"], 1)
        sql = client.query.call_args.args[0]
        self.assertIn("Active library changed", sql)
        self.assertIn("research_item_version", sql)
        self.assertIn("discovered_at", sql)
        self.assertIn("OR excluded.payload->>''status''=''verified''", sql)

    def test_promotion_sql_checks_current_candidate_and_marks_it_atomically(self):
        first = bundle()
        first["items"][0].pop("event_key")
        candidate = validate_bundle(first, {"example": {}})[-1]["payload"]
        repository = MagicMock()
        repository.load.return_value = {"id": "a" * 64, "competitor": {"example": {}},
                                        "research_item": {candidate["id"]: candidate}}
        repository.params = {}
        client = MagicMock()
        repository.write_connect.return_value = MigrationConnection(client)
        ResearchStore({}, repository=repository).import_bundle(bundle())
        sql = client.query.call_args.args[0]
        self.assertIn("FOR UPDATE", sql)
        self.assertIn("Candidate changed before promotion", sql)
        self.assertIn("jsonb_build_object(''superseded_by''", sql)
        self.assertIn("OR NOT (skb_analytics.records.payload ? ''superseded_by'')", sql)
        self.assertIn(candidate["id"], sql)

    def test_replay_old_verified_version_after_newer_one_is_noop(self):
        first = bundle()
        first["items"][0].pop("event_key")
        candidate = validate_bundle(first, {"example": {}})[-1]["payload"]
        v1_bundle = bundle()
        v1 = validate_bundle(v1_bundle, {"example": {}},
                             {candidate["id"]: candidate})[-1]["payload"]
        linked = {**candidate, "superseded_by": v1["id"]}
        v2_bundle = copy.deepcopy(v1_bundle)
        v2_bundle["items"][0]["summary"] = "Новая редакция определения"
        v2_bundle["items"][0]["discovered_at"] = "2026-10-20T10:04:30+05:00"
        v2 = validate_bundle(v2_bundle, {"example": {}},
                             {candidate["id"]: linked})[-1]["payload"]
        self.assertEqual(v1["id"], v2["id"])
        self.assertNotEqual(v1["version"], v2["version"])
        repository = MagicMock()
        repository.load.return_value = {"id": "a" * 64, "competitor": {"example": {}},
                                        "research_item": {candidate["id"]: linked, v2["id"]: v2}}
        repository.params = {}
        client = MagicMock()
        repository.write_connect.return_value = MigrationConnection(client)
        ResearchStore({}, repository=repository).import_bundle(v1_bundle)
        sql = client.query.call_args.args[0]
        self.assertLess(sql.index("INTO replay_versions"), sql.index("INSERT INTO skb_analytics.records"))
        self.assertIn("replay_versions ? (link->>''version'')", sql)
        self.assertIn("previous->>''superseded_by''=link->>''id''", sql)
        self.assertIn("CONTINUE;", sql)


if __name__ == "__main__":
    unittest.main()
