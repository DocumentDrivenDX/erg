# Adapters

Adapters emit canonical `metric` events: `{"t":"metric","src","m","ts","iv","v","pk"}`
(percent 0-100, memory MB, disk/net KB/s, temperature C).

## iStat Menus 7
Read-only copy of `~/Library/Application Support/iStat Menus 7/history/history.db`. Ring buffers: fine tiers expire, so sync regularly.
Sensor names are hashed; temperature and fan are inferred by value range.

## Endpoint-metrics CSV
Point at directories containing `endpoint-metrics.csv` (and optionally `container-metrics.csv`) via
`$USAGE_ENDPOINT_CSV_DIRS` (colon-separated) or `~/.config/usage/config.json`:
`{"endpoint_csv_dirs": ["/path/to/dir"]}`.
Columns used: `ts_utc, cpu_busy_pct, cpu_user_pct, cpu_sys_pct, am_used_pct, am_pressure_pct, am_used_mb, am_compressed_mb,
am_swap_used_mb, load1, load_norm, therm_pct, disk_pct, comp_ratio, demand_mb, pressure, swapins_cum, swapouts_cum,
decompressions_cum, top_apps_mem_plus_cmprs`; container CSV: `vm_footprint_mb, containers_running, match_cpu_pct, other_cpu_pct`.
Missing columns are skipped; `-1` means unavailable.

## Databricks AI Gateway
`usage add-gateway --profile <cli-profile>`; reads `system.ai_gateway.usage` through the Databricks CLI login.
Needs SELECT on that table. Gateway rows are stored as tool `gateway` with the requester.

## Priority
Per metric, the first source with data wins: endpointcsv > istat > sampler (disk/net/load prefer istat).
