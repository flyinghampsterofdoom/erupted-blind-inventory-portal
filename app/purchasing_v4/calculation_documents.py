"""Explicit document inputs; never reads a profile or source implicitly."""
from datetime import date,datetime
from decimal import Decimal
from fractions import Fraction
from .contracts import WindowContract,CandidateModelInputs,QuantityEvidence,EvidenceState,ExposureDay,Availability
from .calculation_config import Parameters,StockoutControl,Coverage,Configuration,CategoryOverride
from .calculation import StoreInput,Supply,SupplyLot,Deduction,ManualRule,ProductCoverageOverride


def exact(value):
    if type(value) in (float,bool):
        raise ValueError('Exact rational inputs required; floats and booleans forbidden')
    if isinstance(value,dict):
        if set(value)!={'numerator','denominator'} or any(type(v) is not int for v in value.values()):
            raise ValueError('Invalid exact rational document')
        return Fraction(value['numerator'],value['denominator'])
    if not isinstance(value,(str,int,Decimal,Fraction)):
        raise ValueError('Exact rational input required')
    return Fraction(value)


def configuration_from_document(row):
    parameters=Parameters(CandidateModelInputs(WindowContract(row['lookback_days'],row['recent_days'],row['minimum_available_days']),
        *(exact(row[k]) for k in ('recent_influence','increasing_sensitivity','decreasing_sensitivity','increasing_limit','decreasing_limit'))),
        exact(row['reliability_depth_multiple']),StockoutControl(row['stockout_kind'],
            exact(row['stockout_threshold']) if row['stockout_threshold'] is not None else None),row['negative_inventory'])
    categories=[]
    for c in row.get('categories',[]):
        fields=[]
        for key,value in c['parameters'].items():
            if value is not None and key not in ('lookback_days','recent_days','minimum_available_days','negative_inventory','stockout'):
                value=exact(value)
            if key=='stockout' and value is not None:
                value=StockoutControl(value['kind'],exact(value['threshold']) if value['threshold'] is not None else None)
            fields.append((key,value))
        coverage=c.get('coverage')
        categories.append(CategoryOverride(c['category_id'],tuple(fields),None if coverage is None else
                                           Coverage(exact(coverage['reorder_weeks']),exact(coverage['target_weeks']))))
    return Configuration(row['id'],parameters,Coverage(exact(row['reorder_weeks']),exact(row['target_weeks'])),tuple(categories))


def quantity_from_document(row):
    return QuantityEvidence(EvidenceState(row['state']),Decimal(str(row['value'])) if row['value'] is not None else None,
        row['source'],row.get('reason',''),datetime.fromisoformat(row['observed_at']) if row.get('observed_at') else None,
        tuple(row.get('references',())))


def store_from_document(row):
    supply=row['supply'];manual=row.get('manual');override=row.get('product_coverage')
    return StoreInput(row['variation_id'],row['store_id'],row.get('category_id'),
        tuple(ExposureDay(date.fromisoformat(d['day']),Availability(d['availability']),quantity_from_document(d['sales']),d.get('availability_reason',''),tuple(d.get('availability_references',())))
              for d in row['history']),quantity_from_document(row['on_hand']),
        Supply(tuple(SupplyLot(x['identity'],x['stage'],quantity_from_document(x['quantity']),x['represented_in_on_hand']) for x in supply['incoming']),
            tuple(Deduction(x['identity'],quantity_from_document(x['quantity']),x['represented_in_on_hand']) for x in supply['outward_reserved']),
            tuple(Deduction(x['identity'],quantity_from_document(x['quantity']),x['represented_in_on_hand']) for x in supply['unavailable_holds']),
            supply['coherent_with_on_hand'],supply['watermark'],tuple(supply.get('coherence_reasons',()))),row['pack_size'],row['no_reorder'],
        ManualRule(manual['identity'],manual['par_units'],manual['level_units']) if manual else None,
        ProductCoverageOverride(Coverage(exact(override['coverage']['reorder_weeks']),exact(override['coverage']['target_weeks'])),override['authority_reference']) if override else None,
        date.fromisoformat(row['eligible_from']) if row.get('eligible_from') else None,tuple(row.get('identity_issues',())),tuple(row.get('policy_issues',())))
