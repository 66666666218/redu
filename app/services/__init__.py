"""服务层(业务逻辑)包。

注意:这里不急切导入子模块,避免 `python -m app.services.pipeline`
触发 `app.services` 的 `__init__` 而导致双重导入警告。
"""
