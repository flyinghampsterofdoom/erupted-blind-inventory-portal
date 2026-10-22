"""The production rehearsal tools cannot target operational databases."""
import importlib.util
from pathlib import Path
import pytest

ROOT=Path(__file__).resolve().parents[1]

def module(name):
    spec=importlib.util.spec_from_file_location(name,ROOT/'scripts/release'/f'{name}.py')
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod);return mod

@pytest.mark.parametrize('url',[
    'postgresql+psycopg://user:password@production.example/erupted_prebeta_release_test',
    'postgresql+psycopg://user@localhost/production',
    'postgresql+psycopg://user@127.0.0.1/postgres',
    'postgresql+psycopg:///erupted_prebeta_release_test',
])
def test_rehearsal_refuses_non_disposable_or_non_loopback_targets(url):
    with pytest.raises(ValueError,match='disposable loopback'):
        module('rehearse_prebeta').target_url(url)


def test_rehearsal_accepts_private_loopback_postgres():
    url=module('rehearse_prebeta').target_url('postgresql+psycopg://user@127.0.0.1:55432/erupted_prebeta_release_test')
    assert url.port==55432

@pytest.mark.parametrize('name',[
    'schedule_approvals','schedule_generation_runs','scheduling_preference_requests',
    'stores','store_operating_hours','store_special_hours',
    'inventory_store_access_grants','employees','principals','coverage_requirements',
    'schedule_generator_snapshots','schedule_coverage_commitments','lead_duty_outcomes',
])
def test_recovery_protects_new_and_existing_operational_evidence(name):
    assert module('prebeta_recovery_guard').protected(name)


def test_recovery_leaves_inventory_and_session_storage_available():
    guard=module('prebeta_recovery_guard')
    assert not guard.protected('count_sessions')
    assert not guard.protected('web_sessions')


@pytest.mark.parametrize('path', ['/password','/forgot-password','/admin/settings/integrations/test',
    '/management/stores/1/credentials','/management/employee-logs/employees/1/deactivate',
    '/v2/scheduling/api/assignment-change','/v2/hr/employees/1'])
def test_recovery_rejects_all_account_and_scheduling_mutation_front_doors(path):
    assert module('prebeta_recovery_guard').blocked_mutation_path(path)

@pytest.mark.parametrize('path',['/management/stores/1/set-next-group','/store/sessions/1/draft','/login','/logout'])
def test_recovery_keeps_unaffected_inventory_and_authentication_paths(path):
    assert not module('prebeta_recovery_guard').blocked_mutation_path(path)


def test_prepared_recovery_python_compiles(tmp_path, monkeypatch):
    artifact = module('prepare_prebeta_artifacts')
    # Archives intentionally have no Git metadata. Supply the packaging inventory
    # so this regression runs against both a checkout and the exact frozen source.
    files = ['app/main.py', 'app/services/v2_scheduling_board_service.py',
        'app/templates/v2/base.html', 'app/templates/legacy_base.html',
        'scripts/run_schedule_automation.py', 'app/services/v2_scheduling_policy_service.py',
        'app/schema_contract.py', 'scripts/release/prebeta_recovery_guard.py']
    def git_output(command, **kwargs):
        if command[:2] == ['git', 'ls-files']:
            return ('\0'.join(files) + '\0').encode()
        assert command == ['git', 'rev-parse', 'HEAD']
        return '0' * 40 + '\n'
    monkeypatch.setattr(artifact.subprocess, 'check_output', git_output)
    output = tmp_path / 'artifacts'
    artifact.prepare(ROOT, output)
    for path in (output / 'recovery').rglob('*.py'):
        compile(path.read_text(), str(path), 'exec')


def test_recovery_accepts_only_equivalent_postgres_status_check_casts():
    canonical = module('prebeta_recovery_guard').canonical_preference_status_check
    values = ('pending', 'approved', 'denied', 'withdrawn', 'revoked')
    outer = "status::text = any (array[" + ', '.join("'" + v + "'::character varying" for v in values) + "]::text[])"
    elements = "status::text = any (array[" + ', '.join("'" + v + "'::character varying::text" for v in values) + "])"
    assert canonical(elements) == canonical(outer) == outer
    for altered in (elements.replace('revoked', 'other'), elements.replace("'pending'::character varying::text, ", ''), elements.replace('status::text', 'reason::text')):
        assert canonical(altered) == altered and canonical(altered) != outer


@pytest.mark.parametrize('statement',[
    'db.add(warning)', 'db.add_all(warnings)', 'db.flush()', 'db.commit()',
    'db.execute(delete(ScheduleWarning))', 'rebuild_schedule_warnings(db)',
])
def test_recovery_pure_board_check_rejects_new_maintenance_writes(statement):
    source='def serialize_week_board(db):\n    evaluate_schedule_warnings(db)\n    '+statement+'\n'
    assert not module('prepare_prebeta_artifacts').read_only_board(source)


def test_recovery_pure_board_check_requires_known_evaluator():
    guard=module('prepare_prebeta_artifacts').read_only_board
    assert guard('def serialize_week_board(db):\n    return evaluate_schedule_warnings(db)\n')
    assert not guard('def serialize_week_board(db):\n    return other_evaluator(db)\n')
