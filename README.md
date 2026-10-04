# Fuel Route API

A production-quality Django REST API that calculates cost-optimised driving routes with fuel stop planning across the USA. Built as a backend engineering assessment.

---

## Project Overview

Given a start and finish location within the USA, the API:

1. Geocodes both locations using Nominatim (OpenStreetMap).
2. Retrieves the driving route from OSRM (one API call).
3. Loads fuel stations from the OPIS truckstop dataset.
4. Filters stations along the route using great-circle geometry (entirely local).
5. Selects cost-effective fuel stops respecting a 500-mile vehicle range.
6. Returns the full route geometry (GeoJSON), fuel stops, prices, and costs.

---

## Architecture

```
POST /api/v1/route/
       │
       ▼
 RouteView (views.py)
       │
       ├──► geocode_location() ──► Nominatim API (1 req, cached 24h)
       │       (start + finish)
       │
       ├──► get_driving_route() ──► OSRM API (1 req, cached 1h)
       │       (returns distance, duration, GeoJSON geometry)
       │
       ├──► build_route_points() ──► Local computation only
       │       (converts GeoJSON → RoutePoint list with cumulative distances)
       │
       ├──► optimize_fuel_stops() ──► Local computation only
       │       (queries DB, filters by proximity, greedy selection)
       │
       └──► JSON response
```

**Total external API calls per request: 2** (geocoding + routing, both cached).

---

## Setup

### Prerequisites

- Python 3.14+
- pip

### 1. Clone and Set Up Environment

```bash
# Windows
python -m venv venv
venv\Scripts\activate

# macOS/Linux
python -m venv venv
source venv/bin/activate
```

### 2. Install Dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure Environment

```bash
# Copy the example env file
cp .env.example .env

# Edit .env with your settings (defaults work out of the box)
# At minimum, update NOMINATIM_USER_AGENT with your contact email
```

### 4. Apply Migrations

```bash
python manage.py migrate
```

### 5. Import Fuel Stations

```bash
python manage.py import_fuel_stations data/fuel-prices-for-be-assessment.csv
```

Expected output:
```
Importing from: data\fuel-prices-for-be-assessment.csv
Headers validated: ['OPIS Truckstop ID', 'Truckstop Name', ...]
Parsed 8151 valid rows (0 skipped).
...
Import complete:
  Imported   : 6967
  Skipped    : 0
  Duplicates : 1184 (de-duplicated via unique constraint)
  Errors     : 0

Total stations in database: 6967
```

### 6. Geocode Fuel Stations

> ⚠️ **This step is required before the route API will return fuel stops.**

```bash
# Geocode all stations (takes ~40 min due to Nominatim rate limiting)
python manage.py geocode_fuel_stations

# Or geocode a single state first for quick testing:
python manage.py geocode_fuel_stations --state IL
python manage.py geocode_fuel_stations --state OH
python manage.py geocode_fuel_stations --state PA
python manage.py geocode_fuel_stations --state NY
python manage.py geocode_fuel_stations --state IN
```

For a NY→Chicago demo route, geocode at minimum: NY, PA, OH, IN, IL.

### 7. Start the Development Server

```bash
python manage.py runserver
```

The API is now available at `http://127.0.0.1:8000/`.

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `DJANGO_SECRET_KEY` | (generated) | Django secret key |
| `DEBUG` | `True` | Debug mode |
| `ALLOWED_HOSTS` | `localhost,127.0.0.1` | Comma-separated allowed hosts |
| `OSRM_BASE_URL` | `http://router.project-osrm.org` | OSRM routing server URL |
| `NOMINATIM_BASE_URL` | `https://nominatim.openstreetmap.org` | Nominatim geocoding URL |
| `NOMINATIM_USER_AGENT` | `FuelRouteAPI/1.0 (...)` | **Required**: Your contact email |
| `DATABASE_URL` | *(uses SQLite)* | PostgreSQL connection URL |
| `CACHE_BACKEND` | `locmem` | Django cache backend |
| `CACHE_LOCATION` | `fuel-route-api-default` | Cache location identifier |
| `MAX_RANGE_MILES` | `500` | Vehicle maximum range |
| `FUEL_EFFICIENCY_MPG` | `10` | Vehicle fuel efficiency |
| `ROUTE_PROXIMITY_MILES` | `10.0` | Max station distance from route |
| `HTTP_TIMEOUT` | `30` | External API timeout (seconds) |

