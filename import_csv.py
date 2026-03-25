#!/usr/bin/env python3
import csv
import os
import sys
import mysql.connector
from datetime import datetime

DB_HOST = os.environ.get('DB_HOST', 'host.docker.internal')
DB_PORT = int(os.environ.get('DB_PORT', '3306'))
DB_USER = os.environ.get('DB_USER', 'root')
DB_PASS = os.environ.get('DB_PASS', '')
DB_NAME = os.environ.get('DB_NAME', 'airgradient')

INSERT_SQL = """
INSERT IGNORE INTO measures (
    location_id, location_name, location_type, sensor_id, place_open,
    recorded_at, recorded_at_utc, aggregated_records,
    pm25_raw, pm25_corrected, pm03_count,
    co2_raw, co2_corrected,
    temp_raw, temp_corrected, heat_index_c,
    humidity_raw, humidity_corrected,
    tvoc_ppb, tvoc_index, nox_index,
    pm1, pm10
) VALUES (
    %s, %s, %s, %s, %s,
    %s, %s, %s,
    %s, %s, %s,
    %s, %s,
    %s, %s, %s,
    %s, %s,
    %s, %s, %s,
    %s, %s
)
"""

def parse_float(val):
    if val is None or val == '':
        return None
    try:
        return float(val)
    except ValueError:
        return None

def parse_bool(val):
    if val is None or val == '':
        return None
    return val.lower() == 'true'

def parse_recorded_at(val):
    if not val:
        return None
    return datetime.strptime(val, '%Y-%m-%d %H:%M:%S')

def parse_utc_time(val):
    if not val:
        return None
    return datetime.strptime(val.replace('T', ' ').replace('.000Z', ''), '%Y-%m-%d %H:%M:%S')

def import_csv(filepath):
    conn = mysql.connector.connect(
        host=DB_HOST, port=DB_PORT, user=DB_USER, password=DB_PASS, database=DB_NAME
    )
    cursor = conn.cursor()

    with open(filepath, 'r', encoding='utf-8-sig') as f:
        reader = csv.reader(f)
        header = next(reader)
        print(f"Columns: {len(header)}")
        print(f"Header: {header[:10]}...")

        batch = []
        total = 0
        for row in reader:
            if len(row) < 24:
                continue

            record = (
                int(row[0]) if row[0] else None,       # location_id
                row[1] or None,                          # location_name
                row[3] or None,                          # location_type
                row[4] or None,                          # sensor_id
                parse_bool(row[5]),                      # place_open
                parse_recorded_at(row[6]),                # recorded_at
                parse_utc_time(row[7]),                  # utc_time
                int(row[8]) if row[8] else None,        # aggregated_records
                parse_float(row[9]),                     # pm25_raw
                parse_float(row[10]),                    # pm25_corrected
                parse_float(row[11]),                    # pm03_count
                parse_float(row[12]),                    # co2_raw
                parse_float(row[13]),                    # co2_corrected
                parse_float(row[14]),                    # temp_raw
                parse_float(row[15]),                    # temp_corrected
                parse_float(row[16]),                    # heat_index_c
                parse_float(row[17]),                    # humidity_raw
                parse_float(row[18]),                    # humidity_corrected
                parse_float(row[19]),                    # tvoc_ppb
                parse_float(row[20]),                    # tvoc_index
                parse_float(row[21]),                    # nox_index
                parse_float(row[22]),                    # pm1
                parse_float(row[23]),                    # pm10
            )
            batch.append(record)
            total += 1

            if len(batch) >= 1000:
                cursor.executemany(INSERT_SQL, batch)
                conn.commit()
                print(f"  Imported {total} rows...")
                batch = []

        if batch:
            cursor.executemany(INSERT_SQL, batch)
            conn.commit()

    print(f"Done. Total rows imported: {total}")
    cursor.close()
    conn.close()

if __name__ == '__main__':
    filepath = sys.argv[1] if len(sys.argv) > 1 else '/app/export.csv'
    print(f"Importing {filepath} into {DB_NAME}@{DB_HOST}:{DB_PORT}")
    import_csv(filepath)
