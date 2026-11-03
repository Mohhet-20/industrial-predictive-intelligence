# Data Ingestion Contract

## Endpoint

`POST /api/v1/ingestion/measurements` — synchronous, for batches of up to
2,000 records. Returns a definitive result in the response.

`POST /api/v1/ingestion/batches` — asynchronous, for larger batches. Same
validation, runs on the background job runner. Returns `{job_id,
status_url}`; poll `GET /api/v1/jobs/{job_id}` for status/progress/result.

Both require a bearer token for a user with the `admin` or `engineer` role,
and both take the same request body:

```json
{
  "data_source_name": "plant-scada-export-2026-08",
  "records": [
    {
      "asset_code": "PUMP-07",
      "sensor_code": "bearing_temp_de",
      "timestamp": "2026-08-01T00:00:00Z",
      "value": 61.2,
      "unit": "celsius"
    }
  ]
}
```

`data_source_name` is get-or-create: the first ingestion under a new name
creates a `DataSource` row (type `api_client`) automatically. `unit` is
optional per record but, when present, is checked against the sensor's
declared unit.

## Per-field validation

| Field | Requirement | On failure |
|---|---|---|
| `asset_code` | Must match an existing Asset in the caller's organization | record rejected: "Unknown asset_code" |
| `sensor_code` | Must match an existing Sensor on that asset | record rejected: "Unknown sensor_code" |
| `timestamp` | ISO 8601, **must include a timezone offset** | naive timestamps rejected outright — this is the one thing this API will not guess for you |
| `timestamp` | Must not be more than 1 day in the future | rejected as implausible; a smaller future skew (>5 min) is accepted but flagged as a `timestamp_future` data-quality issue |
| `value` | Must be a finite number (no `Infinity`/`NaN`) | rejected |
| `value` | Missing/null | rejected, and recorded as a `missing_value` data-quality issue if the asset can still be identified |
| `unit` | If present, must equal the sensor's declared unit | rejected, and recorded as an `unsupported_unit` data-quality issue |

## What is never rejected, only flagged

- **Out-of-range values** (outside the sensor's `expected_min`/`expected_max`)
  are stored as-is and flagged with an `out_of_range` data-quality issue.
  Real sensors spike; the platform records what actually happened rather
  than discarding evidence.
- **Chronologically out-of-order batches** — a batch does not need to be
  sorted. Submitting a sensor's readings out of order is accepted and noted
  with one informational `timestamp_out_of_order` issue per sensor per
  batch.

## Duplicates

A record with a `(sensor, timestamp)` pair that already exists — either
already stored from a previous ingestion, or repeated within the same
batch — is **not an error**. It's counted separately
(`records_duplicate` in the response) and never overwrites the existing
value. This makes retries and overlapping-window re-uploads safe by
construction.

## Response shape (synchronous path)

```json
{
  "batch_id": "...",
  "status": "succeeded_with_errors",
  "records_submitted": 100,
  "records_accepted": 97,
  "records_rejected": 2,
  "records_duplicate": 1,
  "errors": [
    {"index": 14, "error": "Unknown sensor_code 'bearign_temp' for asset 'PUMP-07'.", "record": {...}}
  ]
}
```

`status` is one of `succeeded` (no rejections), `succeeded_with_errors`
(some records rejected, the rest still ingested), or `failed` (every record
in the batch was rejected — nothing to ingest). A partially-successful
batch is not an error condition at the HTTP level; check `status` and
`errors` in the body.

## What ingestion never does

- Never mutates an already-stored `SensorMeasurement`.
- Never "fixes" a bad record silently — every rejection has a specific,
  returned reason.
- Never blocks the whole batch because one record was malformed — parsing
  and validation happen per record.
