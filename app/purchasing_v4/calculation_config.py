"""Immutable candidate configuration inputs. No installed operational profile."""
from dataclasses import dataclass, replace
from fractions import Fraction
from .contracts import CandidateModelInputs, WindowContract
from .identity import digest


@dataclass(frozen=True)
class StockoutControl:
    kind: str
    threshold: Fraction | None

    def __post_init__(self):
        if self.kind == 'confirmed_only':
            if self.threshold is not None:
                raise ValueError('Confirmed-only baseline has no extra numerical gate')
        elif self.kind in ('minimum_available_share', 'maximum_unknown_share'):
            if not isinstance(self.threshold, Fraction) or not 0 <= self.threshold <= 1:
                raise ValueError('Candidate exposure threshold must be an explicit fraction in [0,1]')
        else:
            raise ValueError('Unsupported candidate Stockout Adjustment interpretation')


@dataclass(frozen=True)
class Parameters:
    model: CandidateModelInputs
    reliability_depth_multiple: Fraction
    stockout: StockoutControl
    negative_inventory: str

    def __post_init__(self):
        if not isinstance(self.model, CandidateModelInputs) or not isinstance(self.stockout, StockoutControl):
            raise ValueError('Explicit validated model and exposure control required')
        if not isinstance(self.reliability_depth_multiple, Fraction) or self.reliability_depth_multiple <= 0:
            raise ValueError('Reliability depth multiplier must be explicit and positive')
        if self.negative_inventory not in ('preserve_qualified_negative', 'abstain_on_negative'):
            raise ValueError('Explicit candidate negative-inventory treatment required')

    def values(self):
        return dict(lookback_days=self.model.windows.lookback_days,
                    recent_days=self.model.windows.recent_days,
                    minimum_available_days=self.model.windows.minimum_available_days,
                    recent_influence=self.model.recent_influence,
                    increasing_sensitivity=self.model.increasing_sensitivity,
                    decreasing_sensitivity=self.model.decreasing_sensitivity,
                    increasing_limit=self.model.increasing_limit,
                    decreasing_limit=self.model.decreasing_limit,
                    reliability_depth_multiple=self.reliability_depth_multiple,
                    stockout=self.stockout, negative_inventory=self.negative_inventory)

    def override(self, values):
        base = self.values()
        if set(values)-set(base):
            raise ValueError('Unknown category parameter')
        base.update({key:value for key,value in values.items() if value is not None})
        return Parameters(CandidateModelInputs(
            WindowContract(base['lookback_days'],base['recent_days'],base['minimum_available_days']),
            *(base[key] for key in ('recent_influence','increasing_sensitivity','decreasing_sensitivity',
                                   'increasing_limit','decreasing_limit'))),
            base['reliability_depth_multiple'],base['stockout'],base['negative_inventory'])


@dataclass(frozen=True)
class Coverage:
    reorder_weeks: Fraction
    target_weeks: Fraction

    def __post_init__(self):
        if (not isinstance(self.reorder_weeks,Fraction) or not isinstance(self.target_weeks,Fraction)
                or not 0 < self.reorder_weeks < self.target_weeks):
            raise ValueError('Automatic coverage requires explicit positive weeks and a higher target')


@dataclass(frozen=True)
class CategoryOverride:
    category_id: str
    parameters: tuple[tuple[str, object], ...]
    coverage: Coverage | None

    def __post_init__(self):
        if (not isinstance(self.category_id,str) or not self.category_id or not isinstance(self.parameters,tuple)
                or any(not isinstance(p,tuple) or len(p)!=2 or not isinstance(p[0],str) for p in self.parameters)):
            raise ValueError('Immutable category override required')
        if len(dict(self.parameters)) != len(self.parameters):
            raise ValueError('Duplicate category parameter')
        if self.coverage is not None and not isinstance(self.coverage,Coverage):
            raise ValueError('Invalid category coverage')


@dataclass(frozen=True)
class Configuration:
    version: str
    global_parameters: Parameters
    global_coverage: Coverage
    categories: tuple[CategoryOverride, ...]

    def __post_init__(self):
        if (not isinstance(self.version,str) or not self.version or not isinstance(self.categories,tuple)
                or not isinstance(self.global_parameters,Parameters) or not isinstance(self.global_coverage,Coverage)
                or any(not isinstance(c,CategoryOverride) for c in self.categories)):
            raise ValueError('Versioned immutable configuration input required')
        if len({c.category_id for c in self.categories}) != len(self.categories):
            raise ValueError('Duplicate category override')
        for category in self.categories:
            self.global_parameters.override(dict(category.parameters))

    def resolve(self, category_id):
        category=next((c for c in self.categories if c.category_id==category_id),None)
        parameters=self.global_parameters if category is None else self.global_parameters.override(dict(category.parameters))
        coverage=category.coverage if category and category.coverage is not None else self.global_coverage
        inherited=tuple((key,'CATEGORY' if category and dict(category.parameters).get(key) is not None else 'GLOBAL')
                        for key in parameters.values())
        return parameters,coverage,inherited,'CATEGORY' if category and category.coverage else 'GLOBAL'

    def without_category_parameter(self,category_id,key,*,new_version):
        if key not in self.global_parameters.values():
            raise ValueError('Unknown category parameter')
        return replace(self,version=new_version,categories=tuple(
            replace(c,parameters=tuple((k,v) for k,v in c.parameters if k!=key))
            if c.category_id==category_id else c for c in self.categories))

    @property
    def content_digest(self):
        # Canonical primitive forms, never an object repr containing memory IDs.
        return digest(configuration_document(self))


def parameter_document(parameters):
    values=parameters.values()
    return {key:dict(kind=value.kind,threshold=str(value.threshold) if value.threshold is not None else None)
            if isinstance(value,StockoutControl) else str(value) if isinstance(value,Fraction) else value
            for key,value in values.items()}


def configuration_document(configuration):
    return dict(version=configuration.version,parameters=parameter_document(configuration.global_parameters),
        coverage=dict(reorder_weeks=str(configuration.global_coverage.reorder_weeks),
                      target_weeks=str(configuration.global_coverage.target_weeks)),
        categories=[dict(category_id=c.category_id,
            parameters=[(key, dict(kind=value.kind,threshold=str(value.threshold) if value.threshold is not None else None)
                         if isinstance(value,StockoutControl) else str(value) if isinstance(value,Fraction) else value)
                        for key,value in c.parameters],
            coverage=None if c.coverage is None else (str(c.coverage.reorder_weeks),str(c.coverage.target_weeks)))
            for c in configuration.categories])
