"""Current production schema contract; migration history stays frozen."""
from app.services.flavor_explorer.production_schema_v0058 import (
    metadata,verify_schema,EXPLORER_LOCK,SCHEMA_VERSION)
