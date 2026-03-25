# AirGradient Local Monitor

Self-hosted air quality dashboard for [AirGradient](https://www.airgradient.com/) sensors. Reads directly from your local sensor's API, stores historical data in MySQL, and serves a real-time dashboard.

## Features

- Real-time polling from AirGradient sensor local API (every 60s)
- Historical data stored in MySQL with 5-minute bucketing
- CSV import from AirGradient cloud export (auto-ingests on startup)
- Multi-sensor support with monitor selector dropdown
- Themeable via CSS custom properties
- Time range selector: 8 hours to all-time
- CSV export of current view or full database
- WHO Air Quality Guideline reference line on PM2.5 chart
- AQI color-coded metric cards (PM2.5, CO2, Temperature, Humidity, TVOC, NOx, Heat Index)

## Quick Start

```bash
git clone https://github.com/coffeegrind123/airgradient-monitor.git
cd airgradient-monitor
```

Copy `.env.example` to `.env` and set your sensor IP(s):
```bash
cp .env.example .env
# Edit .env with your sensor IP(s)
```

```env
# Single sensor
AIRGRADIENT_HOSTS=192.168.1.100

# Multiple sensors
AIRGRADIENT_HOSTS=192.168.1.100,192.168.1.101
```

```bash
docker compose up -d
```

Dashboard available at `http://localhost:8085`

## Importing Historical Data

1. [Export your data](https://app.airgradient.com/dashboard/export) from AirGradient as CSV (select "5 Minute Buckets")
2. Place the CSV file(s) in the `data/` directory
3. Restart the dashboard container — files are auto-ingested on startup

```bash
mkdir -p data
cp ~/Downloads/export.csv data/
docker compose restart airgradient-dashboard
```

Duplicate rows are safely skipped (keyed on sensor ID + timestamp).

## Configuration

All configuration is via environment variables in `docker-compose.yml`:

| Variable | Default | Description |
|----------|---------|-------------|
| `AIRGRADIENT_HOSTS` | `192.168.1.100` | Comma-separated IPs of your AirGradient sensors |
| `PORT` | `8080` | Internal server port |
| `DB_HOST` | `mysql` | MySQL hostname |
| `DB_PORT` | `3306` | MySQL port |
| `DB_USER` | `root` | MySQL user |
| `DB_PASS` | `airgradient` | MySQL password |
| `DB_NAME` | `airgradient` | MySQL database name |
| `COLLECT_INTERVAL` | `60` | Seconds between sensor polls |
| `LOCATION_ID` | `0` | Location ID for stored readings |

## Theming

The dashboard uses CSS custom properties for theming. Two themes are included:

- **`theme.css`** — Modern dark theme (default)
- **`theme-airgradient.css`** — 1:1 clone of the AirGradient cloud dashboard

To switch themes, change the `<link>` tag in `index.html`:
```html
<link rel="stylesheet" href="theme-airgradient.css">
```

Create your own theme by copying `theme.css` and modifying the CSS variables.

## API Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /` | Dashboard |
| `GET /api/measures/current` | Proxied live sensor data |
| `GET /api/history?hours=8&sensor=...` | Historical data from DB |
| `GET /api/sensors` | List of available sensors |
| `GET /api/export?sensor=...` | Full CSV export from DB |
| `GET /swagger` | Redirect to AirGradient API docs |

## Architecture

```
┌──────────────┐     ┌──────────────────┐     ┌─────────┐
│  AirGradient │────▶│  airgradient-    │────▶│  MySQL  │
│  Sensor      │     │  dashboard       │     │         │
│  (local API) │     │  (Python server) │◀────│         │
└──────────────┘     └──────┬───────────┘     └─────────┘
                            │
                     ┌──────▼───────────┐
                     │  Browser         │
                     │  (index.html +   │
                     │   Chart.js)      │
                     └──────────────────┘
```

## File Structure

```
├── index.html              # Dashboard frontend
├── base.css                # Structural CSS (shared by all themes)
├── theme.css               # Default modern theme
├── theme-airgradient.css   # AirGradient clone theme (reference)
├── server.py               # HTTP server + API proxy + data collector
├── import_csv.py           # CSV import script
├── schema.sql              # MySQL table schema
├── Dockerfile
├── docker-compose.yml
└── data/                   # Drop CSV exports here for auto-import
```

## License

MIT
