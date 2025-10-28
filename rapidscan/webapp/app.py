import os
import re
import uuid
import queue
import signal
import threading
from subprocess import Popen, PIPE
from flask import Flask, Response, jsonify, render_template, request

# Paths
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
RAPIDSCAN_PATH = os.path.join(BASE_DIR, 'rapidscan.py')

app = Flask(__name__, template_folder='templates', static_folder='static')

# Scan registry: scan_id -> { proc, queue, progress, total, done }
scans = {}
lock = threading.Lock()

ansi_escape = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")

def _enqueue_output(proc, q, scan_meta):
    for line in iter(proc.stdout.readline, ''):
        clean = ansi_escape.sub('', line.rstrip('\n'))
        # Progress parsing
        # Example: "Deploying 3/81 | ..."
        m = re.search(r"Deploying\s+(\d+)\/(\d+)", clean)
        if m:
            try:
                cur = int(m.group(1))
                total = int(m.group(2))
                with lock:
                    scan_meta['progress'] = cur
                    scan_meta['total'] = total
            except Exception:
                pass
        q.put(clean)
    proc.stdout.close()
    proc.wait()
    with lock:
        scan_meta['done'] = True

@app.route('/')
def index():
    return render_template('index.html')

@app.post('/start')
def start_scan():
    data = request.get_json(force=True, silent=True) or {}
    target = (data.get('target') or '').strip()
    skip_raw = (data.get('skip') or '').strip()
    if not target:
        return jsonify({ 'error': 'target is required' }), 400

    scan_id = uuid.uuid4().hex

    # Build command: python3 -u rapidscan.py -n [--skip ...] <target>
    cmd = ['python3', '-u', RAPIDSCAN_PATH, '-n']
    # Optional skip list: comma/space separated
    if skip_raw:
        for tok in re.split(r"[,\s]+", skip_raw):
            tok = tok.strip()
            if tok:
                cmd.extend(['--skip', tok])
    cmd.append(target)

    # Environment: avoid clearing server terminal affecting service
    env = os.environ.copy()

    proc = Popen(cmd, stdout=PIPE, stderr=PIPE, text=True, bufsize=1, universal_newlines=True, env=env)
    q = queue.Queue()
    meta = { 'proc': proc, 'queue': q, 'progress': 0, 'total': 0, 'done': False }
    with lock:
        scans[scan_id] = meta

    t = threading.Thread(target=_enqueue_output, args=(proc, q, meta), daemon=True)
    t.start()

    # Also read stderr and forward into same queue
    def _enqueue_err():
        for line in iter(proc.stderr.readline, ''):
            clean = ansi_escape.sub('', line.rstrip('\n'))
            q.put(clean)
        proc.stderr.close()
    threading.Thread(target=_enqueue_err, daemon=True).start()

    return jsonify({ 'scan_id': scan_id })

@app.get('/stream/<scan_id>')
def stream(scan_id):
    with lock:
        meta = scans.get(scan_id)
    if not meta:
        return jsonify({ 'error': 'Invalid scan id' }), 404

    def event_stream():
        q = meta['queue']
        while True:
            try:
                line = q.get(timeout=0.5)
            except queue.Empty:
                pass
            else:
                yield f"data: {line}\n\n"
            with lock:
                done = meta['done']
                prog = meta['progress']
                total = meta['total']
            # Periodic progress events
            yield f"event: progress\ndata: {{\"progress\": {prog}, \"total\": {total}}}\n\n"
            if done and q.empty():
                yield "event: complete\ndata: done\n\n"
                break
    return Response(event_stream(), mimetype='text/event-stream')

@app.post('/stop/<scan_id>')
def stop(scan_id):
    with lock:
        meta = scans.get(scan_id)
    if not meta:
        return jsonify({ 'error': 'Invalid scan id' }), 404
    proc = meta['proc']
    try:
        proc.send_signal(signal.SIGINT)
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass
    return jsonify({ 'status': 'stopping' })

@app.get('/status/<scan_id>')
def status(scan_id):
    with lock:
        meta = scans.get(scan_id)
    if not meta:
        return jsonify({ 'error': 'Invalid scan id' }), 404
    return jsonify({ 'progress': meta['progress'], 'total': meta['total'], 'done': meta['done'] })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', '8000'))
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
