from pydantic import BaseModel, Field


class RollInput(BaseModel):
    code: str = Field(min_length=2, max_length=80)
    name: str = Field(default='', max_length=160)
    note: str = Field(default='', max_length=500)


class RollLinkInput(BaseModel):
    parent: str = Field(min_length=2, max_length=80)
    child: str = Field(min_length=2, max_length=80)
    kind: str = Field(default='split', max_length=20)


class UsageInput(BaseModel):
    roll: str = Field(min_length=2, max_length=80)
    run: str = Field(min_length=1, max_length=120)
    note: str = Field(default='', max_length=300)


class RecallInput(BaseModel):
    roll: str = Field(min_length=2, max_length=80)
    reason: str = Field(default='', max_length=500)


class ScanInput(BaseModel):
    batch_size: int = Field(default=200, ge=1, le=1000)
