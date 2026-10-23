"""One-off bootstrap: discard inherited business/provider-admin environment first."""
import os,sys
# Only stdlib before environment replacement. No business app/config import.
keep=('PATH','HOME','PYTHONPATH','LANG','LC_ALL','ENVIRONMENT','SQUARE_ACCESS_TOKEN',
      'SQUARE_ENVIRONMENT','RENDER_GIT_COMMIT','FV_QUALIFICATION_CONFIG','FV_EXECUTION_DIRECTORY',
      'FORECAST_QUALIFICATION_SOURCE_DATABASE_URL','FORECAST_QUALIFICATION_EVIDENCE_DATABASE_URL',
      'FORECAST_VALIDATION_BACKUP_ENDPOINT_URL','FORECAST_VALIDATION_BACKUP_BUCKET',
      'FORECAST_VALIDATION_BACKUP_ACCESS_KEY_ID','FORECAST_VALIDATION_BACKUP_SECRET_ACCESS_KEY')
filtered={k:os.environ[k] for k in keep if k in os.environ}
filtered.update(FORECAST_VALIDATION_WORKER_ENABLED='false',FORECAST_VALIDATION_SCHEDULE_ENABLED='false')
os.execve(sys.executable,[sys.executable,'-m','app.forecast_validation.execution',*sys.argv[1:]],filtered)
