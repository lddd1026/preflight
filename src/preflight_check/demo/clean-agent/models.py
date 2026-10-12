from pydantic import BaseModel, Field


class Evidence(BaseModel):
    source: str
    finding: str


class Diagnosis(BaseModel):
    root_cause: str = Field(...)
    root_cause_service: str = Field(...)
    evidence: list[Evidence] = Field(...)
    summary: str = Field(...)


def check_evidence(diagnosis, tool_calls):
    """每条依据都要对上一次真实发生过的工具调用。"""
    return all(any(e.source in call for call in tool_calls) for e in diagnosis.evidence)


def accept(diagnosis, tool_calls):
    """结论只有在依据能对上时才收下。"""
    return diagnosis if check_evidence(diagnosis, tool_calls) else None
