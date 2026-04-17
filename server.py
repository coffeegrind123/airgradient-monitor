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
OUTDOOR_CITY = os.environ.get('OUTDOOR_CITY', '')
OUTDOOR_STATE = os.environ.get('OUTDOOR_STATE', '')
OUTDOOR_COUNTRY = os.environ.get('OUTDOOR_COUNTRY', '')
PIRATE_WEATHER_KEY = os.environ.get('PIRATE_WEATHER_KEY', '')
WEATHER_LAT = os.environ.get('WEATHER_LAT', '')
WEATHER_LON = os.environ.get('WEATHER_LON', '')
PIRATE_CACHE = {'data': None, 'ts': 0}

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

def ensure_schema():
    for attempt in range(10):
        conn = get_db()
        if conn:
            break
        print(f"Waiting for database (attempt {attempt + 1}/10)...", flush=True)
        time.sleep(3)
    if not conn:
        print("Could not connect to database for schema init", flush=True)
        return
    try:
        cursor = conn.cursor()
        schema_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'schema.sql')
        if os.path.exists(schema_path):
            with open(schema_path, 'r') as f:
                statements = f.read().split(';')
            for stmt in statements:
                stmt = stmt.strip()
                if stmt:
                    try:
                        cursor.execute(stmt)
                    except Exception:
                        pass
            conn.commit()
            print("Schema ensured via application startup", flush=True)
        cursor.close()
    except Exception as e:
        print(f"Schema init error: {e}", flush=True)
    finally:
        conn.close()

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


def aqiToPM25(aqi):
    bp = [(0,50,0,12.0),(51,100,12.1,35.4),(101,150,35.5,55.4),(151,200,55.5,150.4),(201,300,150.5,250.4),(301,400,250.5,350.4),(401,500,350.5,500.4)]
    for iLow, iHigh, cLow, cHigh in bp:
        if aqi >= iLow and aqi <= iHigh:
            return round(((cHigh - cLow) / (iHigh - iLow)) * (aqi - iLow) + cLow, 1)
    return 0

