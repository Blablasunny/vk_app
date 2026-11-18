# -*- coding: utf-8 -*-
"""Система обслуживания заявок на поступление на военную кафедру — интерфейс Streamlit."""
from datetime import date, timedelta

import pandas as pd
import streamlit as st

import core
from core import AppError

st.set_page_config(page_title="Заявки на военную кафедру", page_icon="🎖️", layout="wide")


@st.cache_resource
def _init():
    core.init_db()
    return True


_init()


# ---------------------------------------------------------------------------
# вспомогательное
# ---------------------------------------------------------------------------
def fmt_files(uploaded):
    """UploadedFile -> (имя, байты) или None."""
    if uploaded is None:
        return None
    return (uploaded.name, uploaded.getvalue())


def show_error(e):
    st.error(str(e))


def flash(text, kind="success"):
    """Сообщение, которое переживёт st.rerun()."""
    st.session_state["flash"] = (kind, text)


def status_badge(status):
    colors = {core.S_NEW: "blue", core.S_OK: "green", core.S_FIX: "orange", core.S_NO: "red"}
    return f":{colors.get(status, 'gray')}[**{status}**]"


# ---------------------------------------------------------------------------
# вход
# ---------------------------------------------------------------------------
def login_page():
    st.title("Заявки на поступление на военную кафедру")
    st.caption(f"Учебный прототип, версия {core.APP_VERSION}. Вход имитирует университетскую учётную запись.")
    st.warning("Не загружайте реальные документы и персональные данные: используйте только тестовые файлы.")
    with st.form("login"):
        username = st.text_input("Логин")
        password = st.text_input("Пароль", type="password")
        ok = st.form_submit_button("Войти")
    if ok:
        user = core.authenticate(username, password)
        if user is None:
            st.error("Неверный логин или пароль.")
        else:
            st.session_state["user"] = user
            st.rerun()
    with st.expander("Тестовые учётные записи"):
        st.markdown(
            f"Пароль у всех: `{core.DEMO_PASSWORD}`\n\n"
            "- Студенты: `student01` … `student10`\n"
            "- Оператор: `operator1`\n"
            "- Начальник учебной части: `edu_head`\n"
            "- Начальник военной кафедры: `kafedra_head`\n"
            "- ИТ-служба: `it_service`"
        )


# ---------------------------------------------------------------------------
# студент
# ---------------------------------------------------------------------------
def student_page(user):
    st.header("Моя заявка")
    student = core.get_student(user["student_id"])
    with st.container(border=True):
        st.markdown(
            f"**{student['full_name']}** · группа {student['group_name']} · "
            f"студенческий билет {student['ticket_no']} · {student['email']}"
        )
    app = core.get_student_application(user["student_id"])

    if app is None:
        if not core.reception_open():
            st.info("Приём заявок закрыт.")
            return
        st.subheader("Подача заявки")
        specs = core.list_specialties()
        with st.form("submit_app"):
            spec_name = st.selectbox("Специальность", [s["name"] for s in specs])
            uploads = {}
            for d in core.REQUIRED_DOCS:
                uploads[d] = st.file_uploader(f"{d} (PDF, PNG или JPG, до 5 МБ)", key=f"up_{d}",
                                              type=["pdf", "png", "jpg", "jpeg"])
            sent = st.form_submit_button("Отправить заявку")
        if sent:
            spec_id = next(s["id"] for s in specs if s["name"] == spec_name)
            files = {d: fmt_files(u) for d, u in uploads.items() if u is not None}
            try:
                core.submit_application(user["student_id"], spec_id, files)
                flash("Заявка принята к рассмотрению.")
                st.rerun()
            except AppError as e:
                show_error(e)
        return

    st.subheader("Статус заявки")
    st.markdown(f"Статус: {status_badge(app['status'])}")
    st.write(f"Специальность: {app['specialty']}")
    st.write(f"Подана: {app['created_at']} · обновлена: {app['updated_at']}")
    if app["comment"]:
        st.info(f"Комментарий оператора: {app['comment']}")

    if app["status"] == core.S_FIX:
        st.warning("Нужно донести документы: " + "; ".join(app["missing_docs"]) + ".")
        with st.form("resubmit"):
            uploads = {}
            for d in app["missing_docs"]:
                uploads[d] = st.file_uploader(f"{d} (PDF, PNG или JPG, до 5 МБ)", key=f"re_{d}",
                                              type=["pdf", "png", "jpg", "jpeg"])
            sent = st.form_submit_button("Отправить документы")
        if sent:
            files = {d: fmt_files(u) for d, u in uploads.items() if u is not None}
            try:
                core.resubmit_documents(user["student_id"], files)
                flash("Документы отправлены, заявка снова на рассмотрении.")
                st.rerun()
            except AppError as e:
                show_error(e)
    elif app["status"] == core.S_NO:
        st.error("Заявка отклонена. Порядок повторной подачи уточняется — обратитесь в учебную часть.")

    mails = core.student_notifications(user["student_id"])
    st.subheader("Письма")
    if not mails:
        st.caption("Писем пока нет.")
    for m in mails:
        with st.expander(f"{m['sent_at']} — {m['subject']}"):
            st.text(m["body"])


