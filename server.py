#!/usr/bin/env python3
import http.server
import json
import os
import threading
import time
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timedelta

AIRGRADIENT_HOSTS_STR = os.environ.get('AIRGRADIENT_HOSTS', os.environ.get('AIRGRADIENT_HOST', '192.168.1.100'))
AIRGRADIENT_HOSTS = [h.strip() for h in AIRGRADIENT_HOSTS_STR.split(',') if h.strip()]
PORT = int(os.environ.get('PORT', '8080'))
IQAIR_API_KEY = os.environ.get('IQAIR_API_KEY', '')
IQAIR_CACHE = {'data': None, 'ts': 0}
IQAIR_CACHE_SECONDS = 300
LOCATION_ID = int(os.environ.get('LOCATION_ID', '0'))

sensor_registry = {}

def detect_sensors():
    for host in AIRGRADIENT_HOSTS:
        try:
            url = f'http://{host}/measures/current'
            with urllib.request.urlopen(url, timeout=5) as resp:
                data = json.loads(resp.read())
                serial = data.get('serialno', '')
                model = data.get('model', '')
                if serial:
                    sid = f'airgradient:{serial}'
                    sensor_registry[sid] = host
                    print(f"Detected sensor {sid} (model {model}) at {host}", flush=True)
        except Exception as e:
            print(f"Failed to detect sensor at {host}: {e}", flush=True)

def get_host_for_sensor(sensor_id):
    return sensor_registry.get(sensor_id, AIRGRADIENT_HOSTS[0] if AIRGRADIENT_HOSTS else None)

SENSOR_ID = ''

DB_HOST = os.environ.get('DB_HOST', 'host.docker.internal')
DB_PORT = int(os.environ.get('DB_PORT', '3306'))
DB_USER = os.environ.get('DB_USER', 'root')
DB_PASS = os.environ.get('DB_PASS', '')
DB_NAME = os.environ.get('DB_NAME', 'airgradient')

COLLECT_INTERVAL = int(os.environ.get('COLLECT_INTERVAL', '60'))
BUCKET_SECONDS = 300  # 5 minutes

db_available = False
try:
    import mysql.connector
    db_available = True
except ImportError:
    print("mysql-connector-python not installed, running without DB")

def get_db():
    if not db_available:
        return None
    try:
        conn = mysql.connector.connect(
            host=DB_HOST, port=DB_PORT, user=DB_USER, password=DB_PASS,
            database=DB_NAME, connect_timeout=5
        )
        return conn
    except Exception as e:
        print(f"DB connection failed: {e}")
        return None

