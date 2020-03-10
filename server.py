
import cgi
from collections import defaultdict
from datetime import datetime
import functools
import logging
import json
import os
import random
import string

from flask import (
    Flask,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    session,
    url_for)
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_socketio import emit, join_room, send, SocketIO
from profanity_filter import ProfanityFilter
import uuid

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

pf = ProfanityFilter(languages=['en'])

app = Flask(__name__)
app.secret_key = 'My super secret key!'
app._static_folder = 'static'
socketio = SocketIO(app)

limiter = Limiter(app, key_func=get_remote_address)

PHOTO_PATH = 'static/data/profile_photos'
photos = list(os.path.join(PHOTO_PATH, f) for f in os.listdir(PHOTO_PATH))

current_slide = '0/0'

PRESENTER_UID = 'presenter'
PRESENTER_USERNAME = 'presenter'
PRESENTER_PHOTO = 'static/data/admin.jpg'

ANNOUNCED_ACHIEVERS = 3


class User:
    def __init__(self, uid, username, profile_photo):
        self.uid = uid
        self.username = username
        self.profile_photo = profile_photo

class Users:
    _users_by_id = {
        PRESENTER_UID: User(
            uid=PRESENTER_UID,
            username=PRESENTER_USERNAME,
            profile_photo=PRESENTER_PHOTO
        )
    }
    _users_by_name = {
        PRESENTER_USERNAME: _users_by_id[PRESENTER_UID]
    }

    @classmethod
    def add(cls, uid, username, profile_photo):
        cls._users_by_id[uid] = cls._users_by_name[username] = User(
            uid=uid,
            username=username,
            profile_photo=profile_photo
        )

    @classmethod
    def get_name(cls, username):
        return cls._users_by_name.get(username)

    @classmethod
    def get_id(cls, uid):
        return cls._users_by_id.get(uid)

    @classmethod
    def get_all(cls):
        return cls._users_by_id.values()

class Achievement:
    def __init__(self, name, description):
        self.name = name
        self.description = description
        self.achievers = set()

achievements = {
    'client-side/username': Achievement('Yoink', 'stole the same name as another user!'),
    'client-side/button': Achievement('So Much Spaghetti Code', 'found out how to view the server source code!'),
    'xss/search': Achievement('Here - Click This', 'created a malicious XSS link!'),
    # 'xss/bio': Achievement('created a malicious page!'),
    'cookies/admin': Achievement('Look At Me; I Am The Admin Now', 'managed to view the admin user\'s page!'),
    # 'csrf/admin': Achievement(''),
}


def trigger_achievement(uid, achievement_id):
    user = Users.get_id(uid)
    achievement = achievements[achievement_id]

    if uid in achievement.achievers:
        return

    achievement.achievers.add(uid)
    logger.info(f'{user.username} got achievement: {achievement_id}')

    socketio.emit('achievement', {
        'username': user.username,
        'name': achievement.name,
        'description': f'You {achievement.description}',
        'rank': len(achievement.achievers),
    }, json=True, room=uid)

    # Send the first N winners to the presenter as well, for recognition.
    if len(achievement.achievers) <= ANNOUNCED_ACHIEVERS:
        socketio.emit('achievement', {
            'username': user.username,
            'name': achievement.name,
            'description': f'They {achievement.description}',
            'rank': len(achievement.achievers),
        }, json=True, room=PRESENTER_UID)

def requires_auth(f):
    @functools.wraps(f)
    def decorator(*pargs, **kwargs):
        if not Users.get_id(session.get('uid')):
            return redirect(url_for('signup'))
        else:
            return f(*pargs, **kwargs)

    return decorator


@socketio.on('connect')
@requires_auth
def test_connect():
    # Add client to client list
    flask_id = session.get('uid')
    socketio_id = request.sid
    logger.info(f'New socketio connection from {flask_id} (id #{socketio_id})')

    join_room(flask_id)