def store_outdoor_reading(data):
    conn = get_db()
    if not conn:
        return
    try:
        d = data
        p = d['current']['pollution']
        w = d['current']['weather']
        ts = datetime.strptime(p['ts'].replace('.000Z',''), '%Y-%m-%dT%H:%M:%S')
        pm25_est = aqiToPM25(p['aqius']) if p.get('mainus') == 'p2' else None

        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO outdoor_measures (
                city, state, country, recorded_at_utc,
                aqi_us, aqi_cn, main_pollutant, pm25_estimated,
                temp_c, humidity, pressure, wind_speed, wind_direction, weather_icon
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE
                aqi_us = VALUES(aqi_us), aqi_cn = VALUES(aqi_cn),
                main_pollutant = VALUES(main_pollutant), pm25_estimated = VALUES(pm25_estimated),
                temp_c = VALUES(temp_c), humidity = VALUES(humidity),
                pressure = VALUES(pressure), wind_speed = VALUES(wind_speed),
                wind_direction = VALUES(wind_direction), weather_icon = VALUES(weather_icon)
        """, (
            d['city'], d.get('state',''), d.get('country',''), ts,
            p['aqius'], p.get('aqicn'), p.get('mainus'),
            pm25_est, w.get('tp'), w.get('hu'), w.get('pr'),
            w.get('ws'), w.get('wd'), w.get('ic')
        ))
        conn.commit()
        cursor.close()
    except Exception as e:
        print(f"Outdoor DB store error: {e}", flush=True)
    finally:
        conn.close()

def query_outdoor_history(city, country, hours=168):
    conn = get_db()
    if not conn:
        return None
    try:
        cursor = conn.cursor(dictionary=True)
        if hours == 0:
            cursor.execute("""SELECT recorded_at_utc, aqi_us, main_pollutant, pm25_estimated,
                temp_c, humidity, pressure, wind_speed FROM outdoor_measures
                WHERE city = %s AND country = %s ORDER BY recorded_at_utc ASC""", (city, country))
        else:
            since = datetime.utcnow() - timedelta(hours=hours)
            cursor.execute("""SELECT recorded_at_utc, aqi_us, main_pollutant, pm25_estimated,
                temp_c, humidity, pressure, wind_speed FROM outdoor_measures
                WHERE city = %s AND country = %s AND recorded_at_utc >= %s
                ORDER BY recorded_at_utc ASC""", (city, country, since))
        rows = cursor.fetchall()
        cursor.close()
        conn.close()
        for r in rows:
            if r.get('recorded_at_utc'):
                r['recorded_at_utc'] = r['recorded_at_utc'].isoformat() + 'Z'
        return rows
    except Exception as e:
        print(f"Outdoor DB query error: {e}", flush=True)
        if conn: conn.close()
        return None

outdoor_collector_cities = []

def outdoor_collector_loop():
    while True:
        for city_info in outdoor_collector_cities:
            try:
                city, state, country = city_info
                url = f'http://api.airvisual.com/v2/city?city={urllib.parse.quote(city)}&state={urllib.parse.quote(state)}&country={urllib.parse.quote(country)}&key={IQAIR_API_KEY}'
                with urllib.request.urlopen(url, timeout=10) as resp:
                    data = json.loads(resp.read())
                    if data.get('status') == 'success':
                        store_outdoor_reading(data['data'])
                        IQAIR_CACHE['data'] = json.dumps(data).encode()
                        IQAIR_CACHE['ts'] = time.time()
                        IQAIR_CACHE['key'] = f"{city},{country}"
            except Exception as e:
                print(f"Outdoor collector error ({city_info}): {e}", flush=True)
        time.sleep(IQAIR_CACHE_SECONDS)


def store_weather(data):
    conn = get_db()
    if not conn:
        return
    try:
        c = data.get('currently', {})
        ts = datetime.utcfromtimestamp(c['time'])
        lat = float(data['latitude'])
        lon = float(data['longitude'])
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO weather (lat, lon, recorded_at_utc, summary, icon,
                temperature, apparent_temperature, dew_point, humidity, pressure,
                wind_speed, wind_gust, wind_bearing, cloud_cover, uv_index,
                visibility, ozone, precip_intensity, precip_probability, precip_type
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE
                summary=VALUES(summary), icon=VALUES(icon), temperature=VALUES(temperature),
                apparent_temperature=VALUES(apparent_temperature), dew_point=VALUES(dew_point),
                humidity=VALUES(humidity), pressure=VALUES(pressure), wind_speed=VALUES(wind_speed),
                wind_gust=VALUES(wind_gust), wind_bearing=VALUES(wind_bearing),
                cloud_cover=VALUES(cloud_cover), uv_index=VALUES(uv_index),
                visibility=VALUES(visibility), ozone=VALUES(ozone),
                precip_intensity=VALUES(precip_intensity), precip_probability=VALUES(precip_probability),
                precip_type=VALUES(precip_type)
        """, (lat, lon, ts, c.get('summary'), c.get('icon'),
              c.get('temperature'), c.get('apparentTemperature'), c.get('dewPoint'),
              c.get('humidity'), c.get('pressure'), c.get('windSpeed'), c.get('windGust'),
              c.get('windBearing'), c.get('cloudCover'), c.get('uvIndex'),
              c.get('visibility'), c.get('ozone'), c.get('precipIntensity'),
              c.get('precipProbability'), c.get('precipType')))
        conn.commit()
        cursor.close()
    except Exception as e:
        print(f"Weather DB store error: {e}", flush=True)
    finally:
        conn.close()

