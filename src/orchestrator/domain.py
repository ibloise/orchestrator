"""Provider-independent definitions and validation errors."""
from __future__ import annotations

import json


class ValidationError(ValueError):
    """The requested operation violates a domain rule."""


class NotFoundError(LookupError):
    """An identifier does not exist."""


def nonempty(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f'{field} must be nonempty text')
    return value.strip()


def json_object(value: dict, field: str) -> dict:
    if not isinstance(value, dict):
        raise ValidationError(f'{field} must be a JSON object')
    try:
        return json.loads(json.dumps(value, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ValidationError(f'{field} must contain valid JSON') from exc


def task_definition(value: dict) -> dict:
    value = json_object(value, 'task definition')
    value['description'] = nonempty(value.get('description'), 'description')
    criteria = value.get('acceptance_criteria')
    if not isinstance(criteria, list) or not criteria:
        raise ValidationError('acceptance_criteria must be a nonempty list')
    value['acceptance_criteria'] = [nonempty(item, 'acceptance criterion') for item in criteria]
    for field in ('suggested_role', 'complexity', 'model_profile'):
        if field in value:
            value[field] = nonempty(value[field], field)
    if 'context_references' in value:
        refs = value['context_references']
        if not isinstance(refs, list) or any(not isinstance(ref, dict) for ref in refs):
            raise ValidationError('context_references must be a list of JSON objects')
    return value
