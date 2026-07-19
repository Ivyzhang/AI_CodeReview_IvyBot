from __future__ import annotations

import yaml
from pydantic import ValidationError

from app.models import RepositoryPolicy


class PolicyError(ValueError):
    pass


def parse_policy(content: str | None) -> RepositoryPolicy:
    if not content:
        return RepositoryPolicy()
    try:
        data = yaml.safe_load(content) or {}
        if not isinstance(data, dict):
            raise PolicyError("repository policy must be a mapping")
        return RepositoryPolicy.model_validate(data)
    except (yaml.YAMLError, ValidationError) as exc:
        raise PolicyError("invalid .ai-review.yml") from exc
