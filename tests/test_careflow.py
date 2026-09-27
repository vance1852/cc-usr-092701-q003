from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import threading
import unittest
from datetime import UTC, datetime
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from careflow.api import create_handler
from careflow.clock import FrozenClock
from careflow.db import Database
from careflow.errors import Conflict, Forbidden, NotFound, Unauthorized, ValidationError
from careflow.service import Careflow


class CareflowCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "clinic.sqlite3")
        self.clock = FrozenClock(datetime(2026, 9, 27, 12, 0, tzinfo=UTC))
        self.app = Careflow(self.db, self.clock)
        initial = self.app.initialize_clinic("澄序门诊", "Asia/Shanghai", "诊所负责人", "LongPassphrase!2026")
        self.clinic = initial["clinic_id"]
        self.owner = initial["owner_id"]
        self.clinician = self.app.create_staff(self.clinic, "临床医生", "clinician", actor_id=self.owner)["id"]
        self.nurse = self.app.create_staff(self.clinic, "护理人员", "nurse", actor_id=self.owner)["id"]
        self.coordinator = self.app.create_staff(self.clinic, "运营协调员", "coordinator", actor_id=self.owner)["id"]
        self.patient = self.app.create_patient(self.clinic, self.coordinator, "case-017", "林女士")

    def tearDown(self):
        self.temp.cleanup()

    def document(self, purpose="weight_program", version=1, body=None, language="zh-CN",
                 covers=("复诊",), requires_resign=False):
        body = body if body is not None else f"{purpose} 第{version}版知情同意正文"
        return self.app.publish_consent_document(self.clinic, self.clinician, purpose, language,
                                                 version, body, list(covers), requires_resign=requires_resign)

    def consent(self, purpose="weight_program", expires_at=None, language="zh-CN", covers=("复诊",)):
        if not self.app.list_consent_documents(self.clinic, self.clinician, purpose=purpose, language=language):
            self.document(purpose, 1, language=language, covers=covers)
        return self.app.grant_consent(self.clinic, self.clinician, self.patient["id"], purpose,
                                      language=language, expires_at=expires_at)

    def plan(self, kind="weight"):
        consent = self.consent("weight_program" if kind == "weight" else "aesthetic_procedure")
        return self.app.create_plan(
            self.clinic, self.clinician, self.patient["id"], kind, self.clinician,
            {"description": "按门诊约定复核", "review_interval_days": 30},
            {"screening": "reviewed", "contraindications": [], "review_required": False},
            "2026-09-27", target_date="2026-12-27", consent_id=consent["id"])

    def appointment(self, key="visit-1"):
        return self.app.create_appointment(
            self.clinic, self.coordinator, self.patient["id"], "复诊",
            "2026-09-29T10:00:00+08:00", "2026-09-29T10:30:00+08:00", key, staff_id=self.clinician)

    def test_initialization_is_atomic_and_password_change_revokes_sessions(self):
        self.assertEqual(self.app.login(self.clinic, self.owner, "LongPassphrase!2026")["role"], "owner")
        token = self.app.login(self.clinic, self.owner, "LongPassphrase!2026")["access_token"]
        with self.assertRaises(Conflict):
            self.app.initialize_clinic("另一诊所", "UTC", "第二负责人", "AnotherPassphrase!2026")
        self.app.set_password(self.clinic, self.owner, self.owner, "NewPassphrase!2026")
        with self.assertRaises(Unauthorized):
            self.app.staff_for_token(self.clinic, token)
        self.assertTrue(self.app.login(self.clinic, self.owner, "NewPassphrase!2026")["access_token"])

    def test_clinic_boundary_and_role_permissions_hide_cross_clinic_records(self):
        other = self.app.create_clinic("另一诊所", "UTC")
        outsider = self.app.create_staff(other["id"], "负责人", "owner")
        with self.assertRaises(Unauthorized):
            self.app.get_patient(self.clinic, outsider["id"], self.patient["id"])
        with self.assertRaises(Forbidden):
            self.app.grant_consent(self.clinic, self.coordinator, self.patient["id"], "weight_program", language="zh-CN")
        self.assertNotIn("phone_ciphertext", self.app.get_patient(self.clinic, self.coordinator, self.patient["id"]))

    def test_withdrawal_preserves_consent_history_and_pauses_dependent_plan(self):
        plan = self.plan()
        self.app.transition_plan(self.clinic, self.clinician, plan["id"], 1, "propose")
        self.app.transition_plan(self.clinic, self.clinician, plan["id"], 2, "activate")
        consent = self.app.consent_history(self.clinic, self.clinician, self.patient["id"])[0]
        result = self.app.withdraw_consent(self.clinic, self.clinician, consent["id"], "患者提出撤回")
        self.assertEqual(result["state"], "withdrawn")
        self.assertEqual(self.app.plan_history(self.clinic, self.clinician, plan["id"])[-1]["snapshot"]["state"], "paused")
        self.assertEqual(len(self.app.consent_history(self.clinic, self.clinician, self.patient["id"])), 1)
        self.assertTrue(self.app.withdraw_consent(self.clinic, self.clinician, consent["id"], "重复请求")["replayed"])

    def test_plan_requires_signed_assessment_and_versioned_consent(self):
        consent = self.consent()
        assessment = self.app.create_assessment(self.clinic, self.clinician, self.patient["id"], "weight",
                                                {"weight_kg": "72.5", "waist_cm": 83}, {"sleep": "一般"})
        with self.assertRaises(Conflict):
            self.app.create_plan(self.clinic, self.clinician, self.patient["id"], "weight", self.clinician,
                                 {"description": "目标"}, {"review_required": True}, "2026-09-27",
                                 consent_id=consent["id"], assessment_id=assessment["id"])
        self.app.sign_assessment(self.clinic, self.clinician, assessment["id"], expected_version=1)
        plan = self.app.create_plan(self.clinic, self.clinician, self.patient["id"], "weight", self.clinician,
                                    {"description": "目标"}, {"review_required": True}, "2026-09-27",
                                    consent_id=consent["id"], assessment_id=assessment["id"])
        self.assertEqual(plan["state"], "draft")
        with self.assertRaises(Conflict):
            self.app.transition_plan(self.clinic, self.clinician, plan["id"], 1, "activate")

    def test_expired_consent_is_not_used_for_new_plan(self):
        consent = self.consent(expires_at="2026-09-27T12:01:00Z")
        self.clock.set(datetime(2026, 9, 27, 12, 2, tzinfo=UTC))
        with self.assertRaises(Conflict):
            self.app.create_plan(self.clinic, self.clinician, self.patient["id"], "weight", self.clinician,
                                 {"description": "目标"}, {}, "2026-09-27", consent_id=consent["id"])

    def test_consent_document_is_immutable_and_versioned(self):
        doc = self.document("weight_program", 1, body="第一版正文")
        self.assertEqual(doc["body_sha256"], hashlib.sha256("第一版正文".encode()).hexdigest())
        with self.assertRaises(Conflict):
            self.document("weight_program", 1, body="同版本不同正文")
        with self.assertRaises(ValidationError):
            self.document("weight_program", 0)
        with self.assertRaises(Forbidden):
            self.app.publish_consent_document(self.clinic, self.coordinator, "weight_program", "zh-CN", 9, "正文", [])
        newer = self.document("weight_program", 2, body="第二版正文", requires_resign=True)
        self.assertTrue(newer["requires_resign"])
        with self.db.transaction() as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE consent_documents SET body='篡改正文' WHERE id=?", (doc["id"],))
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM consent_documents WHERE id=?", (newer["id"],))
        restored = self.app.get_consent_document(self.clinic, self.clinician, doc["id"])
        self.assertEqual(restored["body"], "第一版正文")
        self.assertTrue(restored["integrity_ok"])

    def test_grant_records_version_language_digest_and_signing_time(self):
        doc = self.document("aesthetic_procedure", 3, body="医美术后风险说明第三版", covers=("激光", "注射"))
        consent = self.app.grant_consent(self.clinic, self.clinician, self.patient["id"],
                                         "aesthetic_procedure", document_id=doc["id"])
        self.assertEqual(consent["document_version"], 3)
        self.assertEqual(consent["language"], "zh-cn")
        self.assertEqual(consent["text_digest"], hashlib.sha256("医美术后风险说明第三版".encode()).hexdigest())
        self.assertEqual(consent["signed_at"], "2026-09-27T12:00:00Z")
        self.assertEqual(consent["covers"], ["注射", "激光"])
        with self.assertRaises(NotFound):
            self.app.grant_consent(self.clinic, self.clinician, self.patient["id"], "clinical_care", language="zh-CN")
        with self.assertRaises(ValidationError):
            self.app.grant_consent(self.clinic, self.clinician, self.patient["id"], "weight_program",
                                   document_id=doc["id"])

    def test_text_update_does_not_rewrite_existing_signature(self):
        v1 = self.document("weight_program", 1, body="术后风险说明第一版")
        self.app.grant_consent(self.clinic, self.clinician, self.patient["id"],
                               "weight_program", document_id=v1["id"])
        self.document("weight_program", 2, body="术后风险说明第二版（已调整）")
        history = self.app.consent_history(self.clinic, self.clinician, self.patient["id"], "weight_program")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["document_version"], 1)
        self.assertEqual(history[0]["text_digest"], v1["body_sha256"])
        restored = self.app.get_consent_document(self.clinic, self.clinician, history[0]["document_id"])
        self.assertEqual(restored["body"], "术后风险说明第一版")
        self.consent("data_export")
        exported = self.app.exports.export(self.clinic, self.owner, self.patient["id"], ["consents"],
                                           "投诉材料核对", "export-consents-1")
        entry = next(item for item in exported["data"]["consents"] if item["purpose"] == "weight_program")
        self.assertEqual(entry["body"], "术后风险说明第一版")
        self.assertEqual(entry["document_version"], 1)
        self.assertTrue(entry["digest_verified"])

    def test_coverage_reports_missing_covered_and_scope_expanded(self):
        coverage = self.app.check_consent_coverage(self.clinic, self.nurse, self.patient["id"], "weight_program")
        self.assertEqual(coverage["status"], "missing")
        self.document("weight_program", 1, covers=("复诊", "营养指导"))
        consent = self.app.grant_consent(self.clinic, self.clinician, self.patient["id"],
                                         "weight_program", language="zh-CN")
        coverage = self.app.check_consent_coverage(self.clinic, self.nurse, self.patient["id"],
                                                   "weight_program", items=["复诊"])
        self.assertEqual(coverage["status"], "covered")
        self.assertEqual(coverage["consent_id"], consent["id"])
        expanded = self.app.check_consent_coverage(self.clinic, self.nurse, self.patient["id"],
                                                   "weight_program", items=["复诊", "抽脂"])
        self.assertEqual(expanded["status"], "scope_expanded")
        self.assertEqual(expanded["missing_items"], ["抽脂"])

    def test_coverage_reports_resign_required_and_recovers_after_resign(self):
        self.document("weight_program", 1, body="旧版风险说明")
        self.app.grant_consent(self.clinic, self.clinician, self.patient["id"], "weight_program", language="zh-CN")
        self.document("weight_program", 2, body="调整后的术后风险说明", requires_resign=True)
        coverage = self.app.check_consent_coverage(self.clinic, self.clinician, self.patient["id"], "weight_program")
        self.assertEqual(coverage["status"], "resign_required")
        self.assertEqual(coverage["signed_version"], 1)
        self.assertEqual(coverage["latest_version"], 2)
        second = self.app.grant_consent(self.clinic, self.clinician, self.patient["id"],
                                        "weight_program", language="zh-CN")
        self.assertEqual(second["document_version"], 2)
        coverage = self.app.check_consent_coverage(self.clinic, self.clinician, self.patient["id"],
                                                   "weight_program", items=["复诊"])
        self.assertEqual(coverage["status"], "covered")
        history = self.app.consent_history(self.clinic, self.clinician, self.patient["id"], "weight_program")
        self.assertEqual([row["state"] for row in history], ["expired", "granted"])

    def test_coverage_reports_expired_and_withdrawn(self):
        self.consent("followup_contact", expires_at="2026-09-27T12:30:00Z")
        self.clock.set(datetime(2026, 9, 27, 12, 31, tzinfo=UTC))
        coverage = self.app.check_consent_coverage(self.clinic, self.coordinator, self.patient["id"], "followup_contact")
        self.assertEqual(coverage["status"], "expired")
        other = self.consent("clinical_care")
        self.app.withdraw_consent(self.clinic, self.clinician, other["id"], "患者撤回")
        coverage = self.app.check_consent_coverage(self.clinic, self.coordinator, self.patient["id"], "clinical_care")
        self.assertEqual(coverage["status"], "withdrawn")
        self.assertIsNotNone(coverage["withdrawn_at"])

    def test_withdrawal_is_not_silently_overwritten_by_later_grant(self):
        first = self.consent("weight_program")
        self.app.withdraw_consent(self.clinic, self.clinician, first["id"], "患者先提出撤回")
        second = self.app.grant_consent(self.clinic, self.owner, self.patient["id"],
                                        "weight_program", language="zh-CN")
        history = {row["id"]: row for row in
                   self.app.consent_history(self.clinic, self.clinician, self.patient["id"], "weight_program")}
        self.assertEqual(history[first["id"]]["state"], "withdrawn")
        self.assertIsNotNone(history[first["id"]]["withdrawn_at"])
        self.assertEqual(history[second["id"]]["state"], "granted")
        self.assertEqual(history[second["id"]]["supersedes"], first["id"])

    def test_superseded_consent_cannot_be_withdrawn(self):
        first = self.consent("weight_program")
        second = self.app.grant_consent(self.clinic, self.owner, self.patient["id"],
                                        "weight_program", language="zh-CN")
        with self.assertRaises(Conflict):
            self.app.withdraw_consent(self.clinic, self.owner, first["id"], "迟到的撤回")
        history = {row["id"]: row for row in
                   self.app.consent_history(self.clinic, self.clinician, self.patient["id"], "weight_program")}
        self.assertEqual(history[first["id"]]["state"], "expired")
        self.assertEqual(history[second["id"]]["state"], "granted")

    def test_completed_plan_retains_original_consent_basis(self):
        plan = self.plan()
        self.app.transition_plan(self.clinic, self.clinician, plan["id"], 1, "propose")
        self.app.transition_plan(self.clinic, self.clinician, plan["id"], 2, "activate")
        self.app.transition_plan(self.clinic, self.clinician, plan["id"], 3, "complete")
        consent = self.app.consent_history(self.clinic, self.clinician, self.patient["id"], "weight_program")[0]
        self.app.withdraw_consent(self.clinic, self.clinician, consent["id"], "完成后患者撤回")
        snapshot = self.app.plan_history(self.clinic, self.clinician, plan["id"])[-1]["snapshot"]
        self.assertEqual(snapshot["state"], "completed")
        self.assertEqual(snapshot["consent_id"], consent["id"])

    def test_appointment_hold_is_idempotent_and_expires_at_boundary(self):
        first = self.appointment()
        replay = self.appointment()
        self.assertEqual(first["id"], replay["id"])
        self.assertTrue(replay["replayed"])
        with self.assertRaises(Conflict):
            self.app.create_appointment(self.clinic, self.coordinator, self.patient["id"], "复诊",
                                        "2026-09-29T10:00:00+08:00", "2026-09-29T10:30:00+08:00", "visit-1",
                                        staff_id=self.clinician, plan_id="different")
        self.clock.set(datetime(2026, 9, 27, 12, 10, tzinfo=UTC))
        self.assertEqual(self.app.expire_holds(self.clinic)["expired"], 1)
        with self.assertRaises(Conflict):
            self.app.transition_appointment(self.clinic, self.coordinator, first["id"], 2, "book")

    def test_staff_overlap_is_rejected_but_adjacent_time_is_allowed(self):
        self.appointment("morning")
        with self.assertRaises(Conflict):
            self.app.create_appointment(self.clinic, self.coordinator, self.patient["id"], "复诊",
                                        "2026-09-29T10:15:00+08:00", "2026-09-29T10:45:00+08:00", "overlap",
                                        staff_id=self.clinician)
        adjacent = self.app.create_appointment(self.clinic, self.coordinator, self.patient["id"], "复诊",
                                              "2026-09-29T10:30:00+08:00", "2026-09-29T11:00:00+08:00", "adjacent",
                                              staff_id=self.clinician)
        self.assertEqual(adjacent["state"], "held")

    def test_observation_correction_is_append_only_and_report_uses_effective_value(self):
        original = self.app.record_observation(self.clinic, self.clinician, self.patient["id"], "weight_kg", 73.2,
                                              "2026-09-27T08:00:00+08:00")
        correction = self.app.record_observation(self.clinic, self.clinician, self.patient["id"], "weight_kg", 72.8,
                                                 "2026-09-27T08:00:00+08:00", correction_of=original["id"])
        series = self.app.reports.weight_series(self.clinic, self.clinician, self.patient["id"])
        self.assertEqual([row["weight_kg"] for row in series["observations"]], [72.8])
        self.assertEqual(correction["correction_of"], original["id"])
        with self.assertRaises(Conflict):
            self.app.record_observation(self.clinic, self.clinician, self.patient["id"], "weight_kg", 72.1,
                                         "2026-09-27T08:00:00+08:00", correction_of=original["id"])

    def test_followup_lease_fencing_prevents_late_completion(self):
        followup = self.app.schedule_followup(self.clinic, self.clinician, self.patient["id"],
                                              "2026-09-27T11:00:00Z", "复诊反馈", "fup-1")
        first = self.app.claim_followups(self.clinic, self.nurse, lease_minutes=1)[0]
        self.clock.set(datetime(2026, 9, 27, 12, 2, tzinfo=UTC))
        second = self.app.claim_followups(self.clinic, self.coordinator, lease_minutes=3)[0]
        with self.assertRaises(Conflict):
            self.app.complete_followup(self.clinic, self.nurse, followup["id"], first["claim_token"], "迟到回写", first["version"])
        done = self.app.complete_followup(self.clinic, self.coordinator, followup["id"], second["claim_token"], "已联系", second["version"])
        self.assertEqual(done["state"], "done")

    def test_incident_history_is_versioned_and_replay_does_not_duplicate(self):
        incident = self.app.report_incident(self.clinic, self.nurse, self.patient["id"], "术后不适", "moderate",
                                            "2026-09-27T10:00:00+08:00", "患者报告局部红肿", "incident-1")
        replay = self.app.report_incident(self.clinic, self.nurse, self.patient["id"], "术后不适", "moderate",
                                          "2026-09-27T10:00:00+08:00", "患者报告局部红肿", "incident-1")
        self.assertEqual(replay["id"], incident["id"])
        self.app.transition_incident(self.clinic, self.clinician, incident["id"], "triage", "安排临床评估", 1)
        history = self.app.incident_history(self.clinic, self.clinician, incident["id"])
        self.assertEqual([item["type"] for item in history["events"]], ["reported", "triage"])

    def test_stop_flag_requires_clinician_review_and_diagnostic_reports_it(self):
        flag = self.app.clinical_flags.report(self.clinic, self.nurse, self.patient["id"], "prior_reaction", "stop", "既往材料待核实")
        report = self.app.run_diagnostics(self.clinic, self.owner)
        self.assertIn("clinical_flag.requires_review", {item["code"] for item in report["findings"]})
        with self.assertRaises(Forbidden):
            self.app.clinical_flags.review(self.clinic, self.nurse, flag["id"], 1, "confirm", "已核实")
        self.app.clinical_flags.review(self.clinic, self.clinician, flag["id"], 1, "confirm", "已复核原始材料")
        flags = self.app.clinical_flags.list_for_patient(self.clinic, self.clinician, self.patient["id"])
        self.assertEqual(flags[0]["state"], "confirmed")

    def test_encounter_requires_sections_and_amendment_preserves_signed_note(self):
        appointment = self.appointment()
        self.app.transition_appointment(self.clinic, self.coordinator, appointment["id"], 1, "book")
        self.app.transition_appointment(self.clinic, self.coordinator, appointment["id"], 2, "arrive")
        self.clock.set(datetime(2026, 9, 29, 2, 0, tzinfo=UTC))
        self.app.transition_appointment(self.clinic, self.coordinator, appointment["id"], 3, "start")
        encounter = self.app.encounter_for_appointment(self.clinic, self.clinician, appointment["id"])
        with self.assertRaises(Conflict):
            self.app.sign_encounter(self.clinic, self.clinician, encounter["id"], 1)
        for section in ("chief_complaint", "assessment", "plan"):
            self.app.add_encounter_note(self.clinic, self.clinician, encounter["id"], section, f"记录-{section}",
                                        expected_version=encounter["version"])
            encounter = self.app.encounter_for_appointment(self.clinic, self.clinician, appointment["id"])
        self.app.sign_encounter(self.clinic, self.clinician, encounter["id"], encounter["version"])
        signed = self.app.encounter_notes(self.clinic, self.clinician, encounter["id"])
        first = next(item for item in signed["notes"] if item["section"] == "assessment")
        amended = self.app.add_encounter_note(self.clinic, self.clinician, encounter["id"], "assessment", "补充记录",
                                              expected_version=signed["version"], amendment_reason="补充化验时间")
        self.assertEqual(amended["state"], "amended")
        history = self.app.encounter_notes(self.clinic, self.clinician, encounter["id"])["notes"]
        self.assertTrue(any(item["id"] == first["id"] for item in history))

    def test_stock_uses_fefo_and_quarantine_blocks_consumption(self):
        product = self.app.supplies.register_product(self.clinic, self.owner, "无菌敷料", "consumable", "片")
        later = self.app.supplies.receive_lot(self.clinic, self.owner, product["id"], "s-1", "batch-later", 8,
                                              "receive-1", expires_on="2027-06-01")
        earlier = self.app.supplies.receive_lot(self.clinic, self.owner, product["id"], "s-1", "batch-earlier", 5,
                                                "receive-2", expires_on="2027-01-01")
        appointment = self.appointment()
        reserved = self.app.supplies.reserve(self.clinic, self.coordinator, appointment["id"], product["id"], 7, "stock-reserve-1")
        self.assertEqual([row["lot_id"] for row in reserved["reservations"]], [earlier["id"], later["id"]])
        self.app.supplies.change_lot_state(self.clinic, self.owner, later["id"], "recall", "批次通知召回")
        with self.assertRaises(Conflict):
            self.app.supplies.consume_reservation(self.clinic, self.clinician, reserved["reservations"][1]["id"], expected_version=1)

    def test_stock_reservation_is_all_or_nothing_and_same_request_replays(self):
        product = self.app.supplies.register_product(self.clinic, self.owner, "一次性导管", "consumable", "支")
        self.app.supplies.receive_lot(self.clinic, self.owner, product["id"], "s-2", "lot-a", 2, "receive-a")
        appointment = self.appointment()
        with self.assertRaises(Conflict):
            self.app.supplies.reserve(self.clinic, self.coordinator, appointment["id"], product["id"], 3, "reserve-too-many")
        balance = self.app.supplies.lot_balances(self.clinic, product["id"])[0]
        self.assertEqual(balance["available_quantity"], 2)
        first = self.app.supplies.reserve(self.clinic, self.coordinator, appointment["id"], product["id"], 1, "reserve-one")
        replay = self.app.supplies.reserve(self.clinic, self.coordinator, appointment["id"], product["id"], 1, "reserve-one")
        self.assertEqual(first["reservations"], replay["reservations"])
        self.assertTrue(replay["replayed"])

    def test_milestone_defer_history_and_idempotent_creation(self):
        plan = self.plan()
        first = self.app.milestones.create(self.clinic, self.clinician, plan["id"], "review", "复核体重记录",
                                           "2026-10-10T09:00:00+08:00", "mile-1", assigned_to=self.nurse)
        again = self.app.milestones.create(self.clinic, self.clinician, plan["id"], "review", "复核体重记录",
                                           "2026-10-10T09:00:00+08:00", "mile-1", assigned_to=self.nurse)
        self.assertEqual(first["id"], again["id"])
        deferred = self.app.milestones.transition(self.clinic, self.nurse, first["id"], 1, "defer",
                                                 reason="患者改期", new_due_at="2026-10-12T09:00:00+08:00")
        self.assertEqual(deferred["state"], "pending")
        self.assertEqual(len(self.app.milestones.history(self.clinic, self.clinician, first["id"])), 2)

    def test_export_needs_consent_is_minimized_and_idempotent(self):
        with self.assertRaises(Conflict):
            self.app.exports.export(self.clinic, self.owner, self.patient["id"], ["profile"], "患者本人申请", "export-1")
        self.consent("data_export")
        first = self.app.exports.export(self.clinic, self.owner, self.patient["id"], ["profile", "observations"], "患者本人申请", "export-1")
        replay = self.app.exports.export(self.clinic, self.owner, self.patient["id"], ["observations", "profile"], "患者本人申请", "export-1")
        self.assertEqual(first["sha256"], replay["sha256"])
        self.assertTrue(replay["replayed"])
        self.assertNotIn("phone_ciphertext", json.dumps(first, ensure_ascii=False))

    def test_daily_report_uses_clinic_calendar_day_and_dst_aware_bounds(self):
        clinic = self.app.create_clinic("北美诊所", "America/New_York")
        owner = self.app.create_staff(clinic["id"], "负责人", "owner")
        self.assertEqual(self.app.reports.daily_operations(clinic["id"], owner["id"], "2026-11-01")["window"]["ends_at"],
                         "2026-11-02T05:00:00Z")

    def test_audit_hash_chain_detects_tampering(self):
        self.app.audit_history(self.clinic, self.owner)
        self.assertTrue(self.app.verify_audit(self.clinic, self.owner)["ok"])
        with self.db.transaction() as connection:
            connection.execute("UPDATE audit_events SET action='tampered' WHERE sequence=1")
        self.assertFalse(self.app.verify_audit(self.clinic, self.owner)["ok"])

    def test_http_login_patient_creation_and_validation_error(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), create_handler(self.app))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            request = Request(base + "/auth/token", data=json.dumps({"staff_id": self.owner,
                            "password": "LongPassphrase!2026"}).encode(), method="POST",
                              headers={"X-Clinic-ID": self.clinic, "Content-Type": "application/json"})
            with urlopen(request, timeout=3) as response:
                token = json.loads(response.read())["access_token"]
                self.assertEqual(response.status, 201)
            request = Request(base + "/patients", data=json.dumps({"external_ref": "http-1", "name": "周女士"}).encode(),
                              method="POST", headers={"X-Clinic-ID": self.clinic, "Authorization": f"Bearer {token}",
                                                       "Content-Type": "application/json"})
            with urlopen(request, timeout=3) as response:
                patient = json.loads(response.read())
                self.assertEqual(response.status, 201)
            request = Request(base + f"/patients/{patient['id']}", headers={"X-Clinic-ID": self.clinic,
                              "Authorization": "Bearer invalid"})
            with self.assertRaises(HTTPError) as error:
                urlopen(request, timeout=3)
            self.assertEqual(error.exception.code, 401)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_http_consent_document_sign_and_coverage_flow(self):
        self.app.set_password(self.clinic, self.owner, self.clinician, "ClinicianPass!2026")
        server = ThreadingHTTPServer(("127.0.0.1", 0), create_handler(self.app))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"

        def call(method, path, payload=None, token=None):
            data = json.dumps(payload).encode() if payload is not None else None
            headers = {"X-Clinic-ID": self.clinic, "Content-Type": "application/json"}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            with urlopen(Request(base + path, data=data, method=method, headers=headers), timeout=3) as response:
                return response.status, json.loads(response.read())

        try:
            _, login = call("POST", "/auth/token", {"staff_id": self.clinician, "password": "ClinicianPass!2026"})
            token = login["access_token"]
            status, doc = call("POST", "/consent-documents",
                               {"purpose": "weight_program", "language": "zh-CN", "version": 1,
                                "body": "体重管理知情同意第一版", "covers": ["复诊"]}, token)
            self.assertEqual(status, 201)
            status, fetched = call("GET", f"/consent-documents/{doc['id']}", token=token)
            self.assertEqual((status, fetched["body"], fetched["integrity_ok"]),
                             (200, "体重管理知情同意第一版", True))
            status, consent = call("POST", f"/patients/{self.patient['id']}/consents",
                                   {"purpose": "weight_program", "language": "zh-CN"}, token)
            self.assertEqual((status, consent["document_version"], consent["language"]), (201, 1, "zh-cn"))
            status, coverage = call("GET", f"/patients/{self.patient['id']}/consent-coverage"
                                           f"?purpose=weight_program&items={quote('复诊')}", token=token)
            self.assertEqual((status, coverage["status"]), (200, "covered"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