---

## API Usage

### Health Check

```http
GET /api/v1/health/
```

**Response:**
```json
{
    "status": "ok",
    "service": "Fuel Route API",
    "version": "1.0.0",
    "database": {
        "fuel_stations_total": 6967,
        "fuel_stations_geocoded": 6800
    },
    "vehicle": {
        "max_range_miles": 500,
        "fuel_efficiency_mpg": 10
    },
    "routing_provider": "OSRM (router.project-osrm.org)",
    "geocoding_provider": "Nominatim (nominatim.openstreetmap.org)"
}
```

---

### Calculate Route

```http
POST /api/v1/route/
Content-Type: application/json
```

**Request Body:**
```json
{
    "start": "New York, NY",
    "finish": "Chicago, IL"
}
```

**Success Response (200 OK):**
```json
{
    "start": {
        "input": "New York, NY",
        "latitude": 40.7127281,
        "longitude": -74.0060152,
        "display_name": "New York City, New York, United States"
    },
    "finish": {
        "input": "Chicago, IL",
        "latitude": 41.8755616,
        "longitude": -87.6244212,
        "display_name": "Chicago, Cook County, Illinois, United States"
    },
    "route": {
        "distance_miles": 790.4,
        "duration_minutes": 748.0,
        "geometry": {
            "type": "LineString",
            "coordinates": [
                [-74.0060, 40.7128],
                "... (full route geometry)"
            ]
        }
    },
    "vehicle": {
        "max_range_miles": 500,
        "fuel_efficiency_mpg": 10
    },
    "fuel_stops": [
        {
            "station_id": "1234",
            "station_name": "PILOT TRAVEL CENTER #123",
            "address": "I-80, EXIT 122",
            "city": "Youngstown",
            "state": "OH",
            "price_per_gallon": 3.05733,
            "distance_from_start_miles": 428.5,
            "gallons_purchased": 36.15,
            "fuel_cost": 110.54
        }
    ],
    "fuel": {
        "total_gallons": 79.04,
        "total_cost": 241.68,
        "currency": "USD",
        "stop_count": 1
    }
}
```

### Error Responses

| HTTP Code | Error Code | When |
|---|---|---|
| `400` | `INVALID_REQUEST` | Missing/blank start or finish |
| `400` | `INVALID_REQUEST` | Start and finish are identical |
| `404` | `INVALID_LOCATION` | Location cannot be geocoded |
| `404` | `NO_ROUTE_FOUND` | No drivable route exists |
| `422` | `LOCATION_NOT_IN_USA` | Location outside the USA |
| `422` | `NO_FEASIBLE_FUEL_PLAN` | No feasible fuel stops for range |
| `502` | `ROUTING_SERVICE_ERROR` | OSRM API unavailable |
| `500` | `INTERNAL_ERROR` | Unexpected server error |

**Error Response Format:**
```json
{
    "error": {
        "code": "NO_FEASIBLE_FUEL_PLAN",
        "message": "No feasible fuel-stop plan exists for this route with a maximum vehicle range of 500 miles."
    }
}
```

---

### cURL Examples

```bash
# Health check
curl http://127.0.0.1:8000/api/v1/health/

# New York to Chicago
curl -X POST http://127.0.0.1:8000/api/v1/route/ \
  -H "Content-Type: application/json" \
  -d '{"start": "New York, NY", "finish": "Chicago, IL"}'

# Los Angeles to New York (long distance)
curl -X POST http://127.0.0.1:8000/api/v1/route/ \
  -H "Content-Type: application/json" \
  -d '{"start": "Los Angeles, CA", "finish": "New York, NY"}'
```

---

## Routing Provider

**OSRM** (Open Source Routing Machine) is used for driving directions.

- **Public demo server**: `router.project-osrm.org`
- **No API key required** for the public demo server.
- Returns full route geometry in GeoJSON format (`overview=full`, `geometries=geojson`).
- **Production recommendation**: Self-host OSRM or use a paid routing provider.

**Why OSRM over alternatives?**