# ---------------------------------------------------------------------------
# оператор
# ---------------------------------------------------------------------------
def operator_page(user):
    st.header("Очередь заявок")
    filt = st.selectbox("Фильтр по статусу", ["Все"] + [core.S_NEW, core.S_FIX, core.S_OK, core.S_NO],
                        index=1, key="op_filter")
    rows = core.list_applications(None if filt == "Все" else filt)
    if not rows:
        st.info("Заявок с таким статусом нет.")
        return
    df = pd.DataFrame(rows).rename(columns={
        "id": "№", "status": "Статус", "created_at": "Подана", "updated_at": "Обновлена",
        "full_name": "Студент", "group_name": "Группа", "specialty": "Специальность", "in_list": "В списке"})
    df["В списке"] = df["В списке"].map({1: "да", 0: ""})
    st.dataframe(df, hide_index=True, width="stretch")

    options = {f"№{r['id']} — {r['full_name']} ({r['status']})": r["id"] for r in rows}
    chosen = st.selectbox("Открыть заявку", list(options.keys()), key="op_choose")
    app = core.get_application(options[chosen])

    st.divider()
    st.subheader(f"Заявка №{app['id']}")
    st.write(f"**{app['full_name']}**, группа {app['group_name']}, билет {app['ticket_no']}, {app['email']}")
    st.write(f"Специальность: {app['specialty']} · статус: {status_badge(app['status'])}")
    for d in core.get_documents(app["id"]):
        st.download_button(f"{d['doc_type']}: {d['filename']}", data=d["content"], file_name=d["filename"],
                           key=f"dl_{app['id']}_{d['id']}")

    if app["in_list"]:
        st.warning("Заявка включена в итоговый список. Изменить статус можно только по решению "
                   "начальника военной кафедры.")
    else:
        with st.form(f"status_{app['id']}"):
            new_status = st.radio("Новый статус", list(core.OPERATOR_STATUSES), horizontal=True)
            missing = st.multiselect("Недостающие документы (для «на доработке»)", core.REQUIRED_DOCS)
            comment = st.text_area("Комментарий студенту")
            ok = st.form_submit_button("Сохранить статус")
        if ok:
            try:
                changed = core.change_status(app["id"], new_status, comment, missing, user["display_name"])
                if changed:
                    flash("Статус сохранён, письмо поставлено в отправку.")
                else:
                    flash("Ничего не изменилось: такой статус уже стоит.", "info")
                st.rerun()
            except AppError as e:
                show_error(e)

    with st.expander("Журнал статусов"):
        st.dataframe(pd.DataFrame(core.get_status_log(app["id"]))[
            ["changed_at", "old_status", "new_status", "comment", "changed_by"]
        ].rename(columns={"changed_at": "Время", "old_status": "Был", "new_status": "Стал",
                          "comment": "Комментарий", "changed_by": "Кто"}),
            hide_index=True, width="stretch")