@socketio.on('chat_message')
@requires_auth
def handle_chat_message(message):
    user = Users.get_id(session['uid'])
    logger.info(f'{user.username} says: {message}')

    if len(message) > 200:
        emit('chat_error', 'Message was too long.')
        return

    censored_message = pf.censor(message)
    emit('chat_message', {
        'username': user.username,
        'profile_photo': user.profile_photo,
        'time': datetime.now().strftime('%I:%M%p').lower().strip('0'),
        'content': censored_message,
    }, json=True, broadcast=True)


@socketio.on('slide_change')
def handle_slide_change(slide):
    global current_slide
    if session.get('uid') != PRESENTER_UID:
        emit('error', 'Only the presenter can change the slide this way.')

    current_slide = slide
    emit('slide_change', slide, broadcast=True, include_self=False)


@app.route('/api/users')
def get_all_users():
    return json.dumps([user.username for user in Users.get_all()])


@app.route('/api/user/<username>')
def get_user(username):
    user = Users.get_name(username)
    if not user:
        return jsonify(None)
    else:
        return jsonify({
            'username': user.username,
            'profile_photo': user.profile_photo
        })


# @app.after_request
def apply_csp(response):
    response.headers['Content-Security-Policy-Report-Only'] = (
        'default-src \'none\';'
        'style-src cdn.example.com;'
        'report-uri /csp'
    )
    return response


@app.route('/csp', methods=['POST'])
def handle_csp():
    print('CSP report:')
    print(request.data)
    return 'ok'


@app.route('/')
@requires_auth
def index():
    user = Users.get_id(session['uid'])
    return render_template('index.html', username=user.username, profile_photo=user.profile_photo, slide=current_slide)


@app.route('/presentation')
@requires_auth
def presentation():
    return render_template('presentation.html')


@app.route('/exercises')
@requires_auth
def exercises():
    return render_template('exercises.html')


@app.route('/signup', methods=['GET'])
def signup():
    if 'uid' in session and Users.get_id(session['uid']):
        return redirect(url_for('index'))

    error = session.get('error')
    if error:
        del session['error']
    return render_template('signup.html', photos=enumerate(photos), error=error)


@app.route('/signup', methods=['POST'])
def signup_post():
    username = request.form['username'].lower()
    logger.info(f'Attempted signup for user "{username}"')

    try:
        if pf.is_profane(username):
            logger.warning(f'Potentially profane username: {username}')
            session['error'] = 'Username was detected to be profane'
            return redirect(url_for('signup'))

        uid = uuid.uuid4()

        # TODO: Make sure duplicate name conflicts are handled correctly.
        if Users.get_name(username):
            trigger_achievement(uid, 'client-side/username')

        Users.add(
            uid=uid,
            username=username,
            profile_photo=photos[int(request.form['photo'])]
        )
        session['uid'] = uid

    except Exception as e:
        logger.error(e)
        session['error'] = 'Invalid request'
        return redirect(url_for('signup'))

    logger.info(f'User successfully signed up: {username}')

    resp = make_response(redirect(url_for('index')))
    resp.set_cookie('has_admin_access', 'false')

    return redirect(url_for('index'))


@app.route('/logout', methods=['POST'])
def logout():
    del session['uid']

    return redirect(url_for('signup'))


presenter_exists = False
@app.route('/present', methods=['GET'])
def present():
    global presenter_exists
    if not presenter_exists:
        session['uid'] = PRESENTER_UID
        presenter_exists = True

    if session.get('uid') != PRESENTER_UID:
        return redirect(url_for('index'))

    return render_template('presenter.html')



if __name__ == "__main__":
    # server = pywsgi.WSGIServer(('0.0.0.0', 5000), app, handler_class=WebSocketHandler)

    # server.serve_forever()
    socketio.run(app, debug=True, host='0.0.0.0', port=5000)
    # app.run(debug=True, host='0.0.0.0', port=5000)