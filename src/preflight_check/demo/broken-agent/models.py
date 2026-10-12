from pydantic import BaseModel


class Evidence(BaseModel):
    source: str
    finding: str


class Diagnosis(BaseModel):
    root_cause: str = ""
    root_cause_service: str = ""
    evidence: list[Evidence] = []
    summary: str = ""