# ---------------------------------------------------------------------------
# начальник учебной части
# ---------------------------------------------------------------------------
def edu_head_page(user):
    st.header("Сводка по заявкам")
    c1, c2 = st.columns(2)
    d_from = c1.date_input("С даты", value=date.today() - timedelta(days=30))
    d_to = c2.date_input("По дату", value=date.today())
    try:
        counts = core.summary(d_from.isoformat(), d_to.isoformat())
    except AppError as e:
        show_error(e)
        return
    cols = st.columns(4)
    for col, (status, n) in zip(cols, counts.items()):
        col.metric(status, n)
    hours = st.number_input("Считать задержкой, если заявка не обработана дольше (часов)", min_value=1,
                            value=24, step=1)
    stale = core.stale_new_applications(int(hours))
    if stale:
        st.warning(f"Заявок без обработки дольше {int(hours)} ч: {len(stale)}")
        st.dataframe(pd.DataFrame(stale).rename(columns={"id": "№", "full_name": "Студент",
                                                         "created_at": "Подана"}),
                     hide_index=True, width="stretch")
    else:
        st.success("Задержек нет.")


# ---------------------------------------------------------------------------
# начальник военной кафедры
# ---------------------------------------------------------------------------
def kafedra_head_page(user):
    st.header("Приём заявок и итоговый список")
    is_open = core.reception_open()
    st.markdown(f"Приём заявок: **{'открыт' if is_open else 'закрыт'}**")
    formed = core.list_formed()
    c1, c2 = st.columns(2)
    if is_open:
        if c1.button("Закрыть приём заявок"):
            try:
                core.set_reception_open(False)
                st.rerun()
            except AppError as e:
                show_error(e)
    else:
        if c1.button("Открыть приём заявок"):
            try:
                core.set_reception_open(True)
                st.rerun()
            except AppError as e:
                show_error(e)

    st.subheader("Места по специальностям")
    st.dataframe(pd.DataFrame(core.seats_summary()).rename(columns={
        "name": "Специальность", "seats": "Мест", "accepted": "Принято заявок", "in_list": "В списке"
    }).drop(columns=["id"]), hide_index=True, width="stretch")

    st.subheader("Итоговый список")
    blockers = core.list_blockers()
    if blockers:
        st.info(" ".join(blockers))
    if st.button("Сформировать итоговый список"):
        try:
            core.form_final_list()
            flash("Список сформирован.")
            st.rerun()
        except AppError as e:
            show_error(e)

    rows = core.final_list_rows()
    if rows:
        df = pd.DataFrame(rows).rename(columns={
            "specialty": "Специальность", "full_name": "Студент", "group_name": "Группа",
            "ticket_no": "Билет", "app_id": "№ заявки", "list_decision": "Решение сверх мест"})
        st.dataframe(df, hide_index=True, width="stretch")
        st.download_button("Скачать список (CSV)", df.to_csv(index=False).encode("utf-8-sig"),
                           file_name="itogovyj_spisok.csv", mime="text/csv")
    elif formed:
        st.caption("Список пуст.")

    surplus = core.surplus_rows()
    if surplus:
        st.subheader("Превышение числа мест")
        st.warning("Эти заявки приняты, но мест не хватило. Без решения начальника кафедры в список они не попадают.")
        for r in surplus:
            with st.expander(f"№{r['app_id']} — {r['full_name']} ({r['specialty']})"):
                reason = st.text_input("Основание решения", key=f"sur_{r['app_id']}")
                if st.button("Включить сверх мест", key=f"inc_{r['app_id']}"):
                    try:
                        core.include_over_limit(r["app_id"], reason, user["display_name"])
                        st.rerun()
                    except AppError as e:
                        show_error(e)

    if rows:
        st.subheader("Разблокировать заявку из списка")
        opts = {f"№{r['app_id']} — {r['full_name']}": r["app_id"] for r in rows}
        pick = st.selectbox("Заявка", list(opts.keys()), key="unlock_pick")
        reason = st.text_input("Основание", key="unlock_reason")
        if st.button("Снять блокировку статуса"):
            try:
                core.unlock_application(opts[pick], reason, user["display_name"])
                flash("Блокировка снята. Оператор может изменить статус, затем список нужно сформировать заново.")
                st.rerun()
            except AppError as e:
                show_error(e)


