import asyncio
from quart import Quart, jsonify, request, websocket, render_template
import glob
import os
from types import ModuleType
from datetime import datetime
from collections import deque
import psutil
import json
import re
import sys
import ssl
import copy
from queue import Queue

from quart_auth import basic_auth_required

from quart_cors import cors


if len(sys.argv) == 1:
    print("Usage: python3 server.py <path_to_project>")
    print()
    print("your project should have a hgaas.json file in the root directory, modeled after config.json.template")
    sys.exit(1)

if not os.path.isdir(sys.argv[1]):
    print(f"path give ({sys.argv[1]}) is not a valid directory")
    sys.exit(1)

project_dir = os.path.realpath(sys.argv[1])

if not os.path.isfile(project_dir + '/hgaas.json'):
    print(f"config file does not exist: {project_dir}/hgaas.json")
    sys.exit(1)



config = json.load(open(project_dir + '/hgaas.json'))
config['project_dir'] = project_dir

ignore_regs = []
print(project_dir + '/.hgaasignore')
if os.path.isfile(project_dir + '/.hgaasignore'):
    ignore_regs = [ project_dir + "/" + t.strip() for t in open(project_dir + '/.hgaasignore').readlines() ]

print(ignore_regs)

app = Quart(__name__, static_url_path='/', static_folder='public', template_folder="public")
app = cors(app, allow_origin="*")


app.config['QUART_AUTH_BASIC_USERNAME'] = config['auth_user']
app.config['QUART_AUTH_BASIC_PASSWORD'] = config['auth_password']


blocklist_regs = [ 
    r"\.hgaasignore$", r".*\.crt$", r".*\.csr$", r".*\.key$", r".*DS_STORE$", r".*\.swp$", r".*\.swo$", 
    r".*\.pyc", r"__pycache__", r"node_modules"
]


log = deque(maxlen=100)
pid = None
pids = []
running = True


async def run_proc():
    global config, pid, log, running, pids

    while running:
        a = await asyncio.create_subprocess_shell("exec " + config['cmd'] + " 2>&1", stdout=asyncio.subprocess.PIPE, cwd=config['project_dir'])
        pid = a.pid
        pids.append(pid)

        # read lines iteratively until the process exits
        while True:
            await asyncio.sleep(0.01)
            log.append((await a.stdout.readline()).decode('ascii').rstrip())
            if a.returncode is not None:
                break

        # then flush the buffer
        line = None
        while line != b'':
            line = await a.stdout.readline()
            log.append(line.decode('ascii').rstrip())

        log.append("==========================================")
        await asyncio.sleep(0.1)


async def send_logs():
    global config, pid, log, running
    while running:
        await asyncio.sleep(1.0)
        await broadcast('logs', '', "\n".join(log))


def kill_proc():
    global pids
    _pids = copy.copy(pids)
    pids.clear()
    for pid in _pids:
      print(f"killing main pid: {pid}")
      # gotta kill the whole process tree manually
      proc = psutil.Process(pid)
      mypids = [pid]

      for p in proc.children(recursive=True):
          mypids.append(p.pid)
      for pid in mypids:
          print(f"killing {pid}")
          r = os.system(f"kill -9 {pid}")
          print(f"killing {pid}: {r}")



async def commit_changes():
    pass




file_list = []
file_locks = {}
file_caches = {}

async def track_files():
    global running, file_list

    while running:
        filenames = glob.glob(config['project_dir'] + '/*')
        filenames += glob.glob(config['project_dir'] + '/**/*')
        showfiles = []
        for t in filenames:
            ignore = False
            if os.path.isdir(t):
                continue
            for reg in ignore_regs:
                if re.match(reg, t):
                    ignore = True
                    break
            for reg in blocklist_regs:
                if re.match(reg, t):
                    ignore = True
                    break
            if not ignore:
                showfiles.append(t.replace(project_dir, "."))
        file_list.clear()
        file_list += showfiles
        locked_files = {}
        for fname in file_locks:
            if file_is_locked(fname):
                locked_files[fname] = file_locks[fname]['user_id']
        await broadcast('files', '', { "files": file_list, "locks": locked_files })
        await asyncio.sleep(1)




last_restart_time = str(datetime.now())
@app.route('/last_restart')
def last_restart():
    return last_restart_time



connected_websockets = {}
lock_timeout = 10


@app.websocket('/ws')
async def ws():
    global connected_websockets
    q = asyncio.Queue()
    try:
        user_id = await websocket.receive()
        user_id = str(user_id).replace('"','')
        print(user_id)
        connected_websockets[user_id] = q
        print("userid:",user_id)
        while True:
            d = await q.get()
            await websocket.send(json.dumps(d))
    except asyncio.CancelledError:
        print("removing ws")
        del connected_websockets[user_id]
        raise  # this is a websocket disconect


async def broadcast(msg, user_id, data):
    global connected_websockets
    for uid in connected_websockets:
        if uid == user_id: continue
        if msg != "logs" and msg != "files": print(f"{msg} _{user_id} _{uid}")
        await connected_websockets[uid].put({ "msg": msg, "data": data })


@app.route('/update', methods=['POST'])
async def update():
    await request.get_data()
    j = await request.get_json()
    locked = lock_file(j['fname'], request.headers['X-User-Id'])
    if not locked:
        return ""
    await broadcast("update", request.headers['X-User-Id'], { 
        "fname": j['fname'],
        "code": j['code'],
    })
    return ""




