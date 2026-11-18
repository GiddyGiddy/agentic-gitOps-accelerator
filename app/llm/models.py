from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class FindingCategory(str, Enum):
    IMAGE_TAG = "image_tag_in_deployment"
    RELEASE_TARGET = "release_target_mismatch"
    INVALID_BRANCH = "invalid_release_branch"
    RECONCILIATION_HEALTHY = "reconciliation_healthy"
    RECONCILIATION_UNHEALTHY = "reconciliation_unhealthy"


class AdvisoryInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    task: Literal["routing", "reconciliation"]
    categories: tuple[FindingCategory, ...] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def validate_categories(self):
        routing = {FindingCategory.IMAGE_TAG, FindingCategory.RELEASE_TARGET, FindingCategory.INVALID_BRANCH}
        reconciliation = {FindingCategory.RECONCILIATION_HEALTHY, FindingCategory.RECONCILIATION_UNHEALTHY}
        allowed = routing if self.task == "routing" else reconciliation
        if not set(self.categories).issubset(allowed) or len(set(self.categories)) != len(self.categories):
            raise ValueError("Invalid or duplicate advisory categories")
        if self.task == "reconciliation" and len(self.categories) != 1:
            raise ValueError("Reconciliation requires exactly one health category")
        return self


class AdvisoryOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    summary: str = Field(min_length=1, max_length=1200)
    recommendations: list[str] = Field(min_length=1, max_length=4)

    @model_validator(mode="after")
    def validate_recommendations(self):
        if any(not value or len(value) > 400 for value in self.recommendations):
            raise ValueError("Recommendations must be nonempty and at most 400 characters")
        return self


class QueryIntentOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    intent: Literal["deployed_version", "branch_comparison", "jira_fix", "release_origin", "unsupported"]