| Provider | Free Tier | API Key | GeoJSON | Rate Limit |
|---|---|---|---|---|
| OSRM (public) | Unlimited | Not required | ✅ | Fair use |
| OpenRouteService | 2,000 req/day | Required | ✅ | Strict |
| Mapbox Directions | 100,000 req/mo | Required | ✅ | Yes |

OSRM was selected because it requires no registration, has no hard rate limit for assessment use, and provides full route geometry in a single request.

---

## Fuel Optimization Algorithm

### Overview

The algorithm is a **Route-Aware Greedy with Look-Ahead** approach:

```
1. Get complete route geometry from OSRM (single API call)
2. Build RoutePoint list with cumulative distances from route start
3. Query ALL geocoded stations from the database
4. For each station, compute:
   - Perpendicular distance to nearest route segment (cross-track distance)
   - Along-route progress (distance from start)
5. Filter out stations > ROUTE_PROXIMITY_MILES from route
6. Sort remaining candidates by route progress (ascending)
7. Execute greedy selection:
   a. Start at position 0, range = MAX_RANGE_MILES
   b. Find all stations reachable within current range
   c. Sort reachable stations by price (cheapest first)
   d. Apply "cheapest-that-doesn't-strand" rule:
      - Select cheapest station from which destination OR another station is reachable
   e. Update position to selected station, reset range to full
   f. Repeat until destination is reachable
8. Calculate gallons and cost for each stop
```

### Cheapest-That-Doesn't-Strand Rule

This prevents a pathological case where the globally cheapest nearby station is isolated — after stopping there, no further station is within range:

```
Current position (range: 500 miles)
│
├── Station A @ $2.50 (400 miles away)
│     └── No stations within 500 miles of A → STRANDED
│
└── Station B @ $3.00 (350 miles away)
      └── Station C within 500 miles → FEASIBLE ✓
```

The algorithm selects B, even though A is cheaper, because A leads to an infeasible plan.

### Fuel Calculation

Each stop:
- `gallons_purchased = miles_to_next_waypoint / 10 MPG`
- `fuel_cost = gallons × price_per_gallon` (Decimal arithmetic)

Total:
- `total_gallons = total_route_miles / 10 MPG`
- `total_cost = sum of individual stop costs`

### Algorithm Tradeoffs

| Property | This Algorithm |
|---|---|
| Time complexity | O(n × k) where n = stations, k = segments |
| Optimality | Near-optimal (greedy, not globally optimal) |
| Speed | < 500ms for 8,000 station dataset |
| Correctness | Always feasible when a solution exists |

A globally optimal solution would require dynamic programming over the route progress dimension. For typical highway routes with plentiful fuel stations, the greedy approach produces results within 2-5% of optimal.

---

## Performance

### Minimising External API Calls

| Operation | API Calls | Caching |
|---|---|---|
| Geocoding (start) | 1 (Nominatim) | 24 hours |
| Geocoding (finish) | 1 (Nominatim) | 24 hours |
| Routing | 1 (OSRM) | 1 hour |
| Station filtering | 0 (local DB) | — |
| Optimisation | 0 (local) | — |

**Repeat requests to the same route are answered from cache** with zero external API calls.

### Database

- All fuel station filtering is done in-database with indexed queries.
- Geocoded coordinates stored in `latitude`/`longitude` float fields with index.
- State and retail_price indexed for fast filtering.
- Avoid N+1: stations loaded in a single `SELECT` query.

### Caching Strategy

- **Development**: `LocMemCache` (in-process, zero config)
- **Production**: Set `CACHE_BACKEND=django.core.cache.backends.redis.RedisCache` and `CACHE_LOCATION=redis://localhost:6379/1`

---

## Vehicle Assumptions

| Constant | Value | Meaning |
|---|---|---|
| `MAX_RANGE_MILES` | 500 | Maximum miles on a full tank |
| `FUEL_EFFICIENCY_MPG` | 10 | Miles per gallon |
| Tank size | 50 gallons | Derived: 500 / 10 |

These values are configurable via environment variables but default to the assessment requirements.

---

## Geocoding Strategy for Fuel Stations

