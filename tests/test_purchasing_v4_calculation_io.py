from dataclasses import replace
import json
from fractions import Fraction
import pytest
from app.purchasing_v4.calculation_cli import calculate,atomic_publish
from app.purchasing_v4.calculation_documents import store_from_document,exact
from app.purchasing_v4.calculation_serialization import primitive,canonical_bytes
from app.purchasing_v4.calculation import ProductCoverageOverride
from app.purchasing_v4.calculation_config import Coverage,CategoryOverride
from test_purchasing_v4_calculation import row as fixture,configuration as config,ASOF


def test_roundtrip_and_atomic_retry(tmp_path):
    row=fixture()
    row=replace(row,product_coverage=ProductCoverageOverride(Coverage(Fraction(3),Fraction(7)),'owner-fixture'))
    assert store_from_document(primitive(row))==row
    profiles=json.load(open('tests/fixtures/purchasing_v4_phase2_profiles.json'))['profiles']
    source=tmp_path/'input.json';profile=tmp_path/'profile.json';out=tmp_path/'result.json'
    source.write_bytes(canonical_bytes(dict(as_of=ASOF,rows=(row,),source_exceptions=())))
    profile.write_text(json.dumps(profiles[0]))
    calculate(source,profile,out);before=out.read_bytes()
    def crash():raise RuntimeError('Injected crash before atomic publication')
    with pytest.raises(RuntimeError):atomic_publish(out,b'incomplete',before_replace=crash)
    assert out.read_bytes()==before
    assert not list(tmp_path.glob('.result.json.*'))
    calculate(source,profile,out);assert out.read_bytes()==before
    profile.write_text('{}')
    with pytest.raises(KeyError):calculate(source,profile,out)
    assert out.read_bytes()==before


@pytest.mark.parametrize('value',[0.2,True,{'numerator':True,'denominator':2}])
def test_exact_rejects_lossy_inputs(value):
    with pytest.raises(ValueError):exact(value)


def test_immutable_category_pairs():
    with pytest.raises(ValueError):CategoryOverride('c',(['recent_influence',Fraction(1,2)],),None)
