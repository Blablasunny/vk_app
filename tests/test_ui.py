# -*- coding: utf-8 -*-
import os
import sys

import pytest
from streamlit.testing.v1 import AppTest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import core  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setenv("VK_DB_PATH", str(tmp_path / "ui.db"))
    core.DB_PATH = str(tmp_path / "ui.db")
    core.init_db()
    yield


def run():
    at = AppTest.from_file(os.path.join(ROOT, "app.py"), default_timeout=30)
    at.run()
    return at


def login(at, username, password=core.DEMO_PASSWORD):
    at.text_input[0].set_value(username)
    at.text_input[1].set_value(password)
    at.button[0].click()
    at.run()
    return at


def test_login_page_and_wrong_password():
    at = run()
    assert not at.exception
    login(at, "student01", "bad")
    assert any("Неверный логин" in e.value for e in at.error)


@pytest.mark.parametrize("user,header", [
    ("student01", "Моя заявка"),
    ("operator1", "Очередь заявок"),
    ("edu_head", "Сводка по заявкам"),
    ("kafedra_head", "Приём заявок и итоговый список"),
    ("it_service", "Почтовый сервис и тестовые данные"),
])
def test_each_role_sees_own_page(user, header):
    at = login(run(), user)
    assert not at.exception, at.exception
    assert any(header in h.value for h in at.header)


def test_student_sees_submit_form_then_blocked_when_closed():
    at = login(run(), "student01")
    assert any("Подача заявки" in s.value for s in at.subheader)
    core.set_reception_open(False)
    at = login(run(), "student01")
    assert any("закрыт" in i.value for i in at.info)


def test_operator_changes_status_and_student_sees_letter():
    files = {d: (f"{d}.pdf", core.DUMMY_PDF) for d in core.REQUIRED_DOCS}
    core.submit_application(1, 1, files)
    at = login(run(), "operator1")
    assert not at.exception
    at.radio[0].set_value(core.S_OK)
    next(b for b in at.button if b.label == "Сохранить статус").click()
    at.run()
    assert not at.exception, at.exception
    assert core.get_student_application(1)["status"] == core.S_OK
    at2 = login(run(), "student01")
    assert any(core.S_OK in m.value for m in at2.markdown)
    assert len(core.student_notifications(1)) == 1


def test_operator_cannot_reject_without_comment_shows_error():
    files = {d: (f"{d}.pdf", core.DUMMY_PDF) for d in core.REQUIRED_DOCS}
    core.submit_application(1, 1, files)
    at = login(run(), "operator1")
    at.radio[0].set_value(core.S_NO)
    next(b for b in at.button if b.label == "Сохранить статус").click()
    at.run()
    assert any("комментарий" in e.value for e in at.error)
    assert core.get_student_application(1)["status"] == core.S_NEW


def test_kafedra_head_forms_list():
    core.seed_demo_applications()
    for app in core.list_applications():
        core.change_status(app["id"], core.S_OK, "", [], "t")
    core.set_reception_open(False)
    at = login(run(), "kafedra_head")
    next(b for b in at.button if "Сформировать" in b.label).click()
    at.run()
    assert not at.exception, at.exception
    assert core.list_formed()
    assert len(core.final_list_rows()) == 5      # 6 принятых, на первой специальности 2 места при 3 заявках
    assert len(core.surplus_rows()) == 1
