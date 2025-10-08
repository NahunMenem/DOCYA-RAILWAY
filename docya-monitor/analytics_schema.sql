-- ====================================================
-- 📊 SCHEMA ANALYTICS PARA DOCYA MONITOR
-- ====================================================

CREATE SCHEMA IF NOT EXISTS analytics;

CREATE TABLE IF NOT EXISTS analytics.events (
  id BIGSERIAL PRIMARY KEY,
  event_type TEXT NOT NULL,
  payload JSONB NOT NULL,
  source TEXT NOT NULL DEFAULT 'backend',
  created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_events_type ON analytics.events (event_type);
CREATE INDEX IF NOT EXISTS idx_events_created_at ON analytics.events (created_at DESC);