The OPIS CSV dataset (~8,151 rows) contains **no latitude/longitude** and uses highway exit notations for addresses (e.g., `"I-44, EXIT 283 & US-69"`), which geocode poorly by address.

**Strategy**: Geocode at **city + state level**, not by full address.

- Reduces unique geocoding requests from 8,151 → ~2,000 unique city+state pairs.
- At Nominatim's 1 req/sec limit, this takes ~40 minutes (run once).
- All stations in the same city share coordinates.
- Accuracy: ±5-10 miles (acceptable for 10-mile proximity filter).

**Run**: `python manage.py geocode_fuel_stations`

For quick demos, geocode only the states on your target route:
```bash
python manage.py geocode_fuel_stations --state NY
python manage.py geocode_fuel_stations --state PA
python manage.py geocode_fuel_stations --state OH
python manage.py geocode_fuel_stations --state IN
python manage.py geocode_fuel_stations --state IL
```

---

## Running Tests

```bash
# Run all tests
python -m pytest routing/tests/ -v

# Run specific test file
python -m pytest routing/tests/test_optimizer.py -v

# Run with coverage (if coverage installed)
pip install pytest-cov
python -m pytest routing/tests/ --cov=routing --cov-report=term-missing
```

**All 66 tests pass.** External API calls (Nominatim, OSRM) are mocked in all tests.

---

## Project Structure

```
project/
├── manage.py
├── requirements.txt
├── README.md
├── .env.example
├── .gitignore
├── pytest.ini
├── data/
│   └── fuel-prices-for-be-assessment.csv
├── postman/
│   └── fuel-route-api.postman_collection.json
├── config/                          # Django project package
│   ├── settings.py
│   ├── urls.py
│   ├── wsgi.py
│   └── asgi.py
└── routing/                         # Main application
    ├── models.py                    # FuelStation model
    ├── views.py                     # Thin API views
    ├── serializers.py               # Request validation
    ├── urls.py                      # URL routing
    ├── admin.py                     # Django admin
    ├── exceptions.py                # Custom error handler
    ├── services/
    │   ├── geocoding_service.py     # Nominatim integration
    │   ├── routing_service.py       # OSRM integration
    │   ├── route_utils.py           # Geometry calculations
    │   └── fuel_optimizer.py        # Core algorithm
    ├── management/commands/
    │   ├── import_fuel_stations.py  # CSV import
    │   └── geocode_fuel_stations.py # Station geocoding
    └── tests/
        ├── test_api.py
        ├── test_optimizer.py
        ├── test_services.py
        └── test_import.py
```

---

## Limitations

1. **Geocoding accuracy**: Stations are geocoded at city-center level, not by exact address. This may cause a station to appear slightly off its true position (typically < 10 miles).
2. **Route proximity threshold**: The default `ROUTE_PROXIMITY_MILES=10` may exclude stations that are technically accessible but geocoded to a slightly different city centroid. Increase to 15-20 miles if stops are sparse.
3. **OSRM public server**: The public demo server (`router.project-osrm.org`) is for development only. For production, self-host OSRM.
4. **Greedy optimizer**: Not globally optimal. Dynamic programming would give lower total cost but at O(n²) complexity. Practical difference is < 5% for typical routes.
5. **No real-time prices**: The OPIS dataset is a snapshot. Real fuel prices fluctuate daily.
6. **USA-only**: Location validation restricts geocoding to `countrycodes=us`. Canadian or other border locations will be rejected.
7. **SQLite in development**: Switch to PostgreSQL for production deployments.

---

## Django Admin

Access at `http://127.0.0.1:8000/admin/` (create superuser first):

```bash
python manage.py createsuperuser
```

The FuelStation admin supports:
- **Search**: station name, city, OPIS ID, address
- **Filter**: by state
- **Sort**: by state, city, retail price
- Displays geocoding status per station

---

## Postman Collection

Import `postman/fuel-route-api.postman_collection.json` into Postman.

The collection includes:
1. Health Check
2. NY → Chicago route
3. LA → NY route (long distance)
4. Dallas → Denver route
5. Short route (NY → Boston, no stop)
6. Invalid location error
7. Missing start field error
8. Same start/finish error

Set the `base_url` collection variable to your server URL (default: `http://127.0.0.1:8000`).