def query_weather_history(lat, lon, hours=168):
    conn = get_db()
    if not conn:
        return None
    try:
        cursor = conn.cursor(dictionary=True)
        if hours == 0:
            cursor.execute("""SELECT recorded_at_utc, summary, temperature, apparent_temperature, dew_point,
                humidity, pressure, wind_speed, wind_gust, wind_bearing, cloud_cover,
                uv_index, visibility, ozone, precip_intensity, precip_probability, precip_type
                FROM weather WHERE ABS(lat-%s)<0.1 AND ABS(lon-%s)<0.1 ORDER BY recorded_at_utc ASC""", (lat, lon))
        else:
            since = datetime.utcnow() - timedelta(hours=hours)
            cursor.execute("""SELECT recorded_at_utc, summary, temperature, apparent_temperature, dew_point,
                humidity, pressure, wind_speed, wind_gust, wind_bearing, cloud_cover,
                uv_index, visibility, ozone, precip_intensity, precip_probability, precip_type
                FROM weather WHERE ABS(lat-%s)<0.1 AND ABS(lon-%s)<0.1 AND recorded_at_utc >= %s
                ORDER BY recorded_at_utc ASC""", (lat, lon, since))
        rows = cursor.fetchall()
        cursor.close()
        conn.close()
        for r in rows:
            if r.get('recorded_at_utc'):
                r['recorded_at_utc'] = r['recorded_at_utc'].isoformat() + 'Z'
        return rows
    except Exception as e:
        print(f"Weather DB query error: {e}", flush=True)
        if conn: conn.close()
        return None

def weather_collector_loop():
    while True:
        if PIRATE_WEATHER_KEY and WEATHER_LAT and WEATHER_LON:
            try:
                url = f'https://api.pirateweather.net/forecast/{PIRATE_WEATHER_KEY}/{WEATHER_LAT},{WEATHER_LON}?units=si'
                with urllib.request.urlopen(url, timeout=15) as resp:
                    data = json.loads(resp.read())
                    PIRATE_CACHE['data'] = data
                    PIRATE_CACHE['ts'] = time.time()
                    if db_available:
                        store_weather(data)
            except Exception as e:
                print(f"Weather collector error: {e}", flush=True)
        time.sleep(IQAIR_CACHE_SECONDS)


class ProxyHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/swagger' or self.path == '/api/docs':
            self.send_response(302)
            self.send_header('Location', 'https://api.airgradient.com/public/docs/api/v1/swagger.json')
            self.end_headers()
        elif self.path.startswith('/api/weather/history'):
            self._weather_history()
        elif self.path.startswith('/api/weather'):
            self._weather_current()
        elif self.path.startswith('/api/outdoor/config'):
            self._outdoor_config()
        elif self.path.startswith('/api/outdoor/history'):
            self._outdoor_history_request()
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

    def _weather_current(self):
        if PIRATE_CACHE.get('data') and (time.time() - PIRATE_CACHE['ts']) < IQAIR_CACHE_SECONDS:
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps(PIRATE_CACHE['data']).encode())
            return
        if not PIRATE_WEATHER_KEY or not WEATHER_LAT or not WEATHER_LON:
            self.send_response(503)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'error': 'PIRATE_WEATHER_KEY/WEATHER_LAT/WEATHER_LON not configured'}).encode())
            return
        try:
            url = f'https://api.pirateweather.net/forecast/{PIRATE_WEATHER_KEY}/{WEATHER_LAT},{WEATHER_LON}?units=si'
            with urllib.request.urlopen(url, timeout=15) as resp:
                data = json.loads(resp.read())
                PIRATE_CACHE['data'] = data
                PIRATE_CACHE['ts'] = time.time()
                if db_available: store_weather(data)
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(json.dumps(data).encode())
        except Exception as e:
            self.send_response(502)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'error': str(e)}).encode())

    def _weather_history(self):
        params = {}
        if '?' in self.path:
            qs = self.path.split('?', 1)[1]
            for pair in qs.split('&'):
                if '=' in pair:
                    k, v = pair.split('=', 1)
                    params[k] = v
        lat = float(params.get('lat', WEATHER_LAT or '0'))
        lon = float(params.get('lon', WEATHER_LON or '0'))
        hours = int(params.get('hours', '168'))
        rows = query_weather_history(lat, lon, hours)
        self.send_response(200 if rows is not None else 503)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(json.dumps(rows if rows else []).encode())

    def _outdoor_config(self):
        config = {
            'configured': bool(OUTDOOR_CITY and OUTDOOR_COUNTRY),
            'city': OUTDOOR_CITY,
            'state': OUTDOOR_STATE,
            'country': OUTDOOR_COUNTRY
        }
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(json.dumps(config).encode())

    def _outdoor_history_request(self):
        params = {}
        if '?' in self.path:
            qs = self.path.split('?', 1)[1]
            for pair in qs.split('&'):
                if '=' in pair:
                    k, v = pair.split('=', 1)
                    params[k] = urllib.parse.unquote(v)
        city = params.get('city', '')
        country = params.get('country', '')
        hours = int(params.get('hours', '168'))
        if not city or not country:
            self.send_response(400)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'error': 'city and country required'}).encode())
            return
        rows = query_outdoor_history(city, country, hours)
        if rows is None:
            self.send_response(503)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'error': 'database unavailable'}).encode())
            return
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(json.dumps(rows).encode())

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
                parsed = json.loads(data)
                if parsed.get('status') == 'success' and db_available:
                    store_outdoor_reading(parsed['data'])
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
                if sub == 'city' and db_available:
                    try:
                        parsed = json.loads(data)
                        if parsed.get('status') == 'success':
                            store_outdoor_reading(parsed['data'])
                            c = parsed['data']
                            city_key = (c['city'], c.get('state',''), c.get('country',''))
                            if city_key not in outdoor_collector_cities:
                                outdoor_collector_cities.append(city_key)
                                print(f"Added outdoor collector for {city_key}", flush=True)
                    except Exception: pass
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
        ensure_schema()
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

    if db_available and IQAIR_API_KEY:
        if OUTDOOR_CITY and OUTDOOR_COUNTRY:
            outdoor_collector_cities.append((OUTDOOR_CITY, OUTDOOR_STATE, OUTDOOR_COUNTRY))
            print(f"Outdoor location configured: {OUTDOOR_CITY}, {OUTDOOR_STATE}, {OUTDOOR_COUNTRY}", flush=True)
            if not WEATHER_LAT and OUTDOOR_CITY:
                try:
                    url = f'http://api.airvisual.com/v2/city?city={urllib.parse.quote(OUTDOOR_CITY)}&state={urllib.parse.quote(OUTDOOR_STATE)}&country={urllib.parse.quote(OUTDOOR_COUNTRY)}&key={IQAIR_API_KEY}'
                    with urllib.request.urlopen(url, timeout=10) as resp:
                        d = json.loads(resp.read())
                        if d.get('status') == 'success':
                            coords = d['data']['location']['coordinates']
                            WEATHER_LAT = str(coords[1])
                            WEATHER_LON = str(coords[0])
                            print(f"Auto-detected weather coords: {WEATHER_LAT},{WEATHER_LON}", flush=True)
                            store_outdoor_reading(d['data'])
                except Exception as e:
                    print(f"Failed to auto-detect coords: {e}", flush=True)
        t_outdoor = threading.Thread(target=outdoor_collector_loop, daemon=True)
        t_outdoor.start()
        print(f"Outdoor collector started (every {IQAIR_CACHE_SECONDS}s)", flush=True)

    if db_available and PIRATE_WEATHER_KEY and (WEATHER_LAT or OUTDOOR_CITY):
        t_weather = threading.Thread(target=weather_collector_loop, daemon=True)
        t_weather.start()
        print(f"Weather collector started: {WEATHER_LAT},{WEATHER_LON} (every {IQAIR_CACHE_SECONDS}s)", flush=True)

    server = http.server.HTTPServer(('0.0.0.0', PORT), ProxyHandler)
    print(f'Serving on http://localhost:{PORT}')
    print(f'Polling {len(AIRGRADIENT_HOSTS)} host(s): {", ".join(AIRGRADIENT_HOSTS)}')
    server.serve_forever()
