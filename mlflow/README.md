# mlflow

MLflow tracking server image (upstream MLflow, plus the Postgres driver and
boto3).

Postgres holds the authoritative copies: `core_prompts` and
`audit_evaluations` are append-only. MLflow is a mirror that adds a UI:

| Platform record | MLflow mirror |
|---|---|
| Prompt template (sha256, append-only) | Prompt Registry entry; URI stored back on the row |
| Pipeline tick | Parent run (experiment per index) |
| Factor evaluation | Child run (optional) |

When MLflow is down, scoring still succeeds and the MLflow id columns stay
NULL. Set `PMI_MLFLOW_ENABLED=false` to turn the mirror off.

- Backend store: the `mlflow` database, created by `db/init`.
- Artifact store: the `pmi-mlartifacts` volume.
- Port: 5000, published on `127.0.0.1:5500`.
