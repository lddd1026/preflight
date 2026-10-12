def truncate(text: str, limit: int = 2000) -> str:
    """把工具返回裁到预算内。"""
    return text[:limit]
