from __future__ import annotations

from pydantic import BaseModel, Field


class MoneyMakerComponentForm(BaseModel):
    item_id: int
    role: str
    quantity_per_hour: int = Field(gt=0)
    valuation_mode: str = "market"
    notes: str | None = None


class MoneyMakerCreateForm(BaseModel):
    name: str
    category: str = "processing"
    units_per_hour: int = Field(gt=0)
    notes: str | None = None
