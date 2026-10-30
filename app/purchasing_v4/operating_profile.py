"""Explicit final operating package. Reading it never installs or approves values."""
from pathlib import Path
import json
from .controls import validate
from .calculation_documents import configuration_from_document


def load_operating_package():
    package=json.loads((Path(__file__).parent/'profiles/operating_20261029.json').read_text())
    values=package['global_control']
    if values['status']!='DRAFT' or not package['owner_activation_required']:
        raise ValueError('A release package cannot self-approve owner settings')
    validate('GLOBAL','global',values)
    configuration_from_document(dict(values,id=package['version']))
    return package
