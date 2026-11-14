"""Explicit diagnostic only: compare pinned previous and candidate execution.

Run with erupted-dev test ... python -m pytest tests/benchmark_substitution.py.
Only synthetic fixtures are used. Not automatically collected by pytest.
"""
import json
import subprocess
import types
from pathlib import Path
from time import perf_counter
import pytest
from sqlalchemy import event
from app.services import scheduling_training_service as candidate
from test_scheduling_substitution import seed_representative, owner, START, NOW
from test_v2_scheduling_foundation import scheduling_db

@pytest.mark.parametrize('implementation',['previous','candidate'])
@pytest.mark.parametrize('weeks',[1,12])
def test_benchmark(scheduling_db,implementation,weeks):
    Session,actor,ids,engine=scheduling_db
    if implementation=='previous':
        module=types.ModuleType('previous_training')
        code=subprocess.check_output(['git','show','aca373d:app/services/scheduling_training_service.py'],text=True)
        exec(compile(code,'previous_training.py','exec'),module.__dict__)
    else:module=candidate
    with Session() as db:
        outgoing,incoming,stores=seed_representative(db,actor,ids,weeks)
        calls=[]
        def count(*args):calls.append(args[2])
        event.listen(engine,'before_cursor_execute',count)
        started=perf_counter()
        try:
            result=module.save_transition(db,principal=owner(actor),employee_id=incoming.id,
                first_schedule_date=START,replacing_employee_id=outgoing.id,
                allowed_store_ids=stores,now=NOW)
            db.commit()
        finally:
            elapsed=perf_counter()-started
            event.remove(engine,'before_cursor_execute',count)
        assert len(result['changes'])==weeks*3
        record=dict(implementation=implementation,weeks=weeks,assignments=len(result['changes']),
                    seconds=elapsed,sql_statements=len(calls),employees=117,periods=50,shifts=1312,
                    database='isolated local PostgreSQL 16',commit_included=True,production_mutations=False)
        path=Path('docs/development/scheduling-benchmark.json');path.parent.mkdir(exist_ok=True,parents=True)
        data=json.loads(path.read_text()) if path.exists() else {}
        data[implementation+'-'+str(weeks)]=record
        path.write_text(json.dumps(data,indent=2)+'\n')