@app.route('/')
@basic_auth_required()
async def index():
    return await render_template('index.html')


@app.route('/read')
@basic_auth_required()
async def read():
    ffname = request.args.get('fname')
    fname = project_dir + '/' + ffname
    if ".." in fname: return jsonify({"error": "no."})
    if ffname not in file_caches:
        with open(fname) as f:
            content = f.read()
            file_caches[fname] = content
    return jsonify({"fname": fname, "content": file_caches[fname]})


@app.post('/save')
@basic_auth_required()
async def save():
    global log
    fname = project_dir + '/' + request.args.get('fname')
    if ".." in fname: return jsonify({"error": "no."})
    j = await request.get_json()
    with open(fname, 'w') as f:
        f.write(j['content'])
    kill_proc()
    await commit_changes()
    return jsonify({"fname": fname})


@app.route('/new')
@basic_auth_required()
async def new():
    fname = project_dir + '/' + request.args.get('fname')
    if ".." in fname: return jsonify({"error": "no."})
    if os.path.isfile(fname):
        return jsonify({})
    dirs = re.match(r'^.*/', fname).group()
    os.makedirs(dirs, exist_ok=True)
    with open(fname, 'w') as f:
        f.write("\n\n\ndef register(bot):\n    pass\n\n\n")
    kill_proc()
    await commit_changes()
    return jsonify({})


@app.route('/rm')
@basic_auth_required()
async def rm():
    fname = project_dir + '/' + request.args.get('fname')
    if ".." in fname: return jsonify({"error": "no."})
    os.remove(fname)
    kill_proc()
    await commit_changes()
    return jsonify({})



@app.route('/restart')
@basic_auth_required()
async def restart():
    kill_proc()



def file_is_locked(fname):
    if fname not in file_locks: 
        return False
    if (datetime.now() - file_locks[fname]['time']).total_seconds() < lock_timeout: 
        return True
    return False


def lock_file(fname, user_id):
    if file_is_locked(fname) and file_locks[fname]['user_id'] != user_id:
        # if the file already has a valid lock for a different user, dont lock, and return false
        return False
    else:
        file_locks[fname] = {'time': datetime.now(), 'user_id': user_id}
        return True


@app.route('/lock')
@basic_auth_required()
async def lock():
    await request.get_data()
    if not file_is_locked(fname):
        lock_file(request.args['fname'], request.headers['X-User-Id'])
    return jsonify({})




@app.while_serving
async def close_process_after_shutdown():
    global running
    yield
    running = False
    kill_proc()


import atexit
atexit.register(kill_proc)


SSL_PROTOCOLS = (asyncio.sslproto.SSLProtocol,)
try:
    import uvloop.loop
except ImportError:
    pass
else:
    SSL_PROTOCOLS = (*SSL_PROTOCOLS, uvloop.loop.SSLProtocol)

def ignore_aiohttp_ssl_eror(loop):
    """Ignore aiohttp #3535 / cpython #13548 issue with SSL data after close

    There is an issue in Python 3.7 up to 3.7.3 that over-reports a
    ssl.SSLError fatal error (ssl.SSLError: [SSL: KRB5_S_INIT] application data
    after close notify (_ssl.c:2609)) after we are already done with the
    connection. See GitHub issues aio-libs/aiohttp#3535 and
    python/cpython#13548.

    Given a loop, this sets up an exception handler that ignores this specific
    exception, but passes everything else on to the previous exception handler
    this one replaces.

    Checks for fixed Python versions, disabling itself when running on 3.7.4+
    or 3.8.

    """

    orig_handler = loop.get_exception_handler()

    def ignore_ssl_error(loop, context):
        if context.get("message") == "Task exception was never retrieved":
            return
        if context.get("message") in {
            "SSL error in data received",
            "Fatal error on transport",
        }:
            # validate we have the right exception, transport and protocol
            exception = context.get('exception')
            protocol = context.get('protocol')
            if (
                isinstance(exception, ssl.SSLError)
                and exception.reason == 'APPLICATION_DATA_AFTER_CLOSE_NOTIFY'
                and isinstance(protocol, SSL_PROTOCOLS)
            ):
                if loop.get_debug():
                    asyncio.log.logger.debug('Ignoring asyncio SSL KRB5_S_INIT error')
                return
        if orig_handler is not None:
            orig_handler(loop, context)
        else:
            loop.default_exception_handler(context)

    loop.set_exception_handler(ignore_ssl_error)


if __name__ == "__main__":
    port = 8082
    if 'port' in config:
        port = config['port']
    if 'PORT' in os.environ:
        port = os.environ['PORT']

    loop = asyncio.get_event_loop()
    ignore_aiohttp_ssl_eror(loop)

    print(config)
    if 'ssl_crt' in config and 'ssl_key' in config:
    #if False:
        print("using security")
        run_task = app.run_task(host='0.0.0.0', port=port, certfile=config['ssl_crt'], keyfile=config['ssl_key'])
    else:
        run_task = app.run_task(host='0.0.0.0', port=port)

    loop.run_until_complete(asyncio.gather(
        run_task, 
        run_proc(),
        send_logs(),
        track_files(),
    ))

