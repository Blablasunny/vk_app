# -*- coding: utf-8 -*-
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import core  # noqa: E402
from core import AppError  # noqa: E402

PDF = core.DUMMY_PDF
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 20
JPG = b"\xff\xd8\xff" + b"0" * 20


def all_docs(ext="pdf", data=PDF):
    return {d: (f"{d}.{ext}", data) for d in core.REQUIRED_DOCS}


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    core.DB_PATH = str(tmp_path / "t.db")
    core.init_db()
    yield


# --- вход ---------------------------------------------------------------
def test_login_ok_and_wrong_password():
    assert core.authenticate("student01", core.DEMO_PASSWORD)["role"] == core.R_STUDENT
    assert core.authenticate("student01", "wrong") is None
    assert core.authenticate("nobody", core.DEMO_PASSWORD) is None
    assert core.authenticate("", "") is None


# --- подача заявки -------------------------------------------------------
def test_submit_ok_creates_new_status():
    app_id = core.submit_application(1, 1, all_docs())
    app = core.get_student_application(1)
    assert app["id"] == app_id and app["status"] == core.S_NEW
    assert len(core.get_documents(app_id)) == 3


def test_submit_without_required_doc_rejected_and_names_missing():
    files = all_docs()
    del files["Паспорт"]
    with pytest.raises(AppError) as e:
        core.submit_application(1, 1, files)
    assert "Паспорт" in str(e.value)
    assert core.get_student_application(1) is None


def test_double_submit_no_duplicate():
    core.submit_application(1, 1, all_docs())
    with pytest.raises(AppError):
        core.submit_application(1, 1, all_docs())
    assert len(core.list_applications()) == 1


@pytest.mark.parametrize("name,data", [
    ("a.exe", PDF), ("a.pdf", b"not a pdf"), ("a.png", PDF), ("a.pdf", b""),
    ("a.pdf", b"%PDF-" + b"0" * (core.MAX_FILE_BYTES + 1)),
])
def test_invalid_files_rejected(name, data):
    files = all_docs()
    files["Паспорт"] = (name, data)
    with pytest.raises(AppError):
        core.submit_application(1, 1, files)


def test_file_size_boundary():
    ok = b"%PDF-" + b"0" * (core.MAX_FILE_BYTES - 5)       # ровно 5 МБ
    files = all_docs()
    files["Паспорт"] = ("p.pdf", ok)
    core.submit_application(1, 1, files)


def test_png_and_jpg_accepted():
    files = {d: (f"{d}.png", PNG) for d in core.REQUIRED_DOCS}
    files["Заявление"] = ("z.jpg", JPG)
    core.submit_application(1, 1, files)


def test_reception_closed_blocks_submit():
    core.set_reception_open(False)
    with pytest.raises(AppError):
        core.submit_application(1, 1, all_docs())


def test_unknown_specialty():
    with pytest.raises(AppError):
        core.submit_application(1, 999, all_docs())


# --- статусы и письма -----------------------------------------------------
def test_status_ok_sends_mail():
    aid = core.submit_application(1, 1, all_docs())
    assert core.change_status(aid, core.S_OK, "", [], "op") is True
    mails = core.list_notifications()
    assert len(mails) == 1 and mails[0]["state"] == "отправлено"
    assert mails[0]["delay_sec"] <= core.MAIL_SLA_SECONDS


def test_fix_requires_missing_docs_and_reject_requires_comment():
    aid = core.submit_application(1, 1, all_docs())
    with pytest.raises(AppError):
        core.change_status(aid, core.S_FIX, "нужен скан", [], "op")
    with pytest.raises(AppError):
        core.change_status(aid, core.S_NO, "  ", [], "op")
    with pytest.raises(AppError):
        core.change_status(aid, "что-то", "", [], "op")
    assert core.get_application(aid)["status"] == core.S_NEW


def test_repeating_same_status_changes_nothing():
    aid = core.submit_application(1, 1, all_docs())
    core.change_status(aid, core.S_OK, "ок", [], "op")
    assert core.change_status(aid, core.S_OK, "ок", [], "op") is False
    assert len(core.list_notifications()) == 1
    assert len(core.get_status_log(aid)) == 2


def test_status_history_keeps_previous_value():
    aid = core.submit_application(1, 1, all_docs())
    core.change_status(aid, core.S_FIX, "нет скана", ["Паспорт"], "op")
    core.change_status(aid, core.S_OK, "", [], "op")
    log = core.get_status_log(aid)
    assert [r["new_status"] for r in log] == [core.S_NEW, core.S_FIX, core.S_OK]
    assert log[2]["old_status"] == core.S_FIX


def test_mail_down_flags_and_retry():
    core.set_mail_available(False)
    aid = core.submit_application(1, 1, all_docs())
    core.change_status(aid, core.S_OK, "", [], "op")
    failed = core.list_notifications(only_failed=True)
    assert len(failed) == 1 and failed[0]["state"] == "письмо не ушло"
    with pytest.raises(AppError):
        core.retry_notification(failed[0]["id"])
    core.set_mail_available(True)
    assert core.retry_notification(failed[0]["id"]) is True
    assert core.list_notifications(only_failed=True) == []


def test_student_sees_only_sent_mails():
    core.set_mail_available(False)
    aid = core.submit_application(1, 1, all_docs())
    core.change_status(aid, core.S_OK, "", [], "op")
    assert core.student_notifications(1) == []