# ---------------------------------------------------------------------------
# ИТ-служба
# ---------------------------------------------------------------------------
def it_page(user):
    st.header("Почтовый сервис и тестовые данные")
    avail = core.mail_available()
    st.markdown(f"Почтовый сервис: **{'доступен' if avail else 'недоступен (имитация сбоя)'}**")
    if avail:
        if st.button("Имитировать недоступность почты"):
            core.set_mail_available(False)
            st.rerun()
    else:
        if st.button("Восстановить почтовый сервис"):
            core.set_mail_available(True)
            st.rerun()

    st.subheader("Отправленные письма")
    mails = core.list_notifications()
    if mails:
        df = pd.DataFrame(mails)[["id", "full_name", "to_email", "subject", "state", "created_at",
                                  "sent_at", "delay_sec"]].rename(columns={
            "id": "№", "full_name": "Студент", "to_email": "Кому", "subject": "Тема", "state": "Состояние",
            "created_at": "Создано", "sent_at": "Отправлено", "delay_sec": "Задержка, с"})
        st.dataframe(df, hide_index=True, width="stretch")
        slow = [m for m in mails if m["delay_sec"] > core.MAIL_SLA_SECONDS]
        if slow:
            st.error(f"Писем со сроком больше 5 минут: {len(slow)}")
    else:
        st.caption("Писем пока нет.")

    failed = core.list_notifications(only_failed=True)
    if failed:
        st.subheader("Письма, которые не ушли")
        for m in failed:
            cols = st.columns([4, 1])
            cols[0].write(f"№{m['id']} — {m['full_name']}: {m['subject']}")
            if cols[1].button("Повторить", key=f"retry_{m['id']}"):
                try:
                    core.retry_notification(m["id"])
                    st.rerun()
                except AppError as e:
                    show_error(e)

    st.subheader("Тестовые данные")
    c1, c2 = st.columns(2)
    if c1.button("Создать демо-заявки (студенты 05–10)"):
        try:
            n = core.seed_demo_applications()
            flash(f"Создано заявок: {n}.")
            st.rerun()
        except AppError as e:
            show_error(e)
    if c2.button("Сбросить все данные к исходным"):
        core.reset_db()
        st.session_state.clear()
        st.rerun()


# ---------------------------------------------------------------------------
# каркас
# ---------------------------------------------------------------------------
PAGES = {
    core.R_STUDENT: student_page,
    core.R_OPERATOR: operator_page,
    core.R_EDU_HEAD: edu_head_page,
    core.R_KAF_HEAD: kafedra_head_page,
    core.R_IT: it_page,
}


def main():
    user = st.session_state.get("user")
    if not user:
        login_page()
        return
    msg = st.session_state.pop("flash", None)
    if msg:
        getattr(st, {"success": "success", "info": "info", "error": "error"}[msg[0]])(msg[1])
    with st.sidebar:
        st.markdown(f"**{user['display_name']}**")
        st.caption(core.ROLE_TITLES[user["role"]])
        if st.button("Выйти"):
            st.session_state.clear()
            st.rerun()
        st.divider()
        st.caption(f"Версия {core.APP_VERSION}")
    PAGES[user["role"]](user)


main()
