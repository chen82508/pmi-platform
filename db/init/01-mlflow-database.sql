-- MLflow keeps its tracking + Prompt Registry schema in its own database so
-- its migrations never collide with pmi-core's.
SELECT 'CREATE DATABASE mlflow'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'mlflow')\gexec
