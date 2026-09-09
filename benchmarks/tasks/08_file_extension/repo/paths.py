def file_extension(filename: str) -> str:
    """返回不带点的小写文件扩展名。"""
    return filename.split(".")[-1]
