# 最小门禁入口：运行聚焦检查并写入修订级验收记录
# 在 Windows (PowerShell) 下：
#     make check          # 仅校验
#     make gate           # 校验 + 写入验收记录
#     make help

PY ?= python
CHECK := $(PY) tools/change_check.py

.PHONY: help check gate

help:
	@echo "make check  - 静态门禁（python/json/api surface）"
	@echo "make gate   - 同 check，并把结果写入 tools/check_log.jsonl"

check:
	$(CHECK)

gate:
	$(CHECK) --record
