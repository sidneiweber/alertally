from functools import wraps

from flask import Blueprint, current_app, redirect, render_template, session, url_for
from flask_dance.contrib.google import google

from config import Config

auth_bp = Blueprint('auth', __name__)


def is_email_allowed(email):
    """Confere o e-mail contra ADMIN_EMAILS, ALLOWED_EMAILS e ALLOWED_EMAIL_DOMAINS."""
    email = (email or '').strip().lower()
    if '@' not in email:
        return False
    if email in Config.ADMIN_USERS or email in Config.ALLOWED_EMAILS:
        return True
    return email.rsplit('@', 1)[1] in Config.ALLOWED_EMAIL_DOMAINS


def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if Config.DISABLE_GOOGLE_LOGIN:
            return f(*args, **kwargs)
        user = session.get('user')
        # Confere a cada requisição: tirar alguém da lista vale na hora
        if user and is_email_allowed(user.get('email')):
            return f(*args, **kwargs)
        session.pop('user', None)
        if google.authorized:
            return redirect(url_for('auth.google_authorized'))
        return redirect(url_for('google.login'))
    return decorated_function


def _deny(email, reason):
    current_app.logger.warning(f'Login denied for {email or "unknown"}: {reason}')
    # Também descarta o token do Google, guardado na sessão pelo Flask-Dance
    session.clear()
    return render_template('access_denied.html', email=email), 403


@auth_bp.route("/google_authorized")
def google_authorized():
    if Config.DISABLE_GOOGLE_LOGIN:
        return redirect(url_for('main.index'))
    if not google.authorized:
        return redirect(url_for('google.login'))

    try:
        resp = google.get("/oauth2/v2/userinfo")
        user_info = resp.json() if resp.ok else None
    except Exception as e:
        current_app.logger.error(f'Failed to fetch Google user info: {e}')
        user_info = None
    if not user_info:
        session.clear()
        return redirect(url_for('google.login'))

    email = user_info.get('email')
    if not user_info.get('verified_email'):
        return _deny(email, 'email not verified')
    if not is_email_allowed(email):
        return _deny(email, 'not in the allowed list')

    session['user'] = {
        'name': user_info.get('name') or email,
        'email': email.lower(),
        'picture': user_info.get('picture'),
    }
    current_app.logger.info(f'User {email} logged in')
    return redirect(url_for('main.index'))


@auth_bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("main.index"))
