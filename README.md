# Конкрентная аналитика СКБ ИНДУКЦИЯ

Первый этап облачного размещения: Streamlit, вход через Google, доступ по
приглашениям и проверка подключений Neon / Google Drive.

**Это ещё не полный мониторинг.** Перенос публикаций, сборщика, редактора и
архива выполняется следующим этапом. В этом репозитории нет рабочих баз,
отчётов, снимков сайтов, персональных настроек и ключей.

## Первый запуск в Streamlit Community Cloud

1. Открыть https://share.streamlit.io/ и нажать **Create app**.
2. Выбрать репозиторий `12bladick12/skb-competitor-analytics`.
3. Ветка: `main`. Main file path: `streamlit_app.py`.
4. Выбрать доступный адрес, например `skb-competitor-analytics`.
5. В Advanced settings выбрать Python **3.13**. На первом запуске Secrets
   можно оставить пустыми: приложение покажет только экран подготовки.
6. Нажать **Deploy** и сохранить фактический HTTPS-адрес приложения.

Адрес может отличаться от примера, если имя уже занято. До настройки Google
и приглашений приложение не открывает доступ ни одному пользователю.

## Google-вход

В Google Cloud выбрать проект приложения. Открыть Google Auth Platform:

1. **Branding** — заполнить название приложения и контакт владельца.
2. **Audience** — для обычных Google-аккаунтов выбрать External. В режиме
   Testing добавить Google-аккаунт администратора и тестовых сотрудников.
3. **Clients → Create client → Web application** — создать клиент входа.
4. В **Authorized redirect URIs** добавить точный адрес вида
   `https://ВАШ-АДРЕС.streamlit.app/oauth2callback`.
5. В Streamlit **App settings → Secrets** вставить заполненный шаблон из
   `.streamlit/secrets.example.toml`: раздел `[auth]` и список `[access]`.

В `admin_emails` указать собственный email Google, например
`admin_emails = ["owner@example.com"]`, заменив пример своим адресом.
Значения cookie_secret, client_id и client_secret хранить только в Secrets.
Случайный cookie_secret должен иметь минимум 32 символа. На рабочем компьютере
его создаёт `python scripts/prepare_cloud_secrets.py`, не выводя значение на экран.

Вход через Google подтверждает личность, а `[access]` задаёт разрешение входа.
Пользователь, которого нет в списке, не получает доступ. В первой версии
администратор приглашает сотрудника, добавляя его точный Google-email в
`viewer_emails`, `editor_emails` или `admin_emails`, и передаёт ссылку вручную.
После изменения Secrets дождаться применения настроек/перезапуска Streamlit.
Права проверяются при каждом действии. Уже показанную информацию отозвать
из браузера нельзя; последующее обращение после применения настроек будет отклонено.

## Подключения

**Neon:** в `[cloud]` вставить PostgreSQL URL из Neon Connect, сохранив TLS.
Кнопка администратора выполняет только `SELECT 1`, без создания таблиц.

**Google Drive:** в `[drive]` требуются OAuth client_id, client_secret и
refresh_token владельца файлов с разрешением `drive.file`. Одного включения
Drive API недостаточно. Это подключение оформляется отдельно от входа сотрудников;
не публикуйте токены в репозитории, issues или чатах. Перед рабочим использованием
настроить OAuth вне режима Testing, чтобы refresh token для Drive не истекал
через семь дней. При необходимости пройти требования Google к публикации OAuth.

Проверка Drive читает квоту, включая место Gmail и Photos. Она не загружает
и не удаляет файлы, не проверяет запись. Успешные проверки не означают, что
миграция мониторинга или проверка его облачного сборщика уже выполнены.

## Локальный запуск и проверки

```text
python -m venv .venv
python -m pip install -r requirements.txt
python scripts/prepare_cloud_secrets.py
python -m streamlit run streamlit_app.py
python -m unittest discover -s tests -p "test_cloud_*.py" -v
```

Перед командами активировать созданное окружение. Первый запуск без заполненных
Secrets предназначен для проверки загрузки страницы. Google-вход в этой конфигурации
проверяется на HTTPS-адресе Community Cloud. Локальный HTTP callback не включён.

Локальная проверка подключений владельцем:

```text
python scripts/check_cloud_readiness.py --offline
python scripts/check_cloud_readiness.py
```

Тесты используют подмены внешних сервисов и не обращаются к сайтам конкурентов.
Настоящий Google-вход проверяется отдельно после настройки OAuth.

## Бесплатный режим

Используются Community Cloud, Neon Free и бесплатное место аккаунта Google.
Квоты ограничены. Приложение может засыпать, а посетитель — пробуждать его.
Постоянный сборщик и расписание 24/7 на этом этапе не запускаются.

- [Размещение Streamlit](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/deploy)
- [Настройка OIDC](https://docs.streamlit.io/develop/api-reference/user/st.login)
- [Google OAuth](https://developers.google.com/workspace/guides/create-credentials)
- [Хранение секретов](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/secrets-management)