def store_reading(data, sensor_id=None):
    if not sensor_id:
        serial = data.get('serialno', '')
        sensor_id = f'airgradient:{serial}' if serial else 'unknown'
    conn = get_db()
    if not conn:
        return
    try:
        now = datetime.utcnow()
        bucket = now.replace(second=0, microsecond=0)
        bucket = bucket.replace(minute=(bucket.minute // 5) * 5)

        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO measures (
                location_id, location_name, location_type, sensor_id, place_open,
                recorded_at, recorded_at_utc, aggregated_records,
                pm25_raw, pm25_corrected, pm03_count,
                co2_raw, co2_corrected,
                temp_raw, temp_corrected, heat_index_c,
                humidity_raw, humidity_corrected,
                tvoc_ppb, tvoc_index, nox_index, pm1, pm10
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE
                pm25_raw = VALUES(pm25_raw),
                pm25_corrected = VALUES(pm25_corrected),
                pm03_count = VALUES(pm03_count),
                co2_raw = VALUES(co2_raw),
                co2_corrected = VALUES(co2_corrected),
                temp_raw = VALUES(temp_raw),
                temp_corrected = VALUES(temp_corrected),
                heat_index_c = VALUES(heat_index_c),
                humidity_raw = VALUES(humidity_raw),
                humidity_corrected = VALUES(humidity_corrected),
                tvoc_ppb = VALUES(tvoc_ppb),
                tvoc_index = VALUES(tvoc_index),
                nox_index = VALUES(nox_index),
                pm1 = VALUES(pm1),
                pm10 = VALUES(pm10),
                aggregated_records = aggregated_records + 1
        """, (
            LOCATION_ID, 'Room', 'Indoor', sensor_id, True,
            bucket, bucket, 1,
            data.get('pm02'), data.get('pm02Compensated'), data.get('pm003Count'),
            data.get('rco2'), data.get('rco2'),
            data.get('atmp'), data.get('atmpCompensated'), None,
            data.get('rhum'), data.get('rhumCompensated'),
            data.get('tvocRaw'), data.get('tvocIndex'), data.get('noxIndex'),
            data.get('pm01'), data.get('pm10'),
        ))
        conn.commit()
        cursor.close()
    except Exception as e:
        print(f"DB store error: {e}")
    finally:
        conn.close()

def query_history(hours=8, sensor=None):
    if not sensor:
        sensor = next(iter(sensor_registry), '')
    conn = get_db()
    if not conn:
        return None
    try:
        cursor = conn.cursor(dictionary=True)
        select_cols = """recorded_at_utc, pm25_raw, pm25_corrected, pm03_count,
                       co2_raw, co2_corrected, temp_raw, temp_corrected, heat_index_c,
                       humidity_raw, humidity_corrected, tvoc_ppb, tvoc_index, nox_index,
                       pm1, pm10, aggregated_records"""
        if hours == 0:
            if SENSOR_ID:
                cursor.execute(f"SELECT {select_cols} FROM measures WHERE sensor_id = %s ORDER BY recorded_at_utc ASC", (SENSOR_ID,))
            else:
                cursor.execute(f"SELECT {select_cols} FROM measures ORDER BY recorded_at_utc ASC")
        else:
            since = datetime.utcnow() - timedelta(hours=hours)
            if SENSOR_ID:
                cursor.execute(f"SELECT {select_cols} FROM measures WHERE sensor_id = %s AND recorded_at_utc >= %s ORDER BY recorded_at_utc ASC", (SENSOR_ID, since))
            else:
                cursor.execute(f"SELECT {select_cols} FROM measures WHERE recorded_at_utc >= %s ORDER BY recorded_at_utc ASC", (since,))
        rows = cursor.fetchall()
        cursor.close()
        conn.close()
        for r in rows:
            if r.get('recorded_at_utc'):
                r['recorded_at_utc'] = r['recorded_at_utc'].isoformat() + 'Z'
        return rows
    except Exception as e:
        print(f"DB query error: {e}")
        if conn:
            conn.close()
        return None

def collector_loop(host, sensor_id):
    while True:
        try:
            url = f'http://{host}/measures/current'
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
                store_reading(data, sensor_id)
        except Exception as e:
            print(f"Collector error ({host}): {e}")
        time.sleep(COLLECT_INTERVAL)


class ProxyHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/swagger' or self.path == '/api/docs':
            self.send_response(302)
            self.send_header('Location', 'https://api.airgradient.com/public/docs/api/v1/swagger.json')
            self.end_headers()
        elif self.path.startswith('/api/outdoor'):
            self._outdoor_request()
        elif self.path.startswith('/api/iqair/'):
            self._iqair_proxy()
        elif self.path.startswith('/api/history'):
            self._history_request()
        elif self.path.startswith('/api/sensors'):
            self._sensors_request()
        elif self.path.startswith('/api/export'):
            self._export_request()
        elif self.path.startswith('/api/'):
            self._proxy_request()
        else:
            super().do_GET()

    def _proxy_request(self):
        target_path = self.path[4:]
        params = {}
        if '?' in target_path:
            path_part, qs = target_path.split('?', 1)
            for pair in qs.split('&'):
                if '=' in pair:
                    k, v = pair.split('=', 1)
                    params[k] = v
            target_path = path_part
        sensor = urllib.parse.unquote(params.get('sensor', ''))
        host = get_host_for_sensor(sensor) if sensor else (AIRGRADIENT_HOSTS[0] if AIRGRADIENT_HOSTS else '127.0.0.1')
        url = f'http://{host}{target_path}'
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = resp.read()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Cache-Control', 'no-cache')
                self.end_headers()
                self.wfile.write(data)
        except urllib.error.URLError as e:
            self.send_response(502)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'error': str(e)}).encode())
        except Exception as e:
            self.send_response(500)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'error': str(e)}).encode())

    def _history_request(self):
        params = {}
        if '?' in self.path:
            qs = self.path.split('?', 1)[1]
            for pair in qs.split('&'):
                if '=' in pair:
                    k, v = pair.split('=', 1)
                    params[k] = v
        hours = int(params.get('hours', '8'))
        sensor = urllib.parse.unquote(params.get('sensor', SENSOR_ID))
        rows = query_history(hours, sensor)
        if rows is None:
            self.send_response(503)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'error': 'database unavailable'}).encode())
            return
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Cache-Control', 'no-cache')
        self.end_headers()
        self.wfile.write(json.dumps(rows).encode())

    def _sensors_request(self):
        conn = get_db()
        if not conn:
            self.send_response(503)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'error': 'database unavailable'}).encode())
            return
        try:
            cursor = conn.cursor(dictionary=True)
            cursor.execute("SELECT DISTINCT sensor_id, location_name FROM measures ORDER BY location_name")
            rows = cursor.fetchall()
            cursor.close()
            conn.close()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps(rows).encode())
        except Exception as e:
            if conn: conn.close()
            self.send_response(500)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'error': str(e)}).encode())

    def _outdoor_request(self):
        if not IQAIR_API_KEY:
            self.send_response(503)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'error': 'IQAIR_API_KEY not configured'}).encode())
            return
        params = {}
        if '?' in self.path:
            qs = self.path.split('?', 1)[1]
            for pair in qs.split('&'):
                if '=' in pair:
                    k, v = pair.split('=', 1)
                    params[k] = v
        lat = params.get('lat', '')
        lon = params.get('lon', '')
        if not lat or not lon:
            self.send_response(400)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'error': 'lat and lon required'}).encode())
            return
        cache_key = f"{float(lat):.2f},{float(lon):.2f}"
        now = time.time()
        if IQAIR_CACHE.get('key') == cache_key and IQAIR_CACHE['data'] and (now - IQAIR_CACHE['ts']) < IQAIR_CACHE_SECONDS:
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Cache-Control', f'max-age={IQAIR_CACHE_SECONDS}')
            self.end_headers()
            self.wfile.write(IQAIR_CACHE['data'])
            return
        try:
            url = f'http://api.airvisual.com/v2/nearest_city?lat={lat}&lon={lon}&key={IQAIR_API_KEY}'
            with urllib.request.urlopen(url, timeout=10) as resp:
                data = resp.read()
                IQAIR_CACHE['data'] = data
                IQAIR_CACHE['ts'] = now
                IQAIR_CACHE['key'] = cache_key
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Cache-Control', f'max-age={IQAIR_CACHE_SECONDS}')
                self.end_headers()
                self.wfile.write(data)
        except Exception as e:
            self.send_response(502)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'error': str(e)}).encode())

    def _iqair_proxy(self):
        if not IQAIR_API_KEY:
            self.send_response(503)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'error': 'IQAIR_API_KEY not configured'}).encode())
            return
        sub = self.path.split('/api/iqair/')[1].split('?')[0]
        params = {}
        if '?' in self.path:
            qs = self.path.split('?', 1)[1]
            for pair in qs.split('&'):
                if '=' in pair:
                    k, v = pair.split('=', 1)
                    params[k] = urllib.parse.unquote(v)
        qs_parts = [f'key={IQAIR_API_KEY}']
        for k, v in params.items():
            qs_parts.append(f'{k}={urllib.parse.quote(v)}')
        url = f'http://api.airvisual.com/v2/{sub}?{"&".join(qs_parts)}'
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                data = resp.read()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(data)
        except Exception as e:
            self.send_response(502)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'error': str(e)}).encode())

    def _export_request(self):
        params = {}
        if '?' in self.path:
            qs = self.path.split('?', 1)[1]
            for pair in qs.split('&'):
                if '=' in pair:
                    k, v = pair.split('=', 1)
                    params[k] = v
        sensor = urllib.parse.unquote(params.get('sensor', SENSOR_ID))
        conn = get_db()
        if not conn:
            self.send_response(503)
            self.end_headers()
            return
        try:
            cursor = conn.cursor()
            if sensor:
                cursor.execute("""SELECT recorded_at_utc, pm25_raw, pm25_corrected, pm03_count,
                    co2_raw, co2_corrected, temp_raw, temp_corrected, heat_index_c,
                    humidity_raw, humidity_corrected, tvoc_ppb, tvoc_index, nox_index,
                    pm1, pm10 FROM measures WHERE sensor_id = %s ORDER BY recorded_at_utc ASC""", (sensor,))
            else:
                cursor.execute("""SELECT recorded_at_utc, pm25_raw, pm25_corrected, pm03_count,
                    co2_raw, co2_corrected, temp_raw, temp_corrected, heat_index_c,
                    humidity_raw, humidity_corrected, tvoc_ppb, tvoc_index, nox_index,
                    pm1, pm10 FROM measures ORDER BY recorded_at_utc ASC""")
            rows = cursor.fetchall()
            cursor.close()
            conn.close()

            import io, csv as csvmod
            output = io.StringIO()
            writer = csvmod.writer(output)
            writer.writerow(['UTC Time','PM2.5 raw','PM2.5 corrected','PM0.3 count',
                'CO2 raw','CO2 corrected','Temp raw','Temp corrected','Heat Index C',
                'Humidity raw','Humidity corrected','TVOC ppb','TVOC index','NOx index',
                'PM1','PM10'])
            for row in rows:
                writer.writerow([
                    row[0].isoformat() + 'Z' if row[0] else '',
                    *['' if v is None else v for v in row[1:]]
                ])
            csv_data = output.getvalue().encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/csv')
            self.send_header('Content-Disposition', 'attachment; filename="airgradient_export.csv"')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(csv_data)
        except Exception as e:
            if conn: conn.close()
            self.send_response(500)
            self.end_headers()
            self.wfile.write(str(e).encode())

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def log_message(self, format, *args):
        if '/api/' in (args[0] if args else ''):
            super().log_message(format, *args)


if __name__ == '__main__':
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    detect_sensors()
    print(f"Registered {len(sensor_registry)} sensor(s): {dict(sensor_registry)}", flush=True)

    if db_available:
        import glob
        csv_dir = os.environ.get('CSV_DIR', '/data')
        csv_files = sorted(glob.glob(os.path.join(csv_dir, '*.csv')))
        print(f"CSV dir: {csv_dir}, found {len(csv_files)} file(s)", flush=True)
        if csv_files:
            from import_csv import import_csv
            for csv_file in csv_files:
                print(f"Auto-ingesting: {csv_file}", flush=True)
                try:
                    import_csv(csv_file)
                    print(f"  Done: {csv_file}", flush=True)
                except Exception as e:
                    print(f"  Failed: {csv_file}: {e}", flush=True)

        for sid, host in sensor_registry.items():
            t = threading.Thread(target=collector_loop, args=(host, sid), daemon=True)
            t.start()
            print(f"Collector started for {sid} @ {host} (every {COLLECT_INTERVAL}s)", flush=True)

    server = http.server.HTTPServer(('0.0.0.0', PORT), ProxyHandler)
    print(f'Serving on http://localhost:{PORT}')
    print(f'Polling {len(AIRGRADIENT_HOSTS)} host(s): {", ".join(AIRGRADIENT_HOSTS)}')
    server.serve_forever()
