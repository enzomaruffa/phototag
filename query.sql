WITH all_events AS (
  -- Unrecognized events with event detail
  SELECT 
    'unrecognized' AS table_source,
    context.device.model,
    event,
    COUNT(DISTINCT userId) AS unique_users
  FROM `plexbigdata00.events_prod.unrecognized`
  WHERE TIMESTAMP_TRUNC(timestamp, DAY) > TIMESTAMP("2025-10-01")
    AND TIMESTAMP_TRUNC(timestamp, DAY) < TIMESTAMP("2025-10-15")
    AND context.device.platform = "Kepler"
  GROUP BY context.device.model, event
  
  UNION ALL
  
  -- Playback events with event detail
  SELECT 
    'playback' AS table_source,
    context.device.model,
    event,
    COUNT(DISTINCT userId) AS unique_users
  FROM `plexbigdata00.events_prod.playback`
  WHERE TIMESTAMP_TRUNC(timestamp, DAY) > TIMESTAMP("2025-10-01")
    AND TIMESTAMP_TRUNC(timestamp, DAY) < TIMESTAMP("2025-10-15")
    AND context.device.platform = "Kepler"
  GROUP BY context.device.model, event
  
  UNION ALL
  
  -- Ad events with event detail
  SELECT 
    'ad' AS table_source,
    context.device.model,
    event,
    COUNT(DISTINCT userId) AS unique_users
  FROM `plexbigdata00.events_prod.ad`
  WHERE TIMESTAMP_TRUNC(timestamp, DAY) > TIMESTAMP("2025-10-01")
    AND TIMESTAMP_TRUNC(timestamp, DAY) < TIMESTAMP("2025-10-15")
    AND context.device.platform = "Kepler"
  GROUP BY context.device.model, event
  
  UNION ALL
  
  -- Client events with event detail
  SELECT 
    'client' AS table_source,
    context.device.model,
    event,
    COUNT(DISTINCT userId) AS unique_users
  FROM `plexbigdata00.events_prod.client`
  WHERE TIMESTAMP_TRUNC(timestamp, DAY) > TIMESTAMP("2025-10-01")
    AND TIMESTAMP_TRUNC(timestamp, DAY) < TIMESTAMP("2025-10-15")
    AND context.device.platform = "Kepler"
  GROUP BY context.device.model, event
  
  UNION ALL
  
  -- User events with event detail
  SELECT 
    'user' AS table_source,
    context.device.model,
    event,
    COUNT(DISTINCT userId) AS unique_users
  FROM `plexbigdata00.events_prod.user`
  WHERE TIMESTAMP_TRUNC(timestamp, DAY) > TIMESTAMP("2025-10-01")
    AND TIMESTAMP_TRUNC(timestamp, DAY) < TIMESTAMP("2025-10-15")
    AND context.device.platform = "Kepler"
  GROUP BY context.device.model, event
  
  UNION ALL
  
  -- Device events with event detail
  SELECT 
    'device' AS table_source,
    context.device.model,
    event,
    COUNT(DISTINCT userId) AS unique_users
  FROM `plexbigdata00.events_prod.device`
  WHERE TIMESTAMP_TRUNC(timestamp, DAY) > TIMESTAMP("2025-10-01")
    AND TIMESTAMP_TRUNC(timestamp, DAY) < TIMESTAMP("2025-10-15")
    AND context.device.platform = "Kepler"
  GROUP BY context.device.model, event
  
  UNION ALL
  
  -- Other_allowed events with event detail
  SELECT 
    'other_allowed' AS table_source,
    context.device.model,
    event,
    COUNT(DISTINCT userId) AS unique_users
  FROM `plexbigdata00.events_prod.other_allowed`
  WHERE TIMESTAMP_TRUNC(timestamp, DAY) > TIMESTAMP("2025-10-01")
    AND TIMESTAMP_TRUNC(timestamp, DAY) < TIMESTAMP("2025-10-15")
    AND context.device.platform = "Kepler"
  GROUP BY context.device.model, event
  
  UNION ALL
  
  -- Provider events with event detail
  SELECT 
    'provider' AS table_source,
    context.device.model,
    event,
    COUNT(DISTINCT userId) AS unique_users
  FROM `plexbigdata00.events_prod.provider`
  WHERE TIMESTAMP_TRUNC(timestamp, DAY) > TIMESTAMP("2025-10-01")
    AND TIMESTAMP_TRUNC(timestamp, DAY) < TIMESTAMP("2025-10-15")
    AND context.device.platform = "Kepler"
  GROUP BY context.device.model, event
)

SELECT 
  event,
  table_source,
  model,
  unique_users
FROM all_events
ORDER BY 
  event,
  table_source,
  model,
  unique_users DESC
LIMIT 1000
