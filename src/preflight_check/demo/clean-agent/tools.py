LIMIT = 2000


def truncate(text: str, total: int, limit: int = LIMIT) -> str:
    """裁到预算内 —— **并且告诉模型被裁过**。"""
    notice = f"（共 {total} 条，以下是前 {limit} 个字符）\n"
    return notice + text[:limit]
