"""Application-owned DDL; infrastructure owns schemas/volumes/grants."""
from .zerobus import table_name


def statements(catalog, ingest='ingest', telemetry='telemetry'):
    raw = table_name(catalog, ingest, 'delivery_items_raw')
    records = table_name(catalog, telemetry, 'records')
    items = table_name(catalog, telemetry, 'delivery_items')
    inference = table_name(catalog, telemetry, 'inference_calls')
    return [
        '''CREATE TABLE IF NOT EXISTS %s (
          enrollment_id STRING, installation_id STRING, destination_generation STRING,
          batch_id STRING, delivery_seq BIGINT, record_id STRING, record_revision BIGINT,
          record_kind STRING, record_sha256 STRING, item_json STRING) USING DELTA
          TBLPROPERTIES ('delta.enableChangeDataFeed'='true')''' % raw,
        '''CREATE TABLE IF NOT EXISTS %s (
          enrollment_id STRING, destination_generation STRING, delivery_seq BIGINT,
          record_id STRING, record_revision BIGINT, item_json STRING, materialized BOOLEAN, projection_disposition STRING) USING DELTA''' % items,
        '''CREATE TABLE IF NOT EXISTS %s (
          enrollment_id STRING, installation_id STRING, destination_generation STRING,
          record_id STRING, record_revision BIGINT, record_kind STRING,
          record_sha256 STRING, payload_json STRING) USING DELTA
          TBLPROPERTIES ('delta.enableChangeDataFeed'='true')''' % records,
        '''CREATE TABLE IF NOT EXISTS %s (
          event_time TIMESTAMP, request_id STRING, invocation_id STRING, request STRING,
          response STRING, destination_type STRING, destination_name STRING,
          destination_model STRING, requester STRING, schema_version STRING,
          url STRING, api_type STRING, request_tags MAP<STRING,STRING>, status_code INT,
          sampling_fraction DOUBLE, latency_ms BIGINT, time_to_first_byte_ms BIGINT,
          logging_error_codes ARRAY<STRING>, erg_record_id STRING, erg_record_revision BIGINT,
          erg_enrollment_id STRING, erg_session_id STRING, erg_usage_json STRING) USING DELTA''' % inference,
        '''CREATE OR REPLACE VIEW %s AS
          SELECT * EXCEPT(rank) FROM (SELECT *, ROW_NUMBER() OVER (
          PARTITION BY enrollment_id,destination_generation,record_id ORDER BY record_revision DESC) rank
          FROM %s) WHERE rank=1''' % (table_name(catalog, telemetry, 'current_records'), records),
        '''CREATE OR REPLACE VIEW %s AS SELECT * FROM %s
          WHERE record_kind IN ('session_event','content_tombstone')''' % (
            table_name(catalog, telemetry, 'session_events'), table_name(catalog, telemetry, 'current_records')),
    ]
