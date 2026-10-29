"""Golden business boundaries independent of the V3 or candidate forecast engine."""
from dataclasses import FrozenInstanceError
from datetime import date, timedelta
from decimal import Decimal
from fractions import Fraction
import json
from pathlib import Path
import pytest
from app.purchasing_v4.contracts import (QuantityEvidence, EvidenceState, Availability,
    ExposureDay, WindowContract, CandidateModelInputs, confirmed_velocity, final_pack_conversion, classify_interval)


FIXTURE = json.loads((Path(__file__).parent/'fixtures/purchasing_v4_phase1_math.json').read_text())


@pytest.mark.parametrize('value', ['0', '2.5', '-2'])
def test_known_exact_inventory_including_zero_and_negative(value):
    evidence = QuantityEvidence(EvidenceState.KNOWN, Decimal(value), 'fixture')
    assert evidence.value == Decimal(value)
    with pytest.raises(FrozenInstanceError):
        evidence.value = Decimal(9)


@pytest.mark.parametrize('state,value', [
    (EvidenceState.UNKNOWN, Decimal(0)), (EvidenceState.UNAVAILABLE, Decimal(0)),
    (EvidenceState.KNOWN, None), (EvidenceState.KNOWN, Decimal('NaN')),
    (EvidenceState.KNOWN, Decimal('Infinity')), (EvidenceState.KNOWN, 0), ('missing', None)])
def test_evidence_rejects_false_zero_or_invalid_quantity(state, value):
    with pytest.raises(ValueError):
        QuantityEvidence(state, value, 'fixture', 'missing')


def test_unknown_and_unavailable_stay_distinct():
    assert QuantityEvidence(EvidenceState.UNKNOWN, None, 'fixture', 'gap').state != \
           QuantityEvidence(EvidenceState.UNAVAILABLE, None, 'fixture', 'no source').state


def test_windows_are_disjoint_and_only_approved_default_is_fourteen():
    config = WindowContract(182, 56)
    old, recent = config.split(date(2026, 10, 27))
    assert old[1] == recent[0] and (recent[1]-old[0]).days == 182
    assert (recent[1]-recent[0]).days == 56
    assert config.minimum_available_days == 14
    with pytest.raises(TypeError):
        WindowContract()


def test_candidate_inputs_have_independent_sensitivities_and_no_tuning_defaults():
    with pytest.raises(TypeError):CandidateModelInputs(WindowContract(182,56))
    inputs=CandidateModelInputs(WindowContract(182,56),Fraction(2,5),Fraction(3,5),
                               Fraction(3,10),Fraction(1),Fraction(1))
    assert inputs.increasing_sensitivity != inputs.decreasing_sensitivity
    with pytest.raises(ValueError):
        CandidateModelInputs(WindowContract(182,56),Fraction(2),Fraction(0),Fraction(0),Fraction(1),Fraction(1))


@pytest.mark.parametrize('args', [(14,14), (0,1), (30,0), (30,10,0), (30,10,31), (30.0,10)])
def test_window_validation(args):
    with pytest.raises(ValueError):
        WindowContract(*args)


def test_velocity_excludes_unknown_stockout_and_incomplete_sales():
    rows = tuple(ExposureDay(date(2026,1,1)+timedelta(days=2*n), Availability.AVAILABLE,
                            QuantityEvidence(EvidenceState.KNOWN, Decimal(2), 'observed')) for n in range(20))
    rows += (ExposureDay(date(2026,3,1), Availability.UNKNOWN,
                        QuantityEvidence(EvidenceState.KNOWN, Decimal(100), 'observed')),
             ExposureDay(date(2026,3,2), Availability.STOCKOUT,
                        QuantityEvidence(EvidenceState.KNOWN, Decimal(100), 'contradictory')),
             ExposureDay(date(2026,3,3), Availability.AVAILABLE,
                        QuantityEvidence(EvidenceState.UNAVAILABLE, None, 'observed', 'gap')))
    assert confirmed_velocity(rows, 14) == 14
    assert confirmed_velocity(rows[:13], 14) is None
    assert confirmed_velocity(rows[:14], 14) == 14
    zeros = tuple(ExposureDay(r.day, r.availability, QuantityEvidence(EvidenceState.KNOWN, Decimal(0), 'observed'))
                  for r in rows[:14])
    assert confirmed_velocity(zeros, 14) == 0
    with pytest.raises(ValueError):
        confirmed_velocity(rows+rows[:1], 14)


@pytest.mark.parametrize('case', FIXTURE['approved_pack_cases'])
def test_approved_final_pack_fixtures(case):
    assert final_pack_conversion(Fraction(case['units']), case['pack']) == case['expected']


def test_store_rounding_is_before_consolidation_and_monotonic():
    assert sum(final_pack_conversion(Fraction(n),5) for n in (1,6,8,3)) == 5
    assert final_pack_conversion(Fraction(18),5) == 4  # pooled answer would be wrong
    for pack in range(1,11):
        values = [final_pack_conversion(Fraction(n,2), pack) for n in range(301)]
        assert values == sorted(values)


@pytest.mark.parametrize('units,pack', [(Fraction(-1),5), (1.0,5), (Fraction(1),0), (Fraction(1),True)])
def test_invalid_pack_inputs(units, pack):
    with pytest.raises(ValueError):
        final_pack_conversion(units, pack)


@pytest.mark.parametrize('opening,deltas,complete,count,expected', [
    ('10', ('-2','3'), True, False, Availability.AVAILABLE),
    ('0', (), True, False, Availability.STOCKOUT),
    ('5', ('-5','5'), True, False, Availability.UNKNOWN),
    ('10', (), False, False, Availability.UNKNOWN),
    ('10', (), True, True, Availability.UNKNOWN),
    ('-1', (), True, False, Availability.UNKNOWN)])
def test_full_interval_exposure_proof(opening,deltas,complete,count,expected):
    assert classify_interval(QuantityEvidence(EvidenceState.KNOWN, Decimal(opening), 'anchor'),
           tuple(map(Decimal,deltas)), complete=complete, physical_count=count) == expected


def test_independent_candidate_arithmetic_and_policy_units():
    # No adaptive implementation is shipped. These are reviewable Phase2 oracles.
    for row in FIXTURE['candidate_arithmetic_not_defaults']:
        weight = Fraction(row['recent_weight'])
        result = (1-weight)*Fraction(row['historical_weekly'])+weight*Fraction(row['recent_weekly'])
        assert result == Fraction(row['expected_weekly'])
    manual = FIXTURE['manual_units']
    assert manual['on_hand']+manual['incoming'] == manual['par']
    assert manual['level']-(manual['on_hand']+manual['incoming']) == manual['expected_units']
    auto = FIXTURE['automatic_weeks']
    assert Fraction(auto['weekly_demand'])*auto['reorder_weeks'] == auto['position']
    assert Fraction(auto['weekly_demand'])*auto['target_weeks']-auto['position'] == auto['expected_units']
    assert FIXTURE['manual_conversion'] == dict(automatic_expiry=False, history_check_trigger='owner',
                                               preselected=False, conversion_action='Use Automatic Engine')
