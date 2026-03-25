CREATE DATABASE IF NOT EXISTS airgradient;
USE airgradient;

CREATE TABLE IF NOT EXISTS measures (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    location_id INT NOT NULL,
    location_name VARCHAR(100),
    location_type VARCHAR(20),
    sensor_id VARCHAR(100),
    place_open BOOLEAN,
    recorded_at DATETIME NOT NULL,
    recorded_at_utc DATETIME NOT NULL,
    aggregated_records INT,
    pm25_raw FLOAT,
    pm25_corrected FLOAT,
    pm03_count FLOAT,
    co2_raw FLOAT,
    co2_corrected FLOAT,
    temp_raw FLOAT,
    temp_corrected FLOAT,
    heat_index_c FLOAT,
    humidity_raw FLOAT,
    humidity_corrected FLOAT,
    tvoc_ppb FLOAT,
    tvoc_index FLOAT,
    nox_index FLOAT,
    pm1 FLOAT,
    pm10 FLOAT,
    INDEX idx_utc (recorded_at_utc),
    INDEX idx_local (recorded_at),
    INDEX idx_sensor (sensor_id),
    UNIQUE KEY uq_sensor_time (sensor_id, recorded_at_utc)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
