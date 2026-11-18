from pydantic import BaseModel, Field


class ApprovalRequest(BaseModel):
    project_id: int = Field(gt=0)
    release: str = Field(min_length=1, max_length=128)
    wave: str = Field(min_length=1, max_length=64)
    actor: str = Field(min_length=1, max_length=256)
    request_id: str = Field(min_length=1, max_length=128)
