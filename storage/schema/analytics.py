_ANALYTICS_SCHEMA = """
CREATE SEQUENCE IF NOT EXISTS seq_benchmark_runs    START 1 INCREMENT 1;
CREATE SEQUENCE IF NOT EXISTS seq_benchmark_results START 1 INCREMENT 1;
CREATE SEQUENCE IF NOT EXISTS seq_benchmark_prompts START 1 INCREMENT 1;

CREATE TABLE IF NOT EXISTS benchmark_runs (
    id       BIGINT PRIMARY KEY,
    ts       DOUBLE  NOT NULL,
    git_hash VARCHAR,
    mode     VARCHAR DEFAULT 'standard',
    notes    VARCHAR
);

CREATE TABLE IF NOT EXISTS benchmark_results (
    id             BIGINT PRIMARY KEY,
    run_id         BIGINT  NOT NULL,
    model          VARCHAR NOT NULL,
    accuracy_pct   DOUBLE,
    correct        INTEGER,
    total          INTEGER,
    p50_ms         DOUBLE,
    p95_ms         DOUBLE,
    vram_before_gb DOUBLE,
    vram_after_gb  DOUBLE,
    vram_delta_gb  DOUBLE,
    error          VARCHAR
);

CREATE TABLE IF NOT EXISTS benchmark_prompts (
    id        BIGINT PRIMARY KEY,
    result_id BIGINT  NOT NULL,
    prompt    VARCHAR NOT NULL,
    expected  VARCHAR NOT NULL,
    got       VARCHAR,
    correct   BOOLEAN,
    p50_ms    DOUBLE,
    p95_ms    DOUBLE
);
"""