# --- доработка -------------------------------------------------------------
def test_resubmit_after_fix_returns_to_queue():
    aid = core.submit_application(1, 1, all_docs())
    core.change_status(aid, core.S_FIX, "скан нечитаем", ["Паспорт"], "op")
    with pytest.raises(AppError):
        core.resubmit_documents(1, {})
    core.resubmit_documents(1, {"Паспорт": ("new.pdf", PDF)})
    app = core.get_student_application(1)
    assert app["status"] == core.S_NEW and app["missing_docs"] == []
    docs = {d["doc_type"]: d["filename"] for d in core.get_documents(aid)}
    assert docs["Паспорт"] == "new.pdf"


def test_resubmit_not_allowed_in_other_status():
    core.submit_application(1, 1, all_docs())
    with pytest.raises(AppError):
        core.resubmit_documents(1, {"Паспорт": ("n.pdf", PDF)})


def test_rejected_student_cannot_resubmit_in_v1():
    aid = core.submit_application(1, 1, all_docs())
    core.change_status(aid, core.S_NO, "не подходит", [], "op")
    with pytest.raises(AppError) as e:
        core.submit_application(1, 1, all_docs())
    assert "повторной подачи" in str(e.value)


# --- очередь ---------------------------------------------------------------
def test_queue_order_and_filter():
    a = core.submit_application(1, 1, all_docs())
    b = core.submit_application(2, 1, all_docs())
    c = core.submit_application(3, 2, all_docs())
    core.change_status(b, core.S_OK, "", [], "op")
    ids = [r["id"] for r in core.list_applications()]
    assert ids == [a, b, c]
    assert [r["id"] for r in core.list_applications(core.S_NEW)] == [a, c]
    assert [r["id"] for r in core.list_applications(core.S_OK)] == [b]


# --- итоговый список ----------------------------------------------------------
def accept_all(n_spec1=3):
    ids = []
    for sid in range(1, n_spec1 + 1):
        ids.append(core.submit_application(sid, 1, all_docs()))
    for aid in ids:
        core.change_status(aid, core.S_OK, "", [], "op")
    return ids


def test_list_blocked_while_open_or_unprocessed():
    core.submit_application(1, 1, all_docs())
    with pytest.raises(AppError) as e:
        core.form_final_list()
    assert "открыт" in str(e.value) and "Не обработано" in str(e.value)
    core.set_reception_open(False)
    with pytest.raises(AppError):
        core.form_final_list()


def test_list_respects_seats_and_reports_surplus():
    ids = accept_all(3)                      # специальность 1: 2 места, 3 принятых
    core.set_reception_open(False)
    core.form_final_list()
    in_list = [r["app_id"] for r in core.final_list_rows()]
    assert in_list == ids[:2]
    assert [r["app_id"] for r in core.surplus_rows()] == [ids[2]]


def test_form_list_is_idempotent():
    accept_all(3)
    core.set_reception_open(False)
    core.form_final_list()
    core.form_final_list()
    assert len(core.final_list_rows()) == 2


def test_only_accepted_go_to_list():
    a = core.submit_application(1, 1, all_docs())
    b = core.submit_application(2, 1, all_docs())
    core.change_status(a, core.S_OK, "", [], "op")
    core.change_status(b, core.S_NO, "причина", [], "op")
    core.set_reception_open(False)
    core.form_final_list()
    assert [r["app_id"] for r in core.final_list_rows()] == [a]


def test_status_locked_after_list_and_unlock_by_decision():
    ids = accept_all(1)
    core.set_reception_open(False)
    core.form_final_list()
    with pytest.raises(AppError):
        core.change_status(ids[0], core.S_NO, "поздно", [], "op")
    with pytest.raises(AppError):
        core.unlock_application(ids[0], " ", "head")
    core.unlock_application(ids[0], "ошибка в документах", "head")
    assert core.change_status(ids[0], core.S_NO, "документы недействительны", [], "op") is True


def test_include_over_limit_needs_reason():
    ids = accept_all(3)
    core.set_reception_open(False)
    core.form_final_list()
    with pytest.raises(AppError):
        core.include_over_limit(ids[2], "", "head")
    core.include_over_limit(ids[2], "решение начальника кафедры", "head")
    assert len(core.final_list_rows()) == 3 and core.surplus_rows() == []


def test_reception_cannot_reopen_after_list():
    accept_all(1)
    core.set_reception_open(False)
    core.form_final_list()
    with pytest.raises(AppError):
        core.set_reception_open(True)


# --- сводка ------------------------------------------------------------------
def test_summary_counts_and_period_validation():
    a = core.submit_application(1, 1, all_docs())
    core.submit_application(2, 1, all_docs())
    core.change_status(a, core.S_OK, "", [], "op")
    today = core.now_dt().strftime("%Y-%m-%d")
    counts = core.summary(today, today)
    assert counts[core.S_OK] == 1 and counts[core.S_NEW] == 1
    assert core.summary("2000-01-01", "2000-01-02")[core.S_OK] == 0
    with pytest.raises(AppError):
        core.summary("2026-02-01", "2026-01-01")


# --- тестовые данные ------------------------------------------------------------
def test_seed_demo_and_reset():
    assert core.seed_demo_applications() == 6
    assert core.seed_demo_applications() == 0
    core.reset_db()
    assert core.list_applications() == []
